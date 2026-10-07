"""Obavestenje primaoca o prethodnom porezu, uz knjizno odobrenje.

Kupac njime javlja dobavljacu koliko PDV-a nije koristio kao prethodni porez.
Na SEF portalu je to dugme „Kreiraj obavestenje o prethodnom porezu“ na samom
dokumentu; ovde ide kroz API, jednim klikom, bez otvaranja portala.

Sta obavestenje trazi i odakle uzimamo (mereno na 967 odobrenja, 30.09.):

* `NoticeNumber` - **broj samog knjiznog odobrenja**, doslovno;
* `TotalVatAmount` - PDV sa odobrenja, mora biti veci od nule;
* `DocumentIssueOrigin` - `DocumentIssuedViaSef`, jer odobrenje stize preko SEF-a;
* `BasisOfNotice` - `CreditNoteIssued`;
* `DocumentReferenceType` - `CreditNoteReferenceToInvoice` kad odobrenje pokriva
  jednu fakturu (947 od 967), `CreditNoteReferenceToPeriod` kad ih pokriva vise (20);
* `RelatedDocumentNumber` i `RelatedDocumentIssueDate` - broj i datum **odobrenja**,
  ne originalne fakture; ovo je prvo procitano pogresno pa ispravljeno po formi sa portala;
* `Recipient.vatRegistrationCode` - PIB dobavljaca.

Sve to imamo za 966 od 967 odobrenja. Jedino koje ne moze je ono sa nultim
PDV-om - za takvo obavestenje ni ne postoji, jer iznos mora biti veci od nule.
"""

from __future__ import annotations

import logging

from ..db import session_scope
from ..models import (
    AuditLog,
    Document,
    DocumentType,
    ObavestenjeOPrethodnomPorezu,
    utcnow,
)

log = logging.getLogger(__name__)

PORAKLO_SEF = "DocumentIssuedViaSef"
OSNOV_ODOBRENJE = "CreditNoteIssued"
VEZA_FAKTURA = "CreditNoteReferenceToInvoice"
VEZA_PERIOD = "CreditNoteReferenceToPeriod"


class NeMozeObavestenje(RuntimeError):
    """Razlog zbog kog obavestenje ne moze da se posalje - ide coveku, ne u log."""


def _proveri(doc: Document) -> None:
    if doc is None:
        raise NeMozeObavestenje("Dokument ne postoji.")
    if doc.document_type is not DocumentType.CREDIT_NOTE:
        raise NeMozeObavestenje(
            "Obaveštenje o prethodnom porezu ide uz knjižno odobrenje, "
            f"a ovo je {doc.document_type.value}."
        )
    if not (doc.supplier_vat or "").strip():
        raise NeMozeObavestenje("Dokument nema PIB dobavljača — nema kome da se pošalje.")
    if not (doc.document_number or "").strip():
        raise NeMozeObavestenje("Dokument nema broj.")
    if doc.issue_date is None:
        raise NeMozeObavestenje("Dokument nema datum izdavanja.")
    if abs(doc.vat_amount or 0) <= 0:
        raise NeMozeObavestenje(
            "PDV na ovom odobrenju je nula — obaveštenje traži iznos veći od nule."
        )


def telo_zahteva(doc: Document) -> dict:
    """Telo poziva ka SEF-u, sklopljeno iz samog odobrenja."""
    _proveri(doc)
    vise_faktura = (doc.billing_reference_count or 0) > 1
    return {
        "noticeNumber": doc.document_number.strip()[:200],
        "totalVatAmount": round(abs(doc.vat_amount or 0), 2),
        "documentIssueOrigin": PORAKLO_SEF,
        "basisOfNotice": OSNOV_ODOBRENJE,
        "documentReferenceType": VEZA_PERIOD if vise_faktura else VEZA_FAKTURA,
        "relatedDocumentNumber": doc.document_number.strip(),
        "relatedDocumentIssueDate": doc.issue_date.isoformat(),
        "recipient": {"vatRegistrationCode": doc.supplier_vat.strip()},
    }


def zabelezi_rucno(document_id: int, korisnik: str) -> str:
    """Covek je obavestenje napravio na portalu - app vise ne nudi slanje.

    Bez ovoga bi izvestaj zauvek prijavljivao gresku za nesto sto je zavrseno,
    a neko bi pre ili kasnije kliknuo "posalji ponovo" i poslao dobavljacu
    **drugo** obavestenje - sto se ne moze povuci.
    """
    with session_scope() as session:
        red = session.get(ObavestenjeOPrethodnomPorezu, document_id)
        if red is None:
            raise NeMozeObavestenje("Za ovaj dokument nema zapisa o obaveštenju.")
        if red.uspelo:
            return f"Obaveštenje {red.broj} je app već poslala — nema šta da se beleži."
        red.rucno_potvrdio = korisnik
        red.rucno_u = utcnow()
        session.add(
            AuditLog(
                actor=korisnik,
                action="obavestenje.rucno",
                document_id=document_id,
                detail=f"{red.broj}: napravljeno na portalu",
            )
        )
        return f"Zabeleženo: obaveštenje {red.broj} je napravljeno ručno na portalu."


def posalji(document_id: int, *, korisnik: str, klijent=None) -> tuple[bool, str]:
    """Salje obavestenje i pamti trag. Vraca (da li je poslato, poruka za coveka).

    Slanje je prema dobavljacu i nepovratno, pa se ne ponavlja: ako je jednom
    proslo, drugi poziv samo kaze da vec postoji.
    """
    from ..sef.client import SefClient

    with session_scope() as session:
        vec = session.get(ObavestenjeOPrethodnomPorezu, document_id)
        if vec is not None and vec.uspelo:
            return False, f"Obaveštenje {vec.broj} je već poslato ({vec.poslato_u:%d.%m.%Y})."
        doc = session.get(Document, document_id)
        try:
            telo = telo_zahteva(doc)
        except NeMozeObavestenje as exc:
            return False, str(exc)
        broj = telo["noticeNumber"]
        iznos = telo["totalVatAmount"]
        vrsta = telo["documentReferenceType"]

    greska = None
    odgovor: dict = {}
    try:
        with (klijent or SefClient()) as sef:
            odgovor = sef.posalji_obavestenje_o_prethodnom_porezu(telo)
    except Exception as exc:  # noqa: BLE001 - poruka ide coveku, trag u log
        log.warning("Obaveštenje za #%s nije poslato: %s", document_id, exc)
        greska = str(exc)[:500]

    sef_id = odgovor.get("Id") or odgovor.get("id")
    status = odgovor.get("SendingStatus") or odgovor.get("sendingStatus")

    with session_scope() as session:
        session.merge(
            ObavestenjeOPrethodnomPorezu(
                document_id=document_id,
                broj=broj,
                sef_notice_id=sef_id,
                status=status,
                iznos_pdv=iznos,
                vrsta_veze=vrsta,
                poslao=korisnik,
                poslato_u=utcnow(),
                greska=greska,
            )
        )
        session.add(
            AuditLog(
                actor=korisnik,
                action="obavestenje.poslato" if not greska else "obavestenje.palo",
                document_id=document_id,
                detail=f"{broj}, PDV {iznos}"
                + (f", greška: {greska[:200]}" if greska else f", status {status or '-'}"),
            )
        )

    if greska:
        return False, f"Obaveštenje nije poslato: {greska[:160]}"
    return True, f"Obaveštenje {broj} je poslato na SEF (PDV {iznos:,.2f})."


def bez_obavestenja(session) -> list[Document]:
    """Prihvacena knjizna odobrenja za koja obavestenje nije proslo.

    Ne sluzi masovnom slanju - ono ide iskljucivo uz prihvatanje - nego da se
    vidi sta je propalo, jer slanje ume da padne a prihvatanje ostaje.
    """
    from sqlalchemy import select

    from ..models import SefStatus

    poslata = {
        o.document_id
        for o in session.scalars(select(ObavestenjeOPrethodnomPorezu))
        if o.uspelo
    }
    return [
        d
        for d in session.scalars(
            select(Document).where(
                Document.document_type == DocumentType.CREDIT_NOTE,
                Document.sef_status == SefStatus.APPROVED,
                Document.archived == False,  # noqa: E712
            )
        )
        if d.id not in poslata and abs(d.vat_amount or 0) > 0
    ]
