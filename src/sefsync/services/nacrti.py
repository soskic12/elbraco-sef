"""Stanje kalkulacija koje aplikacija sklapa: sta je spremno, a sta ceka coveka.

Generator (`erp.kalkulacija.pripremi`) svaki put iznova sklopi nacrt iz dokumenta
i ERP-a. To je desetak upita po dokumentu - predugo za ekran koji neko osvezava.
Zato se ovde ishod racunanja upisuje u `nacrt_kalkulacije`, pa ekran cita bazu.

Ono sto covek uradi ne cuva se u nacrtu nego u `item_mapping`: kad Dule kaze koja
je nasa sifra, ta veza vazi i za sve buduce fakture istog dobavljaca.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from sqlalchemy import and_, or_, select, text

from ..db import session_scope
from ..erp.connection import get_erp_engine
from ..erp.kalkulacija import pripremi
from ..models import (
    AuditLog,
    Document,
    DocumentLine,
    DocumentType,
    ItemMapping,  # noqa: F401 - koristi se u razlozi_cekanja
    NacrtKalkulacije,
    ProcessState,
    StanjeNacrta,
    utcnow,
)

log = logging.getLogger(__name__)


# Nazivi koji nisu artikal nego finansijska stavka - po njima se prepoznaje da
# nasa sifra nikad nece ni postojati ("Odobrenje po Saglasnosti broj 277102/2026").
_NIJE_ARTIKAL = re.compile(
    r"odobrenje po saglasnosti|saglasnost broj|troskov|troškov|transport|"
    r"naknad|provizij|rabat|bonus|popust|kamat|uslug",
    re.IGNORECASE,
)


def razlozi_cekanja(engine=None) -> dict:
    """Zasto stavke cekaju sifru, po uzroku - ne po utisku.

    Ekran pokazuje sta ceka coveka, ali ne i zasto. Bez ovoga se ne zna da li je
    posao stvarno rucni ili nesto ne radi kako treba.
    """
    eng = engine or get_erp_engine()
    with eng.connect() as conn:
        barkodovi = {
            r.b
            for r in conn.execute(
                text(
                    "SELECT DISTINCT RTRIM(ISNULL(BARCODE,'')) b FROM dbo.ARTIKLI "
                    "WHERE RTRIM(SIFRAMAG)='099' AND RTRIM(ISNULL(BARCODE,'')) <> ''"
                )
            )
        }

    uzroci: Counter = Counter()
    po_dobavljacu: Counter = Counter()
    primeri: dict[str, list[str]] = {}
    stavki = 0

    with session_scope() as session:
        nacrti = list(
            session.scalars(
                select(NacrtKalkulacije).where(
                    NacrtKalkulacije.stanje == StanjeNacrta.CEKA_SIFRU
                )
            )
        )
        dokumenti = {
            d.id: d
            for d in session.scalars(
                select(Document).where(
                    Document.id.in_([n.document_id for n in nacrti] or [0])
                )
            )
        }
        poznate = {
            (m.supplier_vat, m.supplier_item_id)
            for m in session.scalars(
                select(ItemMapping).where(ItemMapping.active == True)  # noqa: E712
            )
        }

        for nacrt in nacrti:
            doc = dokumenti.get(nacrt.document_id)
            for stavka in nacrt.nemapirane or []:
                stavki += 1
                naziv = stavka.get("naziv") or ""
                ean = (stavka.get("ean") or "").strip()
                sifra_dob = (stavka.get("sifra_dobavljaca") or "").strip()
                pib = doc.supplier_vat if doc is not None else None

                if _NIJE_ARTIKAL.search(naziv):
                    uzrok = "nije artikal (odobrenje, troškovi, naknada)"
                elif ean and ean in barkodovi:
                    uzrok = "barkod POSTOJI u šifarniku — treba da se reši sam"
                elif ean:
                    uzrok = "ima barkod, nema ga u šifarniku — nova šifra"
                elif sifra_dob and (pib, sifra_dob) in poznate:
                    uzrok = "šifra dobavljača je poznata — treba da se reši sama"
                elif sifra_dob:
                    uzrok = "nema barkod, šifra dobavljača nije viđena — čovek"
                else:
                    uzrok = "nema ni barkod ni šifru dobavljača — čovek"

                uzroci[uzrok] += 1
                if doc is not None:
                    po_dobavljacu[(doc.supplier_name or "?")[:34]] += 1
                primeri.setdefault(uzrok, [])
                if len(primeri[uzrok]) < 4:
                    primeri[uzrok].append(
                        f"{naziv[:44]} (EAN {ean or '-'}, šifra {sifra_dob or '-'})"
                    )

    return {
        "nacrta": len(nacrti),
        "stavki": stavki,
        "uzroci": uzroci,
        "primeri": primeri,
        "po_dobavljacu": po_dobavljacu,
    }


def _stanje(nacrt) -> StanjeNacrta:
    if nacrt.problemi:
        return StanjeNacrta.PROBLEM
    if nacrt.nemapirane:
        return StanjeNacrta.CEKA_SIFRU
    return StanjeNacrta.SPREMAN


def osvezi(document_id: int, *, korisnik: str = "app", engine=None) -> NacrtKalkulacije | None:
    """Ponovo sklopi nacrt za jedan dokument i upise ishod. Ne dira ERP upisom."""
    with session_scope() as session:
        doc = session.get(Document, document_id)
        avans = doc is not None and doc.document_type is DocumentType.PREPAYMENT
    if avans:
        # Ne troši se ni jedan upit ka ERP-u: avans nema sta da trazi u kalkulaciji.
        return _upisi_stanje(document_id, StanjeNacrta.NE_TREBA)

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


def _upisi_stanje(document_id: int, stanje: StanjeNacrta) -> NacrtKalkulacije:
    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        if red is None:
            red = NacrtKalkulacije(document_id=document_id)
            session.add(red)
        red.stanje = stanje
        red.problemi = None
        red.nemapirane = None
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
