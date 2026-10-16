"""Ekran „Čekovi“: poslovodja slika, proverava crveno, predaje u banku.

Kancelarija (operater) vidi sve jedinice i preseke predaje. Rute su ovde, a ne
u `app.py`, da se modul cekova moze citati (i jednom izdvojiti) zasebno.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from ..config import get_settings
from ..db import session_scope
from ..models import BusinessUnit, Cek, PredajaCekova, SlikaCekova, StanjeCeka
from ..services import cekovi as C
from .app import Korisnik, _back, _unit_for, log, templates

router = APIRouter()


def _jedinica(session, user, kod: str | None = None) -> BusinessUnit:
    """Jedinica na kojoj se radi: poslovodja - svoja; operater - izabrana."""
    if user.is_operator:
        if not kod:
            raise HTTPException(400, "Izaberi jedinicu.")
        j = session.scalar(select(BusinessUnit).where(BusinessUnit.code == kod))
        if j is None:
            raise HTTPException(404, "Nema te jedinice.")
        return j
    j = _unit_for(session, user)
    if j is None:
        raise HTTPException(403, "Nalog nije vezan za jedinicu.")
    return j


def _sme(session, user, business_unit_id: int) -> None:
    if user.is_operator:
        return
    j = _unit_for(session, user)
    if j is None or j.id != business_unit_id:
        raise HTTPException(403, "To nije tvoja jedinica.")


def _nazad(user, jedinica_kod: str) -> str:
    return f"/cekovi?jedinica={jedinica_kod}" if user.is_operator else "/cekovi"


@router.get("/cekovi", response_class=HTMLResponse)
def cekovi_strana(request: Request, user: Korisnik, jedinica: str = "") -> HTMLResponse:
    danas = date.today()
    with session_scope() as session:
        if user.is_operator and not jedinica:
            # Pregled svih jedinica: koliko je u sefu, sta je za predaju, vraceni.
            redovi = session.execute(
                select(BusinessUnit.code, BusinessUnit.name, Cek.stanje,
                       func.count(Cek.id), func.sum(Cek.iznos))
                .join(Cek, Cek.business_unit_id == BusinessUnit.id)
                .group_by(BusinessUnit.code, BusinessUnit.name, Cek.stanje)
                .order_by(BusinessUnit.code)
            ).all()
            po_jedinici: dict[str, dict] = {}
            for kod, ime, stanje, n, s in redovi:
                d = po_jedinici.setdefault(kod, {"kod": kod, "ime": ime})
                d[stanje.value] = (n, float(s or 0))
            predaje = session.scalars(
                select(PredajaCekova).order_by(PredajaCekova.predato_u.desc()).limit(50)
            ).all()
            for p in predaje:
                _ = p.business_unit.code
            session.expunge_all()
            return templates.TemplateResponse(request, "cekovi_pregled.html", {
                "user": user, "jedinice": list(po_jedinici.values()), "predaje": predaje,
            })

        j = _jedinica(session, user, jedinica)
        slike = session.scalars(
            select(SlikaCekova).where(SlikaCekova.business_unit_id == j.id)
            .order_by(SlikaCekova.id.desc()).limit(20)
        ).all()
        u_sefu = session.scalars(
            select(Cek).where(Cek.business_unit_id == j.id, Cek.stanje == StanjeCeka.U_SEFU)
            .order_by(Cek.datum, Cek.id)
        ).all()
        za_proveru = [c for c in u_sefu if c.za_proveru]
        za_predaju = C.za_predaju(session, j.id, dan=danas)
        vraceni = session.scalars(
            select(Cek).where(Cek.business_unit_id == j.id, Cek.stanje == StanjeCeka.VRACEN)
            .order_by(Cek.vracen_u)
        ).all()
        predati = session.scalars(
            select(Cek).where(Cek.business_unit_id == j.id, Cek.stanje == StanjeCeka.PREDAT)
            .order_by(Cek.datum.desc()).limit(60)
        ).all()
        predaje = session.scalars(
            select(PredajaCekova).where(PredajaCekova.business_unit_id == j.id)
            .order_by(PredajaCekova.predato_u.desc()).limit(12)
        ).all()

        # Sef po danu predaje: koliko komada i koji iznos ide kog dana.
        po_roku: dict[date, list] = defaultdict(lambda: [0, 0.0])
        for c in u_sefu:
            rok = C.rok_predaje(c.datum)
            if rok is not None:
                po_roku[rok][0] += 1
                po_roku[rok][1] += c.iznos
        kalendar = sorted(po_roku.items())
        ukupno = (len(u_sefu), sum(c.iznos for c in u_sefu))
        session.expunge_all()

    return templates.TemplateResponse(request, "cekovi.html", {
        "user": user, "jedinica": j, "slike": slike, "za_proveru": za_proveru,
        "za_predaju": za_predaju, "vraceni": vraceni, "predati": predati,
        "predaje": predaje, "kalendar": kalendar, "ukupno": ukupno,
        "u_sefu": u_sefu, "danas": danas, "juce": danas - timedelta(days=1),
        "claude_ukljucen": bool(get_settings().anthropic_api_key),
        "lep_racun": C.lep_racun, "rok": C.rok_predaje,
    })


@router.post("/cekovi/slike")
async def cekovi_slike(
    user: Korisnik,
    background: BackgroundTasks,
    slike: Annotated[list[UploadFile], File()],
    primljeno: Annotated[str, Form()] = "",
    jedinica: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Poslovodja salje jednu ili vise fotografija; citanje ide u pozadini."""
    with session_scope() as session:
        j = _jedinica(session, user, jedinica)
        jid, kod = j.id, j.code
    nazad = _nazad(user, kod)
    try:
        dan = date.fromisoformat(primljeno) if primljeno.strip() else None
    except ValueError:
        return _back(nazad, "Datum pazara nije ispravan.", False)

    ids, greske = [], []
    for s in slike:
        sadrzaj = await s.read()
        if not sadrzaj:
            continue
        try:
            ids.append(C.sacuvaj_sliku(jid, sadrzaj, mime=s.content_type,
                                       naziv_fajla=s.filename, korisnik=user.login,
                                       primljeno=dan))
        except ValueError as exc:
            greske.append(f"{s.filename}: {exc}")
    for i in ids:
        background.add_task(C.procitaj_sliku, i)
    if not ids:
        return _back(nazad, "Nijedna slika nije primljena. " + " ".join(greske), False)
    poruka = (f"Primljeno slika: {len(ids)}. Čekovi se čitaju — osveži stranu za "
              f"pola minuta po slici.")
    if greske:
        poruka += " Nije primljeno: " + "; ".join(greske)
    return _back(nazad, poruka)


@router.get("/cekovi/slika/{slika_id}")
def cekovi_slika(slika_id: int, user: Korisnik) -> FileResponse:
    with session_scope() as session:
        s = session.get(SlikaCekova, slika_id)
        if s is None:
            raise HTTPException(404, "Nema te slike.")
        _sme(session, user, s.business_unit_id)
        putanja = s.putanja
    if not Path(putanja).exists():
        raise HTTPException(404, "Fajl slike nije na disku.")
    return FileResponse(putanja)


@router.post("/cekovi/slika/{slika_id}/ponovo")
def cekovi_slika_ponovo(slika_id: int, user: Korisnik, background: BackgroundTasks) -> RedirectResponse:
    with session_scope() as session:
        s = session.get(SlikaCekova, slika_id)
        if s is None:
            raise HTTPException(404, "Nema te slike.")
        _sme(session, user, s.business_unit_id)
        kod = session.get(BusinessUnit, s.business_unit_id).code
        s.procitano_u, s.greska = None, None
    background.add_task(C.procitaj_sliku, slika_id)
    return _back(_nazad(user, kod), "Slika se čita ponovo.")


def _cek_i_nazad(session, user, cek_id: int) -> tuple[Cek, str]:
    cek = session.get(Cek, cek_id)
    if cek is None:
        raise HTTPException(404, "Nema tog čeka.")
    _sme(session, user, cek.business_unit_id)
    return cek, _nazad(user, session.get(BusinessUnit, cek.business_unit_id).code)


@router.post("/cekovi/{cek_id}/ispravi")
def cekovi_ispravi(
    cek_id: int, user: Korisnik,
    serijski_broj: Annotated[str, Form()] = "",
    racun: Annotated[str, Form()] = "",
    iznos: Annotated[str, Form()] = "",
    datum: Annotated[str, Form()] = "",
) -> RedirectResponse:
    with session_scope() as session:
        _, nazad = _cek_i_nazad(session, user, cek_id)
    try:
        C.ispravi(cek_id, korisnik=user.login, serijski_broj=serijski_broj,
                  racun=racun, iznos=iznos, datum=datum)
    except ValueError as exc:
        return _back(nazad, str(exc), False)
    return _back(nazad, "Ček je potvrđen.")


@router.post("/cekovi/{cek_id}/obrisi")
def cekovi_obrisi(cek_id: int, user: Korisnik) -> RedirectResponse:
    with session_scope() as session:
        _, nazad = _cek_i_nazad(session, user, cek_id)
    try:
        C.obrisi(cek_id, korisnik=user.login)
    except ValueError as exc:
        return _back(nazad, str(exc), False)
    return _back(nazad, "Ček je obrisan.")


@router.post("/cekovi/predaj")
def cekovi_predaj(
    user: Korisnik,
    cekovi: Annotated[list[int], Form()] = None,
    jedinica: Annotated[str, Form()] = "",
) -> RedirectResponse:
    with session_scope() as session:
        j = _jedinica(session, user, jedinica)
        jid, kod = j.id, j.code
    try:
        predaja_id = C.predaj(jid, list(cekovi or []), korisnik=user.login)
    except ValueError as exc:
        return _back(_nazad(user, kod), str(exc), False)
    return RedirectResponse(f"/cekovi/predaja/{predaja_id}", status_code=303)


@router.get("/cekovi/predaja/{predaja_id}", response_class=HTMLResponse)
def cekovi_presek(request: Request, predaja_id: int, user: Korisnik) -> HTMLResponse:
    """Presek za knjigovodstvo - jedna strana, za stampu ili slanje."""
    with session_scope() as session:
        p = session.get(PredajaCekova, predaja_id)
        if p is None:
            raise HTTPException(404, "Nema te predaje.")
        _sme(session, user, p.business_unit_id)
        _ = p.business_unit.name
        cekovi = sorted(p.cekovi, key=lambda c: (c.datum or date.min, c.id))
        session.expunge_all()
    return templates.TemplateResponse(request, "cekovi_presek.html", {
        "user": user, "p": p, "cekovi": cekovi, "lep_racun": C.lep_racun,
    })


@router.post("/cekovi/{cek_id}/vracen")
def cekovi_vracen(cek_id: int, user: Korisnik,
                  napomena: Annotated[str, Form()] = "") -> RedirectResponse:
    with session_scope() as session:
        _, nazad = _cek_i_nazad(session, user, cek_id)
    try:
        C.vrati(cek_id, korisnik=user.login, napomena=napomena)
    except ValueError as exc:
        return _back(nazad, str(exc), False)
    return _back(nazad, "Zabeleženo: banka je vratila ček.")


@router.post("/cekovi/{cek_id}/resen")
def cekovi_resen(cek_id: int, user: Korisnik,
                 napomena: Annotated[str, Form()] = "") -> RedirectResponse:
    with session_scope() as session:
        _, nazad = _cek_i_nazad(session, user, cek_id)
    try:
        C.resi(cek_id, korisnik=user.login, napomena=napomena)
    except ValueError as exc:
        return _back(nazad, str(exc), False)
    return _back(nazad, "Vraćen ček je rešen.")


__all__ = ["router", "log"]
