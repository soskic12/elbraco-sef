"""Radni panel za ulazne e-fakture sa SEF-a.

Dva ekrana, dve uloge:

  OPERATER    — jedan red sa svim dokumentima koji traže potez: potvrdi ili
                ispravi poslovnu jedinicu, prihvati ili odbij na SEF-u,
                prosledi poslovnoj jedinici. Prihvatanje i prosleđivanje su
                nezavisni: operater bira redosled.
  POSLOVOĐA   — vidi samo svoju jedinicu i samo ono što mu je prosleđeno,
                i potvrđuje da je roba stigla.

Dokument napušta red tek kad je i na SEF-u odlučeno i prosleđen.
Pokretanje:  sefsync serve
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from ..erp.kalkulacija import MAGACIN_ULAZNI_RACUN
from ..routing.engine import MAGACIN_POVRACAJ
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware

from ..config import get_settings
from ..db import init_db, session_scope
from ..logging_setup import setup_logging
from ..models import (
    AuditLog,
    BusinessUnit,
    Document,
    DocumentType,
    MatchField,
    MatchOp,
    NacrtKalkulacije,
    Notification,
    ObavestenjeOPrethodnomPorezu,
    OdlukaManjka,
    PrijavaManjka,
    ProcessState,
    RoutingRule,
    SefStatus,
    Slika,
    StanjeNacrta,
    SyncRun,
    UnitKind,
)
from ..notify.dispatcher import TYPE_LABEL
from ..services.acceptance import AcceptanceService
from ..services.ingest import IngestService
from ..services.workflow import Workflow
from .auth import AuthError, Role, User, authenticate

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tip"] = lambda t: TYPE_LABEL.get(t, str(t))

ODLUCENO = [SefStatus.APPROVED, SefStatus.REJECTED, SefStatus.STORNO]


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging(naziv="sefsync")
    init_db()
    log.info("Panel pokrenut (SEF: %s)", get_settings().base_url)
    yield


app = FastAPI(
    title="ELBRACO — e-fakture (SEF)",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(SessionMiddleware, secret_key=get_settings().web_secret, max_age=12 * 3600)


# --------------------------------------------------------------------------- #
# prijava
# --------------------------------------------------------------------------- #


def current_user(request: Request) -> User:
    data = request.session.get("user")
    if not data:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/prijava"})
    uloga = Role(data["role"])
    login = data["login"]

    # Uloga je upisana u kolacic pri prijavi, pa covek skinut sa spiska
    # operatera ostaje operater dok se ne odjavi. Spisak stoji u memoriji, pa
    # provera ne kosta nista - a bez nje je Bogdan posle prebacivanja na 088
    # i dalje video operaterski ekran.
    if uloga is Role.OPERATER:
        podesavanja = get_settings()
        priznat = (
            login.strip().lower() in podesavanja.operator_logins()
            or login in podesavanja.web_credentials()
        )
        if not priznat:
            uloga = Role.POSLOVODJA

    return User(
        login=login,
        name=data["name"],
        role=uloga,
        unit_code=data.get("unit_code"),
        function=data.get("function", ""),
    )


def operator_only(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_operator:
        raise HTTPException(403, "Ovaj ekran je za operatera.")
    return user


Korisnik = Annotated[User, Depends(current_user)]
Operater = Annotated[User, Depends(operator_only)]


@app.get("/prijava", response_class=HTMLResponse)
def login_form(request: Request, greska: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"greska": greska, "user": None})


@app.post("/prijava")
def login(
    request: Request,
    korisnik: Annotated[str, Form()],
    lozinka: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        user = authenticate(korisnik.strip(), lozinka)
    except AuthError as exc:
        return RedirectResponse(f"/prijava?greska={quote(str(exc))}", status_code=303)

    request.session["user"] = {
        "login": user.login,
        "name": user.name,
        "role": user.role.value,
        "unit_code": user.unit_code,
        "function": user.function,
    }
    log.info("Prijava: %s (%s)", user.login, user.role.value)
    return RedirectResponse("/", status_code=303)


@app.get("/odjava")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/prijava", status_code=303)


# --------------------------------------------------------------------------- #
# pomocno
# --------------------------------------------------------------------------- #


def _back(url: str, message: str, ok: bool = True) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    prefix = "" if ok else "GREŠKA: "
    return RedirectResponse(f"{url}{sep}poruka={quote(prefix + message)}", status_code=303)


def _unit_for(session, user: User) -> BusinessUnit | None:
    """Poslovna jedinica poslovodje - po sifri objekta iz sifarnika osoblja.

    Prvo se gleda spisak prijava upisan uz jedinicu (`logins`): sifarnik osoblja
    deli i projekat LOGISTIKA, pa se u njega ne upisuje nasa sema. Kad prijava
    nije nigde navedena, trazi se po `SifraPovezanogObjekta` -> `erp_code`, kako
    rade prodavnice.
    """
    prijava = (user.login or "").strip().lower()
    code = (user.unit_code or "").strip()
    if prijava:
        # ORDER BY nije kozmetika: bez njega MS SQL sme da vrati redove drugim
        # redosledom pri svakom pozivu, pa covek cija je prijava upisana uz vise
        # jedinica dobije cas jednu cas drugu. Mereno 28.09. na produkciji:
        # Bogdan je upisao kalkulaciju (088/03119), a preusmerenje na istu
        # fakturu sekund kasnije vratilo 403 - ista provera, drugi ishod.
        pogodjene = [
            j
            for j in session.scalars(
                select(BusinessUnit)
                .where(BusinessUnit.logins.is_not(None))
                .order_by(BusinessUnit.id)
            )
            if prijava in j.login_list()
        ]
        if len(pogodjene) > 1:
            # Isti redosled kao u putu bez dvosmislenosti: sifra 088 znaci
            # "radi ulazne racune" i vodi na upravu, i to pre poklapanja po
            # sifri magacina - inace bi Bogdan zavrsio na MAG088, magacinu bez
            # ijednog dokumenta, zbog cega mu je ekran jednom vec bio prazan.
            izbor = None
            if code == MAGACIN_ULAZNI_RACUN:
                izbor = next((j for j in pogodjene if j.kind is UnitKind.HQ), None)
            if izbor is None:
                izbor = next((j for j in pogodjene if (j.erp_code or "").strip() == code), None)
            izbor = izbor or pogodjene[0]
            log.warning(
                "Prijava %s je upisana uz više jedinica (%s) — uzimam %s. "
                "Očisti spisak prijava, inače ishod zavisi od redosleda.",
                prijava,
                ", ".join(j.code for j in pogodjene),
                izbor.code,
            )
            return izbor
        if pogodjene:
            return pogodjene[0]
    if not code:
        return None

    # Sifra objekta 088 u sifarniku osoblja znaci "radi ulazne racune". Sam
    # magacin 088 nema nijedan dokument - usluzne fakture se razvrstavaju na
    # upravu, a 088 nastaje tek pri pravljenju kalkulacije. Zato se covek sa 088
    # vodi na upravu, gde ti racuni stvarno stoje (1.953 dokumenta).
    if code == MAGACIN_ULAZNI_RACUN:
        uprava = session.scalar(
            select(BusinessUnit).where(BusinessUnit.kind == UnitKind.HQ)
        )
        if uprava is not None:
            return uprava

    unit = session.scalar(
        select(BusinessUnit).where(
            BusinessUnit.erp_code == code, BusinessUnit.kind == UnitKind.RETAIL
        )
    )
    return unit or session.scalar(select(BusinessUnit).where(BusinessUnit.erp_code == code))


def _broj(vrednost: str | int | None) -> int | None:
    """Prazan izbor u formi je "nije birano", a ne nula."""
    if vrednost is None or vrednost == "":
        return None
    try:
        return int(vrednost)
    except (TypeError, ValueError):
        return None


def _ids(raw: list[str]) -> list[int]:
    out: list[int] = []
    for value in raw:
        for part in str(value).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out


def _prebroj(upit):
    """Broj redova koje upit vraca, bez zamotavanja u podupit.

    `select(count()).select_from(upit.subquery())` izvlaci sve kolone svih
    redova pa ih prebroji. Stranica reda to radi sedam puta po otvaranju -
    jednom za ukupno i jednom za svaku karticu - pa je na 8.000 dokumenata
    drzala vezu ka bazi po pedesetak sekundi. Mereno 29.09. na produkciji: tri
    takva zahteva su drzala tri od pet veza, a ljudi nisu mogli da se prijave.
    """
    return upit.with_only_columns(func.count(), maintain_column_froms=True).order_by(None)


def _queue_filter(query, prikaz: str):
    """Koji dokumenti pripadaju kojem prikazu."""
    aktivni = Document.archived == False
    if prikaz == "arhiva":
        return query.where(Document.archived == True)
    if prikaz == "nepregledano":
        # Ono sto je kolegama na portalu bio status "Nova" - samo sto oznaku
        # sada vodimo mi, pa preuzimanje UBL-a ne mora nikoga da remeti.
        return query.where(
            aktivni, Document.pregledano_at.is_(None), Document.forwarded_at.is_(None)
        )
    if prikaz == "nerazvrstano":
        return query.where(aktivni, Document.business_unit_id.is_(None))
    if prikaz == "ceka_prijem":
        return query.where(
            aktivni, Document.forwarded_at.isnot(None), Document.received_at.is_(None)
        )
    if prikaz == "manjak":
        # Poslovodja je prijavio da je stiglo manje. Operater to mora da vidi
        # jer on drzi prihvatanje i odbijanje na SEF-u, a rok tece.
        return query.join(
            PrijavaManjka, PrijavaManjka.document_id == Document.id
        ).where(aktivni, PrijavaManjka.zatvoreno_u.is_(None))
    # "red": traži potez dok i SEF i prosleđivanje nisu gotovi
    return query.where(
        aktivni,
        or_(Document.forwarded_at.is_(None), Document.sef_status.notin_(ODLUCENO)),
    )


# --------------------------------------------------------------------------- #
# operaterov red
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: Korisnik):
    if not user.is_operator:
        return RedirectResponse("/moja-jedinica", status_code=303)
    return queue(request, user)


@app.get("/red", response_class=HTMLResponse)
def queue(
    request: Request,
    user: Operater,
    pj: str | None = None,
    tip: str | None = None,
    q: str | None = None,
    prikaz: str = "red",
    strana: str = "1",
) -> HTMLResponse:
    # Izbor "— sve —" u formi stize kao prazan string, a ne kao izostanak
    # parametra, pa se ne moze primiti kao int.
    pj = _broj(pj)
    strana = _broj(strana) or 1
    per_page = 60
    with session_scope() as session:
        query = _queue_filter(select(Document).options(selectinload(Document.business_unit)), prikaz)

        if pj == 0:
            query = query.where(Document.business_unit_id.is_(None))
        elif pj:
            query = query.where(Document.business_unit_id == pj)
        if tip:
            query = query.where(Document.document_type == DocumentType(tip))
        if q:
            needle = f"%{q.strip()}%"
            query = query.where(
                or_(
                    Document.document_number.ilike(needle),
                    Document.supplier_name.ilike(needle),
                    Document.supplier_vat.ilike(needle),
                    Document.delivery_address.ilike(needle),
                )
            )

        total = session.scalar(_prebroj(query)) or 0
        rows = list(
            session.scalars(
                query.order_by(Document.sent_date.desc(), Document.id.desc())
                .offset((max(1, strana) - 1) * per_page)
                .limit(per_page)
            )
        )
        units = list(
            session.scalars(
                select(BusinessUnit)
                .where(BusinessUnit.active == True)
                .order_by(BusinessUnit.kind, BusinessUnit.code)
            )
        )
        brojaci = {
            naziv: session.scalar(_prebroj(_queue_filter(select(Document), naziv)))
            for naziv in (
                "red", "nepregledano", "nerazvrstano", "ceka_prijem", "manjak", "arhiva"
            )
        }
        last_run = session.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        return templates.TemplateResponse(
            request,
            "queue.html",
            {
                "popusta": _broj_popusta(session),
                "zaustavljenih": _broj_zaustavljenih(session),
                "documents": rows,
                "units": units,
                "brojaci": brojaci,
                "last_run": last_run,
                "total": total,
                "page": max(1, strana),
                "pages": max(1, (total + per_page - 1) // per_page),
                "prikaz": prikaz,
                "filters": {"pj": pj, "tip": tip, "q": q},
                "types": list(DocumentType),
                "user": user,
            },
        )


# --------------------------------------------------------------------------- #
# radnje nad dokumentima
# --------------------------------------------------------------------------- #


@app.post("/prosledi")
def forward(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    rezultat = Workflow().forward(_ids(dokumenti), actor=user.login)
    return _back(povratak, rezultat.summary(), rezultat.ok)


@app.post("/prihvati")
def accept_many(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    """Grupno prihvatanje. Odbijanje ide pojedinacno - trazi obrazlozenje."""
    servis = AcceptanceService()
    prihvaceno, problemi = 0, []
    for doc_id in _ids(dokumenti):
        ishod = servis.accept(doc_id, actor=user.login)
        if ishod.ok:
            prihvaceno += 1
        else:
            problemi.append(f"#{doc_id}: {ishod.message}")
    poruka = f"Prihvaćeno na SEF-u: {prihvaceno}."
    if problemi:
        poruka += " " + "; ".join(problemi[:3])
    return _back(povratak, poruka, prihvaceno > 0 or not problemi)


@app.post("/potvrdi")
def confirm_routing(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    """Predlog je tacan - bez ove potvrde se tacnost razvrstavanja ne moze meriti."""
    rezultat = Workflow().confirm_routing(_ids(dokumenti), actor=user.login)
    return _back(povratak, rezultat.summary(), rezultat.ok)


@app.post("/arhiviraj")
def archive(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    rezultat = Workflow().archive(_ids(dokumenti), actor=user.login)
    return _back(povratak, rezultat.summary(), rezultat.ok)


@app.post("/dokument/{doc_id}/vrati")
def unarchive(doc_id: int, user: Operater) -> RedirectResponse:
    rezultat = Workflow().unarchive(doc_id, actor=user.login)
    return _back(f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/pj")
def assign_unit(
    doc_id: int,
    user: Operater,
    business_unit_id: Annotated[int, Form()],
    napravi_pravilo: Annotated[str, Form()] = "",
    polje: Annotated[str, Form()] = "",
    povratak: Annotated[str, Form()] = "",
) -> RedirectResponse:
    # Prazno polje znaci "izaberi sam": uzima se najuzi podatak koji taj dokument
    # ima. Ranije je ovde stajala adresa isporuke kao podrazumevana, pa se kod
    # dokumenata bez adrese pravilo nije ni pravilo.
    rezultat = Workflow().assign_unit(
        doc_id,
        business_unit_id,
        actor=user.login,
        remember=bool(napravi_pravilo),
        field_name=polje or None,
    )
    return _back(povratak or f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/prihvati")
def accept(
    doc_id: int,
    user: Operater,
    komentar: Annotated[str, Form()] = "",
    povratak: Annotated[str, Form()] = "",
) -> RedirectResponse:
    rezultat = AcceptanceService().accept(doc_id, actor=user.login, comment=komentar)
    return _back(povratak or f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/odbij")
def reject(
    doc_id: int,
    user: Operater,
    komentar: Annotated[str, Form()] = "",
    povratak: Annotated[str, Form()] = "",
) -> RedirectResponse:
    rezultat = AcceptanceService().reject(doc_id, actor=user.login, comment=komentar)
    return _back(povratak or f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/osvezi")
def refresh(doc_id: int, user: Operater) -> RedirectResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        sef_id = doc.sef_invoice_id
        jos_nova = doc.sef_status is SefStatus.NEW
    if jos_nova and get_settings().sef_preserve_new:
        # Preuzimanje bi ga na portalu prebacilo u "Viđena" i pomerilo kolegama
        # spisak novih. Operater mora da zna zasto se nista nije desilo.
        return _back(
            f"/dokument/{doc_id}",
            "Dokument je na SEF-u još „Nova“ — ne osvežavam ga da ne bih oborio taj "
            "status kolegama na portalu. Stavke stižu čim ga neko tamo otvori.",
            False,
        )
    IngestService().fetch_one(sef_id, force=True)
    return _back(f"/dokument/{doc_id}", "Dokument je ponovo preuzet sa SEF-a.")


@app.get("/dokument/{doc_id}", response_class=HTMLResponse)
def document_detail(request: Request, doc_id: int, user: Korisnik) -> HTMLResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if not _sme_na_dokument(session, user, doc):
            raise HTTPException(403, "Ovaj dokument nije prosleđen tvojoj jedinici.")
        units = list(
            session.scalars(
                select(BusinessUnit).where(BusinessUnit.active == True).order_by(BusinessUnit.code)
            )
        )
        notifications = list(
            session.scalars(
                select(Notification)
                .where(Notification.document_id == doc_id)
                .order_by(Notification.id.desc())
            )
        )
        audit = list(
            session.scalars(
                select(AuditLog).where(AuditLog.document_id == doc_id).order_by(AuditLog.id.desc())
            )
        )
        slike = list(
            session.scalars(
                select(Slika).where(Slika.document_id == doc_id).order_by(Slika.id)
            )
        )
        nacrt = session.get(NacrtKalkulacije, doc_id)
        # Za svaku stavku: da li je neko vec slikao taj artikal, makar sa druge
        # fakture i iz drugog objekta. Bez toga svaki poslovodja slika isti
        # artikal iznova.
        from ..services.slike import za_artikal

        slikano = {}
        for linija in doc.lines:
            if linija.erp_item_code:
                continue
            nadjene = za_artikal(
                session,
                ean=linija.standard_item_id,
                sifra_dobavljaca=linija.sellers_item_id,
                supplier_vat=doc.supplier_vat,
            )
            if nadjene:
                slikano[linija.line_no] = nadjene
        return templates.TemplateResponse(
            request,
            "document.html",
            {
                "doc": doc,
                "lines": doc.lines,
                "slike": slike,
                "slikano": slikano,
                "nacrt": nacrt,
                "manjak": session.get(PrijavaManjka, doc.id),
                "obavestenje": session.get(ObavestenjeOPrethodnomPorezu, doc.id),
                "upis_ukljucen": get_settings().erp_upis_kalkulacije,
                "units": units,
                "notifications": notifications,
                "audit": audit,
                "match_fields": list(MatchField),
                "user": user,
            },
        )


@app.post("/dokument/{doc_id}/nije-moje")
def vrati_dokument(
    doc_id: int, user: Korisnik, napomena: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Čovek iz jedinice vraća dokument kancelariji: nije za njega.

    Do sada procedure nije bilo. Bogdan ne može da prebaci na Ilijanu - tuđe
    jedinice su mu zabranjene, i tako treba - a dugmeta nije bilo, pa je jedino
    rešenje bilo javiti telefonom. To nigde nije ostavljalo trag.

    Dokument se vraća u „Nerazvrstano“ sa napomenom odakle je došao; operater
    odlučuje čiji je. Ako je prijem već potvrđen ili je kalkulacija upisana,
    vraćanje se odbija - tu je već nešto urađeno, pa to rešava kancelarija.
    """
    from ..models import RoutingSource, utcnow

    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if user.is_operator:
            return _back(f"/dokument/{doc_id}", "Ti si operater — dodeli je odmah.", False)
        if not _sme_na_dokument(session, user, doc):
            raise HTTPException(403, "Taj dokument nije tvoj.")

        povratak = "/moja-jedinica"
        if doc.received_at is not None:
            return _back(povratak, "Prijem je već potvrđen — javi kancelariji.", False)
        red = session.get(NacrtKalkulacije, doc_id)
        if red is not None and red.stanje is StanjeNacrta.UPISAN:
            return _back(povratak, "Kalkulacija je već u ERP-u — javi kancelariji.", False)

        moja = _unit_for(session, user)
        odakle = moja.code if moja is not None else "?"
        doc.business_unit_id = None
        doc.forwarded_at = None
        doc.forwarded_by = None
        doc.routing_source = RoutingSource.NONE
        doc.routing_rule_id = None
        doc.routing_corrected = True
        doc.routing_confirmed_at = None
        doc.routing_confirmed_by = None
        doc.state = ProcessState.UNASSIGNED
        doc.routing_note = (
            f"vratio {user.login} sa {odakle}: {napomena.strip() or 'nije za našu jedinicu'}"
        )[:500]
        session.add(AuditLog(
            actor=user.login, action="document.vracen", document_id=doc_id,
            detail=doc.routing_note,
        ))
    return _back(povratak, "Vraćeno kancelariji — oni odlučuju čija je.")


@app.post("/dokument/{doc_id}/manjak")
def prijavi_manjak(
    doc_id: int,
    user: Korisnik,
    odluka: Annotated[str, Form()],
    line_no: Annotated[list[int], Form()],
    stiglo: Annotated[list[str], Form()],
) -> RedirectResponse:
    """Poslovođa je otvorio kamion i stiglo je manje nego što faktura kaže.

    Količine stižu kao dve uparene liste (`line_no` i `stiglo`, po jedan par po
    stavci fakture); upisuju se samo stavke gde se stiglo razlikuje od
    fakturisanog. Šta app radi dalje zavisi od toga šta je dobavljač rekao —
    vidi `PrijavaManjka`.
    """
    try:
        izbor = OdlukaManjka(odluka)
    except ValueError:
        raise HTTPException(400, "Nepoznata odluka o manjku.") from None
    if len(line_no) != len(stiglo):
        raise HTTPException(400, "Količine ne odgovaraju stavkama.")

    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")
        povratak = f"/dokument/{doc_id}" if user.is_operator else "/moja-jedinica"
        if doc.po_starom:
            return _back(povratak, "Ova faktura je stigla pre prelaska na novi način rada.", False)

        po_broju = {s.line_no: s for s in doc.lines}
        manjak = []
        for broj, unos in zip(line_no, stiglo):
            stavka = po_broju.get(broj)
            if stavka is None or unos.strip() == "":
                continue
            try:
                koliko = float(unos.strip().replace(",", "."))
            except ValueError:
                return _back(povratak, f"„{unos}“ nije broj.", False)
            fakturisano = float(stavka.quantity or 0)
            if abs(koliko - fakturisano) < 1e-9:
                continue
            if koliko < 0 or koliko > fakturisano:
                return _back(
                    povratak,
                    f"Stiglo mora biti između 0 i {fakturisano:g} (stavka {broj}).",
                    False,
                )
            manjak.append({
                "line_no": broj,
                "naziv": stavka.name,
                "fakturisano": fakturisano,
                "stiglo": koliko,
            })

    if not manjak:
        return _back(povratak, "Nisi upisao nijednu količinu koja se razlikuje.", False)

    rezultat = Workflow().confirm_receipt(doc_id, actor=user.login, manjak=manjak, odluka=izbor)
    return _back(povratak, rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/manjak-zatvori")
def zatvori_manjak(doc_id: int, user: Korisnik, kako: Annotated[str, Form()] = "") -> RedirectResponse:
    """Ostatak je stigao — crvena linija pada i kalkulacija sme da se knjiži."""
    from ..models import utcnow
    from ..services.nacrti import prihvati_ako_je_kompletna

    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")
        prijava = session.get(PrijavaManjka, doc_id)
        povratak = f"/dokument/{doc_id}" if user.is_operator else "/moja-jedinica"
        if prijava is None or not prijava.otvoren:
            return _back(povratak, "Za ovu fakturu nema otvorenog manjka.", False)
        prijava.zatvorio = user.login
        prijava.zatvoreno_u = utcnow()
        prijava.zatvoreno_kako = (kako or "stigao ostatak")[:60]
        session.add(AuditLog(
            actor=user.login, action="document.manjak.zatvoren", document_id=doc_id,
            detail=prijava.zatvoreno_kako,
        ))
        upisana = (red := session.get(NacrtKalkulacije, doc_id)) is not None and (
            red.stanje is StanjeNacrta.UPISAN
        )

    # Faktura je sad kompletna. Ako je kalkulacija vec u ERP-u, ovo je trenutak
    # kad sme da se prihvati - dok je manjak stajao otvoren, cuvali smo
    # mogucnost odbijanja.
    dodatak = prihvati_ako_je_kompletna(doc_id, user.login) if upisana else ""
    return _back(povratak, "Manjak je zatvoren — kalkulacija sme da se knjiži." + dodatak)


@app.get("/dokument/{doc_id}/pdf")
def dokument_pdf(doc_id: int, user: Korisnik) -> Response:
    """Zvanicni PDF prikaz fakture sa SEF-a, za stampu.

    Bogdanu i drugima ponekad treba faktura na papiru. Uzima se SEF-ov prosireni
    prikaz (`purchase-invoice/pdf`), a ne nas ekran - na papiru treba da stoji
    ono sto je dobavljac poslao, ne nasa obrada.
    """
    from ..sef.client import SefClient

    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Nema tog dokumenta.")
        if not _sme_na_dokument(session, user, doc):
            raise HTTPException(403, "Taj dokument nije tvoj.")
        sef_id = doc.sef_invoice_id
        ime = (doc.document_number or str(doc_id)).replace("/", "-")

    try:
        with SefClient() as klijent:
            sadrzaj = klijent.purchase_pdf(sef_id)
    except Exception as exc:  # noqa: BLE001 - SEF ume da bude nedostupan
        log.warning("PDF za dokument %s nije preuzet: %s", doc_id, exc)
        return _back(f"/dokument/{doc_id}", f"SEF nije dao PDF: {exc}", False)

    # Pregledacu se nikad ne salje nesto sto nije PDF: on to prijavi kao
    # ostecen dokument, a covek ne vidi sta je zapravo stiglo.
    if sadrzaj[:5] != b"%PDF-":
        log.warning("SEF za dokument %s nije vratio PDF: %r", doc_id, sadrzaj[:200])
        return _back(f"/dokument/{doc_id}", "SEF još nije napravio PDF — probaj ponovo.", False)
    return Response(
        content=sadrzaj,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{quote(ime)}.pdf"'},
    )


@app.post("/dokument/{doc_id}/sumnja")
def odluci_o_sumnji(doc_id: int, user: Korisnik, odluka: str = Form(...)) -> RedirectResponse:
    """Covek presudjuje predlog „možda je već u ERP-u”.

    Predlog se nadje po istom dobavljacu, datumu i iznosu kad se broj racuna ne
    poklapa - najcesce zato sto je covek pogresio u kucanju (Bogdan je uneo
    `9999` gde faktura ima `999`). App ga nikad ne primenjuje sama: lazan
    pogodak bi fakturu tiho sklonio sa ekrana i niko je ne bi zaknjizio.

    Obe odluke ostavljaju trag - ko je i kada rekao jeste ili nije.
    """
    from ..models import utcnow
    from ..services.nacrti import RUCNO_U_ERPU

    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Nema tog dokumenta.")
        if not _sme_na_dokument(session, user, doc):
            raise HTTPException(403, "Taj dokument nije tvoj.")
        red = session.get(NacrtKalkulacije, doc_id)
        if red is None or not red.sumnja:
            return _back(f"/dokument/{doc_id}", "Za ovu fakturu nema predloga.", False)
        oznaka = str(red.sumnja.get("oznaka") or "")
        if odluka == "jeste":
            red.stanje = StanjeNacrta.UPISAN
            red.erp_broj = oznaka[:20]
            red.erp_napomena = f"potvrdio {user.login}: {red.sumnja.get('razlog') or ''}"[:250]
            red.upisao = RUCNO_U_ERPU
            red.upisano_u = utcnow()
            red.sumnja = None
            red.sumnja_odbio = None
            red.sumnja_odbijeno_u = None
            poruka = f"Zabeleženo — faktura je već u ERP-u kao {oznaka}."
        else:
            # Predlog se pamti kao odbijen da se ne vraca pri svakom osvezavanju.
            red.sumnja_odbio = user.login
            red.sumnja_odbijeno_u = utcnow()
            poruka = "Zabeleženo — predlog odbijen, faktura ostaje na ekranu."
        session.flush()
    return _back(f"/dokument/{doc_id}", poruka)


@app.post("/dokument/{doc_id}/upisi-kalkulaciju")
def upisi_kalkulaciju(doc_id: int, user: Korisnik) -> RedirectResponse:
    """Operater upisuje kalkulaciju bez potvrde prijema robe.

    Kod usluga (magacin 088) nema prijema robe - niko ne "primi" struju ili
    zakup, pa taj dokument nema poslovodju koji bi kliknuo. Za robu ovo ostaje
    izuzetak: redovan put je potvrda prijema u objektu.
    """
    from ..config import get_settings as _get
    from ..services.nacrti import upisi_u_erp

    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Nema tog dokumenta.")
        if not _sme_na_dokument(session, user, doc):
            raise HTTPException(403, "Taj dokument nije tvoj.")
        if doc.po_starom:
            return _back(f"/dokument/{doc_id}", "Ova faktura je stigla pre prelaska na novi način rada — kalkulaciju za nju pravi poslovođa u ERP-u, kao i do sada.", False)

    podesavanja = _get()
    if not podesavanja.erp_upis_kalkulacije:
        return _back(
            f"/dokument/{doc_id}",
            "Upis kalkulacija je isključen (ERP_UPIS_KALKULACIJE).",
            False,
        )
    _, poruka = upisi_u_erp(
        doc_id, korisnik=user.login, produkcija=podesavanja.erp_allow_production_write
    )
    return _back(f"/dokument/{doc_id}", poruka)


@app.get("/dokument/{doc_id}/ubl")
def document_ubl(doc_id: int, user: Korisnik) -> FileResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None or not doc.ubl_path or not Path(doc.ubl_path).exists():
            raise HTTPException(404, "UBL fajl nije dostupan.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")
        return FileResponse(
            doc.ubl_path,
            media_type="application/xml",
            filename=f"{doc.document_number or doc.sef_invoice_id}.xml",
        )


@app.post("/dokument/{doc_id}/slika")
async def dodaj_sliku(
    doc_id: int,
    user: Korisnik,
    slika: Annotated[UploadFile, File()],
    line_no: Annotated[str, Form()] = "",
    napomena: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Poslovodja salje sliku ambalaze sa telefona, uz dokument (i stavku)."""
    from ..services.slike import sacuvaj

    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")

    try:
        sacuvaj(
            doc_id,
            await slika.read(),
            mime=slika.content_type,
            naziv_fajla=slika.filename,
            korisnik=user.login,
            line_no=_broj(line_no) or None,
            napomena=napomena,
        )
    except ValueError as exc:
        return _back(f"/dokument/{doc_id}", str(exc), False)
    return _back(f"/dokument/{doc_id}", "Slika je sačuvana.")


@app.get("/slika/{slika_id}")
def prikazi_sliku(slika_id: int, user: Korisnik) -> FileResponse:
    with session_scope() as session:
        red = session.get(Slika, slika_id)
        if red is None or not Path(red.putanja).exists():
            raise HTTPException(404, "Slika nije dostupna.")
        doc = session.get(Document, red.document_id)
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")
        return FileResponse(red.putanja, filename=red.naziv_fajla or "slika.jpg")


@app.post("/slika/{slika_id}/obrisi")
def obrisi_sliku(
    slika_id: int, user: Operater, povratak: Annotated[str, Form()] = ""
) -> RedirectResponse:
    from ..services.slike import obrisi

    with session_scope() as session:
        red = session.get(Slika, slika_id)
        doc_id = red.document_id if red else 0
    obrisi(slika_id, korisnik=user.login)
    return _back(povratak or f"/dokument/{doc_id}", "Slika je obrisana.")


# --------------------------------------------------------------------------- #
# poslovodja
# --------------------------------------------------------------------------- #


EKRAN_POVRACAJ = "povracaj"
EKRAN_USLUGE = "usluge"
EKRAN_OBJEKAT = "objekat"


def _vrsta_ekrana(unit) -> str:
    """Koji ekran dobija covek vezan za tu jedinicu.

    Prati istu podelu kao kalkulacija: objekat i magacin robe rade sa robom,
    a sve ostalo (uprava) su ulazni racuni bez robe, magacin 088.

    Vazno: usluzne fakture se razvrstavaju na **OFFICE**, ne na MAG088 - 088 se
    dobija tek pri pravljenju kalkulacije. Mereno 25.09.: OFFICE ima 1.953
    dokumenta, MAG088 nijedan. Prva verzija je gledala samo sifru magacina, pa
    bi Bogdanov ekran bio prazan.
    """
    from ..models import UnitKind

    sifra = (unit.erp_code or unit.code or "").strip()
    if unit.kind in (UnitKind.RETAIL, UnitKind.WAREHOUSE):
        if sifra == MAGACIN_POVRACAJ:
            return EKRAN_POVRACAJ
        if sifra == MAGACIN_ULAZNI_RACUN:
            return EKRAN_USLUGE
        return EKRAN_OBJEKAT
    return EKRAN_USLUGE


def _samo_novi(query):
    """Izbacuje dokumente od pre prelaska iz upita.

    `Document.po_starom` je svojstvo u Pajtonu, pa se ne moze filtrirati u SQL-u
    direktno - ali pocuvava se na istom `created_at`, pa se granica prevede u
    uslov. Brojaci moraju da se slazu sa onim sto red stvarno nudi: pre ovoga je
    Bogdanu pisalo "8 spremno za upis" a svaki red je govorio "uradi po starom".
    """
    from datetime import datetime, time as _time

    granica = get_settings().novi_tok_od
    if granica is None:
        return query
    return query.where(Document.created_at >= datetime.combine(granica, _time.min))


def _sme_na_dokument(session, user, doc) -> bool:
    """Operater sme na svaki dokument, covek iz jedinice samo na svoje.

    U objektima dokument stize tek kad ga operater prosledi, pa se trazi i
    `forwarded_at`. Kod usluga (088) i povrata (010) prosledjivanja nema -
    dokument stigne i tu se zavrsi. Zato taj uslov vazi samo za objekte.

    Mereno 28.09. na produkciji: stranica dokumenta je trazila `forwarded_at`, a
    dugme za upis nije. Bogdan je kalkulaciju upisao (088/03119), pa dobio 403
    na istu fakturu - i tako na svaku svoju, jer nijedna nije prosledjena.
    """
    if user.is_operator:
        return True
    jedinica = _unit_for(session, user)
    if jedinica is None or doc.business_unit_id != jedinica.id:
        return False
    if _vrsta_ekrana(jedinica) == EKRAN_OBJEKAT:
        return doc.forwarded_at is not None
    return True


def _broj_zaustavljenih(session) -> int:
    """Koliko kalkulacija stoji zaustavljeno - za oznaku u navigaciji.

    Stoje pri dnu ekrana „Sifre“, ispod spiska stavki koje cekaju sifru, pa se
    lako propuste; broj u navigaciji je jedino sto se vidi odmah. A dok stoje,
    roba je neuknjizena.
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(NacrtKalkulacije)
            .where(NacrtKalkulacije.stanje == StanjeNacrta.PROBLEM)
        )
        or 0
    )


def _broj_popusta(session) -> int:
    """Koliko popusta ceka prenos - za oznaku u navigaciji."""
    from ..models import PopustZaPrenos

    return (
        session.scalar(
            select(func.count())
            .select_from(PopustZaPrenos)
            .where(PopustZaPrenos.preneto_at.is_(None))
        )
        or 0
    )


@app.get("/popusti", response_class=HTMLResponse)
def popusti(request: Request, user: Korisnik):
    """Odobrenja na deo vrednosti uređaja — popust koji ide dalje u objekat.

    Vidi ih Ilijana (jedinica povraćaja) i operateri. Poslovođa ne — njemu se
    popust prenosi, ne on njega prenosi.
    """
    from ..models import PopustZaPrenos

    with session_scope() as session:
        if not user.is_operator:
            jedinica = _unit_for(session, user)
            if jedinica is None or (jedinica.erp_code or jedinica.code).strip() != MAGACIN_POVRACAJ:
                raise HTTPException(403, "Popuste vidi magacin povraćaja.")

        upit = (
            select(PopustZaPrenos)
            .options(selectinload(PopustZaPrenos.document))
            .order_by(PopustZaPrenos.otkriveno_u.desc())
        )
        svi = list(session.scalars(upit))
        otvoreni = [p for p in svi if p.otvoren]
        preneti = [p for p in svi if not p.otvoren][:30]
        return templates.TemplateResponse(
            request,
            "popusti.html",
            {"user": user, "otvoreni": otvoreni, "preneti": preneti,
             "popusta": len(otvoreni)},
        )


@app.post("/popusti/{doc_id}/preneto")
def popust_preneto(
    doc_id: int,
    user: Korisnik,
    objekat: str = Form(...),
    napomena: str = Form(""),
) -> RedirectResponse:
    """Ilijana beleži kome je popust prenet — zapis prestaje da bude otvoren."""
    from ..models import PopustZaPrenos, utcnow as _sada

    with session_scope() as session:
        red = session.get(PopustZaPrenos, doc_id)
        if red is None:
            return _back("/popusti", "Taj popust nije zabeležen.", False)
        red.preneto_u_objekat = objekat.strip()[:20]
        red.napomena = (napomena or "").strip()[:500] or None
        red.preneto_at = _sada()
        red.preneto_by = user.login
    return _back("/popusti", f"Zabeleženo — popust prenet u objekat {objekat.strip()}.")


@app.get("/pomoc", response_class=HTMLResponse)
def pomoc(request: Request, user: Korisnik) -> HTMLResponse:
    """Pomoć na ekranu, po ulozi — aktuelno uputstvo za onoga ko gleda.

    Postoji zato što niko ne drži odštampano uputstvo uz sebe. Ista podela kao
    kod ekrana: uprava dobija usluge, magacin povrata svoje, objekat svoje.

    Sadržaj mora da se menja zajedno sa svakom izmenom u načinu rada — zastarela
    pomoć je gora nego nikakva (Bane, 26.09.).
    """
    naslovi = {
        "operater": "kancelarija",
        "poslovodja": "objekat",
        "povracaj": "povrati i odobrenja",
        "usluge": "ulazni računi",
    }
    with session_scope() as session:
        if user.is_operator:
            uloga = "operater"
        else:
            unit = _unit_for(session, user)
            vrsta = _vrsta_ekrana(unit) if unit is not None else None
            uloga = {EKRAN_POVRACAJ: "povracaj", EKRAN_USLUGE: "usluge"}.get(
                vrsta, "poslovodja"
            )
        return templates.TemplateResponse(
            request,
            "pomoc.html",
            {
                "user": user,
                "uloga": uloga,
                "naslov_uloge": naslovi[uloga],
                "datum_prelaska": get_settings().novi_tok_od,
                "popusta": _broj_popusta(session) if uloga != "poslovodja" else 0,
            },
        )


@app.get("/moja-jedinica", response_class=HTMLResponse)
def my_unit(request: Request, user: Korisnik, prikaz: str = "novo"):
    with session_scope() as session:
        unit = _unit_for(session, user)
        if unit is None:
            if user.is_operator:
                return RedirectResponse("/red", status_code=303)
            raise HTTPException(
                403, f"Za šifru objekta {user.unit_code} nema poslovne jedinice u šifarniku."
            )

        vrsta = _vrsta_ekrana(unit)
        if vrsta in (EKRAN_POVRACAJ, EKRAN_USLUGE):
            # Ovde dokument ne prolazi kroz "prosledjivanje" - stigne i tu se
            # zavrsi. Zato se ne filtrira po forwarded_at, kao kod objekata.
            redovi = list(
                session.scalars(
                    select(Document)
                    .options(selectinload(Document.business_unit))
                    .where(
                        Document.business_unit_id == unit.id,
                        Document.archived == (prikaz == "arhiva"),
                    )
                    .order_by(Document.issue_date.desc(), Document.id.desc())
                    .limit(200)
                )
            )
            nacrti = {
                n.document_id: n
                for n in session.scalars(
                    select(NacrtKalkulacije).where(
                        NacrtKalkulacije.document_id.in_([d.id for d in redovi] or [0])
                    )
                )
            }
            po_starom = {d.id for d in redovi if d.po_starom}
            return templates.TemplateResponse(
                request,
                "jedinica_povracaj.html" if vrsta == EKRAN_POVRACAJ else "jedinica_usluge.html",
                {
                    "user": user,
                    "unit": unit,
                    "documents": redovi,
                    "nacrti": nacrti,
                    "prikaz": prikaz,
                    "popusta": _broj_popusta(session) if vrsta == EKRAN_POVRACAJ else 0,
                    # Fakture od pre prelaska se ne broje: red na ekranu za njih
                    # ne nudi dugme nego pise "uradi po starom", pa bi brojac
                    # obecavao posao koji ne postoji (Bogdan, 27.09.: "kaze 8
                    # spremno a svi su obelezeni po starom").
                    "ceka_sifru": sum(
                        1 for n in nacrti.values()
                        if n.stanje is StanjeNacrta.CEKA_SIFRU
                        and n.document_id not in po_starom
                    ),
                    "spremno": sum(
                        1 for n in nacrti.values()
                        if n.stanje is StanjeNacrta.SPREMAN
                        and n.document_id not in po_starom
                    ),
                },
            )

        # Spisak "sta treba slikati": stavke sa njegovih faktura kojima fali nasa
        # sifra. Poslovodja time ne mora da otvara fakturu po fakturu i gada sta
        # je novo - vidi artikle, i odmah pored dugme za kameru.
        za_slikanje = []
        if prikaz == "slikanje":
            from ..services.slike import za_artikal

            nacrti = session.scalars(
                _samo_novi(
                    select(NacrtKalkulacije)
                    .options(selectinload(NacrtKalkulacije.document))
                    .join(Document, Document.id == NacrtKalkulacije.document_id)
                    .where(
                        Document.business_unit_id == unit.id,
                        Document.forwarded_at.isnot(None),
                        Document.archived == False,  # noqa: E712
                        NacrtKalkulacije.stanje == StanjeNacrta.CEKA_SIFRU,
                    )
                ).order_by(NacrtKalkulacije.document_id.desc())
            )
            for nacrt in nacrti:
                for stavka in nacrt.nemapirane or []:
                    slike_te = za_artikal(
                        session,
                        ean=stavka.get("ean"),
                        sifra_dobavljaca=stavka.get("sifra_dobavljaca"),
                        supplier_vat=nacrt.document.supplier_vat,
                    )
                    za_slikanje.append(
                        {"nacrt": nacrt, "stavka": stavka, "slike": slike_te}
                    )

        # Arhivirano ne stoji poslovodji na ekranu. Bez ovoga sutrasnje ciscenje
        # koje rade Nina i Dusan ne bi ocistilo objekte, pa bi Dane i dalje gledao
        # probne potvrde iz testiranja (nadjeno 27.09., vece pred prelazak).
        query = select(Document).where(
            Document.business_unit_id == unit.id,
            Document.forwarded_at.isnot(None),
            Document.archived == (prikaz == "arhiva"),
        )
        if prikaz == "novo":
            query = _samo_novi(query.where(Document.received_at.is_(None)))
        elif prikaz == "manjak":
            # Manjak koji jos stoji otvoren - poslovodja ceka ostatak i ne knjizi.
            query = query.join(
                PrijavaManjka, PrijavaManjka.document_id == Document.id
            ).where(PrijavaManjka.zatvoreno_u.is_(None))
        rows = (
            []
            if prikaz == "slikanje"
            else list(session.scalars(query.order_by(Document.forwarded_at.desc()).limit(200)))
        )
        manjci = {
            m.document_id: m
            for m in session.scalars(
                select(PrijavaManjka).where(
                    PrijavaManjka.document_id.in_([d.id for d in rows] or [0])
                )
            )
        }
        otvorenih_manjaka = session.scalar(
            select(func.count())
            .select_from(PrijavaManjka)
            .join(Document, Document.id == PrijavaManjka.document_id)
            .where(
                Document.business_unit_id == unit.id,
                PrijavaManjka.zatvoreno_u.is_(None),
            )
        )
        # koliko stavki ceka sliku - stoji na kartici i kad nije otvorena
        koliko_za_slikanje = session.scalar(
            _samo_novi(
                select(func.count())
                .select_from(NacrtKalkulacije)
                .join(Document, Document.id == NacrtKalkulacije.document_id)
                .where(
                    Document.business_unit_id == unit.id,
                    Document.forwarded_at.isnot(None),
                    Document.archived == False,  # noqa: E712
                    NacrtKalkulacije.stanje == StanjeNacrta.CEKA_SIFRU,
                )
            )
        )
        ceka = session.scalar(
            _samo_novi(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.business_unit_id == unit.id,
                    Document.forwarded_at.isnot(None),
                    Document.received_at.is_(None),
                    Document.archived == False,  # noqa: E712
                )
            )
        )
        return templates.TemplateResponse(
            request,
            "unit.html",
            {
                "popusta": (
                    _broj_popusta(session)
                    if (unit.erp_code or unit.code).strip() == MAGACIN_POVRACAJ
                    else 0
                ),
                "unit": unit,
                "documents": rows,
                "ceka": ceka,
                "prikaz": prikaz,
                "za_slikanje": za_slikanje,
                "koliko_za_slikanje": koliko_za_slikanje,
                "manjci": manjci,
                "otvorenih_manjaka": otvorenih_manjaka,
                "user": user,
            },
        )


@app.post("/dokument/{doc_id}/prijem")
def confirm_receipt(
    doc_id: int,
    user: Korisnik,
    napomena: Annotated[str, Form()] = "",
) -> RedirectResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")
        if doc.po_starom:
            return _back(
                f"/dokument/{doc_id}" if user.is_operator else "/moja-jedinica",
                "Ova faktura je stigla pre prelaska na novi način rada — kalkulaciju za nju pravi poslovođa u ERP-u, kao i do sada.",
                False,
            )
    rezultat = Workflow().confirm_receipt(doc_id, actor=user.login, note=napomena)
    povratak = f"/dokument/{doc_id}" if user.is_operator else "/moja-jedinica"
    return _back(povratak, rezultat.message, rezultat.ok)


# --------------------------------------------------------------------------- #
# sifarnici
# --------------------------------------------------------------------------- #


@app.get("/tacnost", response_class=HTMLResponse)
def accuracy_page(request: Request, user: Operater) -> HTMLResponse:
    """Koliko razvrstavanje pogadja i koja pravila grese."""
    from ..routing.stats import sazetak

    with session_scope() as session:
        podaci = sazetak(session)
        return templates.TemplateResponse(
            request, "accuracy.html", {**podaci, "user": user}
        )


@app.get("/pj", response_class=HTMLResponse)
def units_page(request: Request, user: Operater) -> HTMLResponse:
    with session_scope() as session:
        units = list(
            session.scalars(select(BusinessUnit).order_by(BusinessUnit.kind, BusinessUnit.code))
        )
        counts = dict(
            session.execute(
                select(Document.business_unit_id, func.count()).group_by(Document.business_unit_id)
            ).all()
        )
        return templates.TemplateResponse(
            request,
            "units.html",
            {"units": units, "counts": counts, "kinds": list(UnitKind), "user": user},
        )


@app.post("/pj")
def unit_save(
    user: Operater,
    code: Annotated[str, Form()],
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()] = UnitKind.RETAIL.value,
    erp_code: Annotated[str, Form()] = "",
    logins: Annotated[str, Form()] = "",
    address: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    postal_code: Annotated[str, Form()] = "",
    emails: Annotated[str, Form()] = "",
    phones: Annotated[str, Form()] = "",
    unit_id: Annotated[int, Form()] = 0,
    active: Annotated[str, Form()] = "",
    routable: Annotated[str, Form()] = "",
) -> RedirectResponse:
    with session_scope() as session:
        unit = session.get(BusinessUnit, unit_id) if unit_id else None
        if unit is None:
            unit = BusinessUnit(code=code.strip(), name=name.strip())
            session.add(unit)
        unit.code = code.strip()
        unit.name = name.strip()
        unit.kind = UnitKind(kind)
        unit.erp_code = erp_code.strip() or None
        unit.logins = logins.strip() or None
        unit.address = address.strip() or None
        unit.city = city.strip() or None
        unit.postal_code = postal_code.strip() or None
        unit.emails = emails.strip() or None
        unit.phones = phones.strip() or None
        unit.active = bool(active)
        unit.routable = bool(routable)
    return _back("/pj", "Poslovna jedinica je sačuvana.")


# --------------------------------------------------------------------------- #
# sifre artikala (faza 2): isti ekran operatera, drugi tab
# --------------------------------------------------------------------------- #


@app.get("/sifre", response_class=HTMLResponse)
def sifre_page(request: Request, user: Operater) -> HTMLResponse:
    """Kalkulacije kojima fali nasa sifra artikla.

    Nina i Dule se menjaju, pa je ovo tab u istom ekranu, ne poseban nalog.
    """
    with session_scope() as session:
        nacrti = list(
            session.scalars(
                select(NacrtKalkulacije)
                .options(
                    selectinload(NacrtKalkulacije.document).selectinload(
                        Document.business_unit
                    )
                )
                .where(NacrtKalkulacije.stanje == StanjeNacrta.CEKA_SIFRU)
                .order_by(NacrtKalkulacije.document_id.desc())
            )
        )
        # Nacrti zaustavljeni iz drugog razloga (dobavljac van sifarnika, sifra
        # bez artikla) moraju da se vide na istom mestu - inace stoje a niko ne
        # zna za njih.
        zapeli = list(
            session.scalars(
                select(NacrtKalkulacije)
                .options(selectinload(NacrtKalkulacije.document))
                .where(NacrtKalkulacije.stanje == StanjeNacrta.PROBLEM)
                .order_by(NacrtKalkulacije.document_id.desc())
            )
        )
        broj = {
            stanje.value: session.scalar(
                select(func.count())
                .select_from(NacrtKalkulacije)
                .where(NacrtKalkulacije.stanje == stanje)
            )
            for stanje in StanjeNacrta
        }
        racunato = session.scalar(select(func.max(NacrtKalkulacije.racunato_u)))
        # Slike se traze PO ARTIKLU, ne po dokumentu: isti artikal stize na vise
        # objekata, pa je slika najcesce sa tudje fakture. Dok se gledalo po
        # dokumentu, Dusan nije video da je artikal vec neko slikao.
        from ..services.slike import za_artikal

        slike_po_stavci: dict[tuple[int, int], list] = {}
        for nacrt in nacrti:
            for stavka in nacrt.nemapirane or []:
                nadjene = za_artikal(
                    session,
                    ean=stavka.get("ean"),
                    sifra_dobavljaca=stavka.get("sifra_dobavljaca"),
                    supplier_vat=nacrt.document.supplier_vat,
                )
                if nadjene:
                    slike_po_stavci[(nacrt.document_id, stavka.get("line_no"))] = nadjene
    return templates.TemplateResponse(
        request,
        "sifre.html",
        {
            "nacrti": nacrti,
            "zapeli": zapeli,
            "zaustavljenih": len(zapeli),
            "slike": slike_po_stavci,
            "broj": broj,
            "racunato": racunato,
            "user": user,
        },
    )


@app.post("/pregledano")
def oznaci_pregledano(
    user: Operater,
    dokumenti: Annotated[list[str], Form()] = None,
    povratak: Annotated[str, Form()] = "",
) -> RedirectResponse:
    rezultat = Workflow().oznaci_pregledano(_ids(dokumenti or []), actor=user.login)
    return _back(povratak or "/red?prikaz=nepregledano", rezultat.message, rezultat.ok)


@app.post("/sifre/povezi")
def sifre_povezi(
    user: Korisnik,
    document_id: int = Form(...),
    line_no: int = Form(...),
    erp_item_code: str = Form(...),
    ne_pamti: str = Form(""),
    zapamti: str = Form(""),
) -> RedirectResponse:
    from ..services.nacrti import povezi_sifru

    # Ilijana povezuje sifre na svojim odobrenjima, pa ekran vise nije samo
    # operaterski. Ko nije operater sme samo dokumente svoje jedinice.
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None:
            raise HTTPException(404, "Nema tog dokumenta.")
        if not _sme_na_dokument(session, user, doc):
            raise HTTPException(403, "Taj dokument nije tvoj.")
        vrsta = doc.document_type
    nazad = "/sifre" if user.is_operator else "/moja-jedinica"

    # Na knjiznom odobrenju sifra dobavljaca nije sifra artikla nego opsta
    # oznaka: EWE salje "USL00027" na 109 odobrenja, Eurotehna "Porez 20%" na
    # 107, Comtrade "R0001" na 90. Da se takva veza zapamti, svako sledece
    # odobrenje tog dobavljaca bi tiho dobilo artikal od proslog puta. Zato se
    # na odobrenjima pamti samo kad covek izricito kaze.
    if vrsta is DocumentType.CREDIT_NOTE:
        pamti = bool(zapamti)
    else:
        pamti = not ne_pamti

    try:
        povezi_sifru(
            document_id, line_no, erp_item_code,
            korisnik=user.login, pamti=pamti,
        )
    except Exception as exc:  # noqa: BLE001 - poruka ide coveku, ne u log
        return _back(nazad, str(exc), False)
    sifra = erp_item_code.strip()
    if pamti:
        return _back(nazad, f"Šifra {sifra} je povezana i zapamćena za ovog dobavljača.")
    # Na odobrenju se bez cekiranja veza NE pamti. Poruka je ranije tvrdila
    # suprotno, pa je izgledalo kao da aplikacija ne reaguje.
    return _back(nazad, f"Šifra {sifra} je povezana samo na ovoj fakturi.")


@app.get("/sifre/predlog/{document_id}/{line_no}", response_class=HTMLResponse)
def sifre_predlog(
    request: Request, document_id: int, line_no: int, user: Operater
) -> HTMLResponse:
    """Sve sto se sa fakture zna o novom artiklu, spremno za otvaranje sifre."""
    from ..erp.artikal import kao_sql, predlozi

    predlog = predlozi(document_id, line_no)
    sql = kao_sql(predlog) if predlog.sifra else None
    with session_scope() as session:
        doc = session.get(Document, document_id)
        broj = doc.document_number if doc else ""
        dobavljac = doc.supplier_name if doc else ""
    return templates.TemplateResponse(
        request,
        "predlog_artikla.html",
        {
            "predlog": predlog,
            "sql": sql,
            "broj": broj,
            "dobavljac": dobavljac,
            "user": user,
        },
    )


@app.post("/sifre/nije-artikal")
def sifre_nije_artikal(
    user: Operater,
    document_id: int = Form(...),
    line_no: int = Form(...),
    vrati: str = Form(""),
) -> RedirectResponse:
    """Stavka koja nikad nece imati nasu sifru izlazi sa spiska (i iz kalkulacije)."""
    from ..services.nacrti import oznaci_nije_artikal

    try:
        oznaci_nije_artikal(
            document_id, line_no, korisnik=user.login, vrati=bool(vrati)
        )
    except Exception as exc:  # noqa: BLE001 - poruka ide coveku
        return _back("/sifre", str(exc), False)
    if vrati:
        return _back(f"/dokument/{document_id}", "Stavka je vraćena na spisak.")
    return _back("/sifre", "Stavka je označena — ne traži se šifra za nju.")


@app.post("/sifre/osvezi")
def sifre_osvezi(user: Operater, background: BackgroundTasks) -> RedirectResponse:
    from ..services.nacrti import osvezi_sve

    # Racunanje ide po ERP-u za svaki dokument, pa se ne ceka na odgovor.
    background.add_task(osvezi_sve)
    return _back("/sifre", "Preračunavanje je pokrenuto — osveži stranu za koji trenutak.")


@app.get("/pravila", response_class=HTMLResponse)
def rules_page(request: Request, user: Operater) -> HTMLResponse:
    with session_scope() as session:
        rules = list(
            session.scalars(
                select(RoutingRule)
                .options(selectinload(RoutingRule.business_unit))
                .order_by(RoutingRule.priority, RoutingRule.id)
            )
        )
        units = list(session.scalars(select(BusinessUnit).order_by(BusinessUnit.code)))
        return templates.TemplateResponse(
            request,
            "rules.html",
            {
                "rules": rules,
                "units": units,
                "fields": list(MatchField),
                "ops": list(MatchOp),
                "types": list(DocumentType),
                "user": user,
            },
        )


@app.post("/pravila")
def rule_save(
    user: Operater,
    field: Annotated[str, Form()],
    op: Annotated[str, Form()],
    pattern: Annotated[str, Form()],
    business_unit_id: Annotated[int, Form()],
    priority: Annotated[int, Form()] = 100,
    supplier_vat: Annotated[str, Form()] = "",
    document_type: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    rule_id: Annotated[int, Form()] = 0,
) -> RedirectResponse:
    with session_scope() as session:
        rule = session.get(RoutingRule, rule_id) if rule_id else None
        if rule is None:
            rule = RoutingRule(
                field=MatchField(field),
                op=MatchOp(op),
                pattern=pattern,
                business_unit_id=business_unit_id,
            )
            session.add(rule)
        rule.field = MatchField(field)
        rule.op = MatchOp(op)
        rule.pattern = pattern.strip()
        rule.business_unit_id = business_unit_id
        rule.priority = priority
        rule.supplier_vat = supplier_vat.strip() or None
        rule.document_type = DocumentType(document_type) if document_type else None
        rule.comment = comment.strip() or None
    return _back("/pravila", "Pravilo je sačuvano.")


@app.post("/pravila/{rule_id}/obrisi")
def rule_delete(rule_id: int, user: Operater) -> RedirectResponse:
    with session_scope() as session:
        rule = session.get(RoutingRule, rule_id)
        if rule is not None:
            session.delete(rule)
    return _back("/pravila", "Pravilo je obrisano.")


@app.post("/pravila/primeni")
def rules_apply(user: Operater, background: BackgroundTasks) -> RedirectResponse:
    def job() -> None:
        service = IngestService()
        with session_scope() as session:
            ids = list(
                session.scalars(
                    select(Document.id).where(
                        Document.business_unit_id.is_(None), Document.archived == False
                    )
                )
            )
        for doc_id in ids:
            try:
                service.reroute(doc_id)
            except Exception:  # noqa: BLE001
                log.exception("Ponovno razvrstavanje dokumenta %s nije uspelo", doc_id)

    background.add_task(job)
    return _back("/pravila", "Ponovno razvrstavanje je pokrenuto.")


# --------------------------------------------------------------------------- #
# sinhronizacija i API
# --------------------------------------------------------------------------- #


@app.post("/sync")
def run_sync(
    user: Operater,
    background: BackgroundTasks,
    od: Annotated[str, Form()] = "",
    do: Annotated[str, Form()] = "",
) -> RedirectResponse:
    date_from = date.fromisoformat(od) if od else None
    date_to = date.fromisoformat(do) if do else None
    background.add_task(lambda: IngestService().sync(date_from, date_to))
    return _back("/red", "Preuzimanje sa SEF-a je pokrenuto.")


@app.get("/api/dokumenti")
def api_documents(
    user: Korisnik,
    stanje: str | None = None,
    pj: str | None = None,
    limit: int = Query(200, le=1000),
) -> JSONResponse:
    with session_scope() as session:
        query = select(Document).order_by(Document.id.desc()).limit(limit)
        if stanje:
            query = query.where(Document.state == ProcessState(stanje))
        if pj:
            query = query.join(BusinessUnit).where(BusinessUnit.code == pj)
        if not user.is_operator:
            moja = _unit_for(session, user)
            query = query.where(Document.business_unit_id == (moja.id if moja else -1))
        docs = list(session.scalars(query))
        return JSONResponse(
            [
                {
                    "id": d.id,
                    "sef_invoice_id": d.sef_invoice_id,
                    "broj": d.document_number,
                    "tip": d.document_type.value,
                    "dobavljac": d.supplier_name,
                    "pib": d.supplier_vat,
                    "iznos": d.amount,
                    "valuta": d.currency,
                    "datum_prometa": d.delivery_date.isoformat() if d.delivery_date else None,
                    "poslovna_jedinica": d.business_unit.code if d.business_unit else None,
                    "sef_status": d.sef_status.value,
                    "prosledjeno": d.forwarded_at.isoformat() if d.forwarded_at else None,
                    "prijem_potvrdjen": d.received_at.isoformat() if d.received_at else None,
                    "stavki": len(d.lines),
                }
                for d in docs
            ]
        )


@app.get("/health")
def health() -> dict:
    """Stanje panela i bazena veza.

    Bazen ka ERP-u se napunio prvog radnog dana i IIS je vracao 502, a iz loga
    se nije videlo da li veze cure ili ih je premalo. Ovde se to vidi bez
    uzimanja ijedne veze: `checkedout` je koliko ih je trenutno u upotrebi.
    Ako taj broj raste i ne vraca se na nulu dok niko ne radi - veze cure.
    """
    from ..db import get_engine
    from ..erp.connection import get_erp_engine

    def stanje(engine) -> dict:
        bazen = engine.pool
        try:
            return {
                "u_upotrebi": bazen.checkedout(),
                "slobodno": bazen.checkedin(),
                "preko_mere": bazen.overflow(),
            }
        except AttributeError:
            return {}

    from ..db import ko_drzi_veze

    podaci: dict = {"status": "ok", "panel": stanje(get_engine())}
    # Ko drzi veze koje se ne vracaju. Ne uzima se nijedna nova veza - ovo mora
    # da radi bas kad je bazen pun i kad ljudi ne mogu da se prijave.
    drze = ko_drzi_veze()
    if drze:
        podaci["drze_vezu"] = drze
    try:
        podaci["erp"] = stanje(get_erp_engine())
    except Exception as exc:  # noqa: BLE001 - zdravlje se ne rusi zbog ERP-a
        podaci["erp"] = {"greska": str(exc)[:80]}
    return podaci
