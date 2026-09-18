"""Stanje kalkulacija koje aplikacija sklapa: sta je spremno, a sta ceka coveka.

Generator (`erp.kalkulacija.pripremi`) svaki put iznova sklopi nacrt iz dokumenta
i ERP-a. To je desetak upita po dokumentu - predugo za ekran koji neko osvezava.
Zato se ovde ishod racunanja upisuje u `nacrt_kalkulacije`, pa ekran cita bazu.

Ono sto covek uradi ne cuva se u nacrtu nego u `item_mapping`: kad Dule kaze koja
je nasa sifra, ta veza vazi i za sve buduce fakture istog dobavljaca.
"""

from __future__ import annotations

import logging

from sqlalchemy import and_, or_, select

from ..db import session_scope
from ..erp.kalkulacija import pripremi
from ..models import (
    AuditLog,
    Document,
    DocumentLine,
    ItemMapping,
    NacrtKalkulacije,
    ProcessState,
    StanjeNacrta,
    utcnow,
)

log = logging.getLogger(__name__)


def _stanje(nacrt) -> StanjeNacrta:
    if nacrt.problemi:
        return StanjeNacrta.PROBLEM
    if nacrt.nemapirane:
        return StanjeNacrta.CEKA_SIFRU
    return StanjeNacrta.SPREMAN


def osvezi(document_id: int, *, korisnik: str = "app", engine=None) -> NacrtKalkulacije | None:
    """Ponovo sklopi nacrt za jedan dokument i upise ishod. Ne dira ERP upisom."""
    nacrt = pripremi(document_id, korisnik=korisnik, engine=engine)

    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        if red is None:
            red = NacrtKalkulacije(document_id=document_id)
            session.add(red)
        if red.stanje is StanjeNacrta.UPISAN:
            # Vec je u ERP-u - ponovno racunanje ne sme da vrati stanje unazad.
            return red
        red.stanje = _stanje(nacrt)
        red.magacin = nacrt.magacin or None
        red.tabela = nacrt.tabela_zaglavlja or None
        red.iznos = nacrt.zaglavlje.get("IZNOS_ZA_NAP")
        red.problemi = list(nacrt.problemi) or None
        red.nemapirane = list(nacrt.nemapirane) or None
        red.racunato_u = utcnow()
        session.flush()
        session.expunge(red)
        return red


def osvezi_sve(*, samo_razvrstane: bool = True, limit: int | None = None, engine=None) -> dict:
    """Prolazi kroz dokumente i osvezava nacrte. Vraca brojace po stanju."""
    with session_scope() as session:
        upit = select(Document.id).where(Document.archived == False)  # noqa: E712
        if samo_razvrstane:
            upit = upit.where(Document.business_unit_id.is_not(None))
        upit = upit.where(Document.state != ProcessState.ERROR).order_by(Document.id.desc())
        if limit:
            upit = upit.limit(limit)
        ids = list(session.scalars(upit))

    brojac = {s.value: 0 for s in StanjeNacrta}
    brojac["greska"] = 0
    for doc_id in ids:
        try:
            red = osvezi(doc_id, engine=engine)
        except Exception as exc:  # noqa: BLE001 - jedan dokument ne sme da obori posao
            brojac["greska"] += 1
            log.warning("Nacrt za #%s nije napravljen: %s", doc_id, exc)
            continue
        if red is not None:
            brojac[red.stanje.value] += 1
    return brojac


def oznaci_nije_artikal(
    document_id: int, line_no: int, *, korisnik: str, vrati: bool = False, engine=None
) -> NacrtKalkulacije | None:
    """Stavka koja nikad nece imati nasu sifru (odobrenje po saglasnosti, troskovi).

    Ne otvara se sifra i ne ceka se - stavka prosto izlazi iz kalkulacije. `vrati`
    ponistava oznaku, ako je neko pogresio.
    """
    with session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_id == document_id, DocumentLine.line_no == line_no
            )
        )
        if linija is None:
            raise ValueError(f"Stavka {line_no} ne postoji na dokumentu #{document_id}.")
        linija.nije_artikal = not vrati
        linija.nije_artikal_by = None if vrati else korisnik

    return osvezi(document_id, korisnik=korisnik, engine=engine)


def povezi_sifru(
    document_id: int,
    line_no: int,
    erp_item_code: str,
    *,
    korisnik: str,
    pamti: bool = True,
    engine=None,
) -> NacrtKalkulacije | None:
    """Covek je rekao koja je nasa sifra za jednu stavku.

    Veza se pamti u `item_mapping` (po dobavljacu i njegovoj sifri, pa i po
    barkodu kad ga ima), dakle vazi i za sve naredne fakture. Zato je ovo
    najvredniji klik u celom toku - ne resava jedan dokument nego sve buduce.

    Ali pamti se samo ono sto je jednoznacno. Neki dobavljaci jednom sifrom
    pokrivaju vise nasih artikala - CSM salje "ponye 20 litium" bez oznake boje,
    a kod nas je svaka boja svoja sifra. Kad se ista dobavljaceva sifra jednom
    veze za jedan artikal, a drugi put za drugi, veza se **gasi** umesto da se
    prepise: sledeci put mora opet da odluci covek. Prepisivanje bi znacilo da
    naredna faktura tiho dobije boju od proslog puta.

    `pamti=False` je za slucaj kad covek unapred zna da je sifra dvosmislena.
    """
    sifra = (erp_item_code or "").strip()
    if not sifra:
        raise ValueError("Šifra artikla je prazna.")

    with session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_id == document_id, DocumentLine.line_no == line_no
            )
        )
        if linija is None:
            raise ValueError(f"Stavka {line_no} ne postoji na dokumentu #{document_id}.")
        doc = session.get(Document, document_id)
        pib = (doc.supplier_vat or "").strip()
        if not pib:
            raise ValueError("Dokument nema PIB dobavljača — veza se ne bi imala za šta zakačiti.")

        linija.erp_item_code = sifra
        linija.erp_mapping_source = "rucno"

        postoji = session.scalar(
            select(ItemMapping).where(
                ItemMapping.supplier_vat == pib,
                ItemMapping.supplier_item_id == (linija.sellers_item_id or None),
                ItemMapping.barcode == (linija.standard_item_id or None),
            )
        )
        if postoji is not None and postoji.erp_item_code.strip() != sifra:
            # Ista dobavljaceva sifra, drugi artikal - dakle ne znaci jedno.
            postoji.active = False
            postoji.source = "kolebljivo"
            postoji.misses += 1
            session.add(
                AuditLog(
                    actor=korisnik,
                    action="mapiranje.kolebljivo",
                    document_id=document_id,
                    detail=f"{pib}/{linija.sellers_item_id or '-'}: "
                           f"{postoji.erp_item_code} -> {sifra}; veza ugašena",
                )
            )
        elif postoji is not None:
            postoji.confirmed_at = utcnow()
            postoji.confirmed_by = korisnik
            postoji.hits += 1
        elif pamti:
            session.add(
                ItemMapping(
                    supplier_vat=pib,
                    supplier_item_id=linija.sellers_item_id or None,
                    barcode=linija.standard_item_id or None,
                    supplier_item_name=(linija.name or "")[:500] or None,
                    erp_item_code=sifra,
                    source="rucno",
                    confirmed_at=utcnow(),
                    confirmed_by=korisnik,
                )
            )

    osvezeni = osvezi(document_id, korisnik=korisnik, engine=engine)

    # Isti artikal stize na vise objekata. Kad se sifra jednom kaze, sve ostale
    # fakture koje ga cekaju moraju da prodju same - inace poslovodja u drugom
    # objektu i dalje gleda "ceka sifru" za nesto sto je vec reseno.
    for drugi in _cekaju_isti_artikal(document_id, line_no):
        try:
            osvezi(drugi, korisnik=korisnik, engine=engine)
        except Exception as exc:  # noqa: BLE001 - jedan dokument ne sme da obori ostale
            log.warning("Nacrt za #%s nije osvezen: %s", drugi, exc)
    return osvezeni


def _cekaju_isti_artikal(document_id: int, line_no: int) -> list[int]:
    """Dokumenti koji cekaju sifru za isti artikal (po barkodu ili sifri dobavljaca)."""
    with session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_id == document_id, DocumentLine.line_no == line_no
            )
        )
        if linija is None:
            return []
        doc = session.get(Document, document_id)
        uslovi = []
        if linija.standard_item_id:
            uslovi.append(DocumentLine.standard_item_id == linija.standard_item_id)
        if linija.sellers_item_id and doc is not None and doc.supplier_vat:
            uslovi.append(
                and_(
                    DocumentLine.sellers_item_id == linija.sellers_item_id,
                    Document.supplier_vat == doc.supplier_vat,
                )
            )
        if not uslovi:
            return []
        return list(
            session.scalars(
                select(Document.id)
                .join(DocumentLine, DocumentLine.document_id == Document.id)
                .join(NacrtKalkulacije, NacrtKalkulacije.document_id == Document.id)
                .where(
                    Document.id != document_id,
                    Document.archived == False,  # noqa: E712
                    NacrtKalkulacije.stanje == StanjeNacrta.CEKA_SIFRU,
                    or_(*uslovi),
                )
                .distinct()
            )
        )
