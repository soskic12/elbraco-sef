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
from datetime import date

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
VEZA_AVANS = "CreditNoteReferenceToPrepaymentInvoice"
# Avansni racun po UBL-u (InvoiceTypeCode 386).
KOD_AVANSA = "386"


# Statusi u kojima SEF obavestenje NIJE poslao dalje. Tacan tekst ne znamo -
# dokumentacija pominje samo „Greska u slanju“ i poseban poziv `mistake-status`
# koji takve vraca - pa se prepoznaje po korenu reci, a svi vidjeni statusi se
# ispisuju da se spisak moze dopuniti kad se pojavi nesto novo.
KOREN_GRESKE = ("mistake", "error", "fail", "gres", "greš", "греш")


def _status_je_greska(status: str) -> bool:
    nisko = (status or "").lower()
    return any(k in nisko for k in KOREN_GRESKE)


def _samo_cifre(vrednost: str) -> str:
    """PIB bez prefiksa i razmaka.

    SEF u odgovoru ume da vrati `RS100000104`, a mi cuvamo `100000104`. Zbog te
    razlike poklapanje obavestenja po broju **nikad nije proradilo** (`po_broju: 0`
    na 1.358 obavestenja), pa je komanda prijavljivala kao neposlata ona koja na
    SEF-u postoje - a to vodi pravo u drugo obavestenje dobavljacu, koje se ne
    moze povuci. Bane je to uhvatio rucnom proverom 09.10.
    """
    return "".join(z for z in str(vrednost) if z.isdigit())


def _trenutak(kad) -> str | None:
    """Nas `sent_date` kao pun ISO trenutak u UTC - rezerva kad SEF-ov zapis ne stigne."""
    if kad is None:
        return None
    from datetime import timezone

    if getattr(kad, "tzinfo", None) is None:
        kad = kad.replace(tzinfo=timezone.utc)
    return kad.isoformat()


def _kljuc_broja(broj: str) -> str:
    """Broj obavestenja za poredjenje - bez razmaka, velikim slovima."""
    return "".join(str(broj).split()).upper()


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


def vrsta_veze(doc: Document) -> str:
    """Koji je izvorni dokument odobrenja - faktura, avansna faktura ili period.

    Dokumentacija (poslovno pravilo 5) dopusta tri vrednosti uz
    `CreditNoteIssued`, a mi smo do 08.10. koristili dve: avansna faktura je
    isla kao obicna. Odobrenje na avans je redak slucaj ali postoji, i SEF za
    njega trazi svoju vrednost.
    """
    if (doc.billing_reference_count or 0) > 1:
        return VEZA_PERIOD
    if doc.prepayment_date is not None or doc.invoice_type_code == KOD_AVANSA:
        return VEZA_AVANS
    return VEZA_FAKTURA


def period_veze(doc: Document) -> tuple[date, date] | None:
    """Pocetak i kraj perioda na koji se odobrenje odnosi.

    SEF ovo trazi kao **obavezno** kad je vrsta veze `CreditNoteReferenceToPeriod`
    (tabela polja, strana 177) - u proznom delu stoji „опционо“, i zato je ovo
    dugo promaseno. Cetiri obavestenja su 02.10. pala sa `400`, sva cetiri bas
    na vise faktura.

    Izvori, po pouzdanosti: period iz UBL-a odobrenja, pa datumi izdavanja
    faktura na koje se poziva, pa datum samog odobrenja kao poslednja odbrana -
    jer bez ikakvog perioda zahtev sigurno pada.
    """
    if doc.period_start and doc.period_end:
        return doc.period_start, doc.period_end

    datumi = _datumi_povezanih_faktura(doc)
    if datumi:
        return min(datumi), max(datumi)
    return (doc.issue_date, doc.issue_date) if doc.issue_date else None


def _datumi_povezanih_faktura(doc: Document) -> list[date]:
    """Datumi faktura na koje se odobrenje poziva, iz naseg sopstvenog spiska."""
    from sqlalchemy import select

    from ..db import session_scope as _scope

    brojevi = [b.strip() for b in (doc.billing_reference or "").split("|") if b.strip()]
    if not brojevi:
        return []
    with _scope() as session:
        return [
            d
            for (d,) in session.execute(
                select(Document.issue_date).where(
                    Document.supplier_vat == doc.supplier_vat,
                    Document.document_number.in_(brojevi),
                    Document.issue_date.is_not(None),
                )
            )
        ]


def sef_zapis_dokumenta(doc: Document, klijent=None) -> dict | None:
    """SEF-ov sopstveni zapis o dokumentu: broj i trenutak prijema, doslovno.

    Obavestenje koje je proslo (`90002975`, 09.10.) nosi:

        RelatedDocumentNumber    "90002975  "
        RelatedDocumentIssueDate "2026-09-28T19:28:39.7200000+00:00"

    Dakle broj **sa razmacima** i **pun trenutak prijema na SEF** (`SentDate`),
    ne datum izdavanja. Mi smo slali skracen broj i samo datum, pa je SEF vracao
    `400 Source document not found`.

    Vrednosti se uzimaju iz SEF-ovog pregleda umesto iz nase baze zato sto se
    `sent_date` kod nas zaokruzuje pri upisu (MS SQL `DateTime` ide do ~3 ms), a
    ovde treba doslovno ono sto SEF drzi.
    """
    from ..sef.client import SefClient

    if not doc.sef_invoice_id or doc.sent_date is None:
        return None
    dan = doc.sent_date.date()
    try:
        if klijent is not None:
            redovi = klijent.purchase_overview(dan, dan)
        else:
            with SefClient() as sef:
                redovi = sef.purchase_overview(dan, dan)
    except Exception as exc:  # noqa: BLE001 - radimo sa onim sto imamo
        log.warning("SEF-ov zapis za #%s nije procitan: %s", doc.id, exc)
        return None
    for r in redovi or []:
        if int(r.get("InvoiceId") or r.get("invoiceId") or 0) == doc.sef_invoice_id:
            return r
    return None


def telo_zahteva(doc: Document, sef_zapis: dict | None = None) -> dict:
    """Telo poziva ka SEF-u. `sef_zapis` je SEF-ov red o dokumentu, ako ga imamo."""
    _proveri(doc)
    zapis = sef_zapis or {}
    vrsta = vrsta_veze(doc)
    telo = {
        "noticeNumber": doc.document_number.strip()[:200],
        "totalVatAmount": round(abs(doc.vat_amount or 0), 2),
        "documentIssueOrigin": PORAKLO_SEF,
        "basisOfNotice": OSNOV_ODOBRENJE,
        "documentReferenceType": vrsta,
        # NE skracivati: SEF cuva broj doslovno onako kako ga je dobavljac
        # poslao. KIM-TEC salje `90002972  ` sa dva razmaka na kraju (provereno
        # 09.10. u SEF-ovom pregledu i u samom UBL-u), pa je skraceni broj davao
        # `400 Source document not found` - SEF nije nalazio izvorni dokument.
        # `noticeNumber` ostaje skracen: to je nas broj obavestenja, ne pokazivac.
        "relatedDocumentNumber": (
            zapis.get("DocumentNumber") or zapis.get("documentNumber") or doc.document_number
        ),
        "relatedDocumentIssueDate": (
            zapis.get("SentDate")
            or zapis.get("sentDate")
            or _trenutak(doc.sent_date)
            or doc.issue_date.isoformat()
        ),
        "recipient": {"vatRegistrationCode": doc.supplier_vat.strip()},
    }

    # Poslovno pravilo 17: `registrationCode` je OBAVEZAN kad je primalac
    # korisnik javnih sredstava (tada je to JBKJS), a opcion za privredni
    # subjekat (matični broj). SEF nam za javna preduzeca u polju matičnog i
    # salje JBKJS, pa je ono što imamo ujedno i ono što treba.
    sifra = (doc.supplier_reg_no or "").strip()
    if sifra:
        telo["recipient"]["registrationCode"] = sifra

    if vrsta == VEZA_PERIOD:
        period = period_veze(doc)
        if period is None:
            raise NeMozeObavestenje(
                "Odobrenje pokriva više faktura, a period se ne može odrediti — "
                "SEF ga tada traži kao obavezan."
            )
        telo["relatedInvoicePeriodStartDate"] = period[0].isoformat()
        telo["relatedInvoicePeriodEndDate"] = period[1].isoformat()

    return telo


def zabelezi_rucno(document_id: int, korisnik: str) -> str:
    """Covek je obavestenje napravio na portalu - app vise ne nudi slanje.

    Bez ovoga bi izvestaj zauvek prijavljivao gresku za nesto sto je zavrseno,
    a neko bi pre ili kasnije kliknuo "posalji ponovo" i poslao dobavljacu
    **drugo** obavestenje - sto se ne moze povuci.
    """
    with session_scope() as session:
        red = session.get(ObavestenjeOPrethodnomPorezu, document_id)
        if red is None:
            # Nema zapisa znaci da slanje nikad nije pokusano - tako stoji svako
            # odobrenje koje nije prihvaceno kroz aplikaciju (na portalu ili pre
            # prelaska). Takvih je 08.10. bilo 81, i bas njima ovo dugme treba
            # najvise, pa se zapis napravi sada.
            doc = session.get(Document, document_id)
            if doc is None:
                raise NeMozeObavestenje("Nema takvog dokumenta.")
            red = ObavestenjeOPrethodnomPorezu(
                document_id=document_id,
                broj=doc.document_number or str(document_id),
                iznos_pdv=abs(doc.vat_amount or 0) or None,
                poslao=korisnik,
            )
            session.add(red)
            session.flush()
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
        # Broj i trenutak izvornog dokumenta se uzimaju od SEF-a, ne iz nase
        # baze - vidi `sef_zapis_dokumenta`.
        try:
            zapis = sef_zapis_dokumenta(doc, klijent)
        except Exception:  # noqa: BLE001 - bez zapisa se salje iz nasih podataka
            zapis = None
        try:
            telo = telo_zahteva(doc, zapis)
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
            # Telo zahteva se zapisuje uz svaki neuspeh. Bez toga se posle ne zna
            # sta je tacno otislo, pa se poredi po secanju - a greska
            # `Source document not found` je 09.10. tri puta izgledala isto iako
            # je kod bio razlicit. U telu nema nicega tajnog.
            import json as _json

            session.add(
                AuditLog(
                    actor=korisnik,
                    action="obavestenje.telo",
                    document_id=document_id,
                    detail=_json.dumps(telo, ensure_ascii=False)[:2000],
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

    # `zavrseno`, ne `uspelo`: obavestenje koje je covek napravio na portalu
    # dobavljac ima, pa ga nema sta da prijavljujemo kao propalo. Do 08.10. je
    # ovde stajalo `uspelo`, pa su ona cetiri rucna zauvek ulazila u spisak.
    poslata = {
        o.document_id
        for o in session.scalars(select(ObavestenjeOPrethodnomPorezu))
        if o.zavrseno
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


def uskladi_sa_sefom(
    od: date, do: date, *, stvarno: bool = False, klijent=None
) -> dict:  # noqa: C901 - jedan prolaz, dve provere nad istim odgovorom
    """Pita SEF koja obavestenja vec postoje i zabelezi ih u panelu.

    Panel vidi samo svoje pokusaje slanja. Covek je obavestenja godinu dana
    pravio na portalu, pa je 08.10. izgledalo da 81 odobrenje duguje obavestenje
    - a Bane kaze da je "sve stare poslao peske". Nagadjati se ne mora: SEF na
    `sender/date-range` vraca sva obavestenja koja smo poslali, bilo kako.

    Poklapanje se trazi po `PurchaseInvoiceId` (to je dokument na koji se
    obavestenje odnosi, pa je dokaz), a kad njega nema - po broju obavestenja
    uz PIB dobavljaca, jer broj obavestenja kod nas jeste broj odobrenja.

    Bez `stvarno` samo prebroji; nista se ne salje ni u jednom slucaju.
    """
    from sqlalchemy import select

    from ..sef.client import SefClient

    with (klijent or SefClient()) as sef:
        redovi = sef.poslata_obavestenja(od, do)

    po_sef_id: dict[int, dict] = {}
    po_broju: dict[tuple[str, str], dict] = {}
    samo_broj: dict[str, list[dict]] = {}
    for r in redovi:
        sef_doc = r.get("PurchaseInvoiceId") or r.get("purchaseInvoiceId")
        if sef_doc:
            po_sef_id[int(sef_doc)] = r
        broj = _kljuc_broja(r.get("NoticeNumber") or r.get("noticeNumber") or "")
        primalac = r.get("Recipient") or r.get("recipient") or {}
        pib = _samo_cifre(
            primalac.get("VatRegistrationCode")
            or primalac.get("vatRegistrationCode")
            or ""
        )
        if broj:
            po_broju[(broj, pib)] = r
            samo_broj.setdefault(broj, []).append(r)

    ishod = {
        "sa_sefa": len(redovi),
        "po_dokumentu": 0,
        "po_broju": 0,
        "nema_obavestenje": [],
        "zabelezeno": 0,
        "statusi": {},
        "sa_greskom": [],
        "naknadno_palo": [],
    }

    # Obavestenje koje je proslo pri slanju SEF ume kasnije da prebaci u
    # „Greska u slanju“ i tada ga ne salje dalje. Nas zapis o tome ne zna nista,
    # a `bez_obavestenja` takav dokument preskace jer kod nas „uspelo“ - pa ga
    # niko nikad vise ne pogleda. Zato se uz uskladjivanje proveravaju i ona
    # koja smatramo zavrsenim.
    po_sef_broju = {
        int(r.get("Id") or r.get("id") or 0): r
        for r in redovi
        if (r.get("Id") or r.get("id"))
    }

    with session_scope() as session:
        for red in session.scalars(
            select(ObavestenjeOPrethodnomPorezu).where(
                ObavestenjeOPrethodnomPorezu.sef_notice_id.is_not(None)
            )
        ):
            nalaz = po_sef_broju.get(red.sef_notice_id or 0)
            if nalaz is None:
                continue
            status = str(
                nalaz.get("SendingStatus") or nalaz.get("sendingStatus") or "?"
            )
            if not _status_je_greska(status):
                continue
            ishod["naknadno_palo"].append(red.document_id)
            if stvarno and not red.greska:
                red.status = status
                red.greska = f"SEF ga je prebacio u status „{status}“ — nije poslat dalje."
                session.add(
                    AuditLog(
                        actor="sef",
                        action="obavestenje.naknadno_palo",
                        document_id=red.document_id,
                        detail=f"{red.broj}: status {status}",
                    )
                )

    with session_scope() as session:
        for doc in bez_obavestenja(session):
            nalaz = po_sef_id.get(doc.sef_invoice_id or 0)
            kako = "po_dokumentu"
            if nalaz is None:
                kljuc = _kljuc_broja(doc.document_number or "")
                nalaz = po_broju.get((kljuc, _samo_cifre(doc.supplier_vat or "")))
                if nalaz is None:
                    # Broj obavestenja je broj odobrenja i jedinstven je medju
                    # nasim dokumentima, pa kad se poklopi tacno jedan - to je on.
                    kandidati = samo_broj.get(kljuc, [])
                    if len(kandidati) == 1:
                        nalaz = kandidati[0]
                kako = "po_broju"
            if nalaz is None:
                ishod["nema_obavestenje"].append(doc.id)
                continue
            status = str(
                nalaz.get("SendingStatus") or nalaz.get("sendingStatus") or "?"
            )
            ishod["statusi"][status] = ishod["statusi"].get(status, 0) + 1
            if _status_je_greska(status):
                # SEF ga ima, ali ga nije poslao dalje - dobavljac ga NEMA, pa
                # ovo nije zavrseno. Bez ove provere bismo zatvorili obavestenje
                # koje nikome nije stiglo.
                ishod["sa_greskom"].append(doc.id)
                continue
            ishod[kako] += 1
            if not stvarno:
                continue
            red = session.get(ObavestenjeOPrethodnomPorezu, doc.id)
            if red is None:
                red = ObavestenjeOPrethodnomPorezu(
                    document_id=doc.id,
                    broj=(nalaz.get("NoticeNumber") or nalaz.get("noticeNumber") or "")[:200]
                    or (doc.document_number or str(doc.id)),
                    poslao="sef",
                )
                session.add(red)
                session.flush()
            red.sef_notice_id = nalaz.get("Id") or nalaz.get("id")
            red.status = nalaz.get("SendingStatus") or nalaz.get("sendingStatus")
            red.greska = None
            if red.iznos_pdv is None:
                red.iznos_pdv = abs(doc.vat_amount or 0) or None
            session.add(
                AuditLog(
                    actor="sef",
                    action="obavestenje.nadjeno",
                    document_id=doc.id,
                    detail=f"{red.broj}: nađeno na SEF-u ({kako})",
                )
            )
            ishod["zabelezeno"] += 1

    return ishod
