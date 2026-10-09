"""Preuzimanje i obrada ulaznih dokumenata sa SEF-a.

Tok za jedan dokument:
    /purchase-invoice/overview  ->  upis zaglavlja u bazu
    /purchase-invoice/xml       ->  UBL na disk + parsiranje (zaglavlje + stavke)
    pravila razvrstavanja       ->  poslovna jedinica
    notifikacije                ->  email / push / dashboard
    (opciono) auto-prihvatanje  ->  acceptRejectPurchaseInvoice

Sinhronizacija je idempotentna: dokument se prepoznaje po `sef_invoice_id`,
ponovni prolaz preko istog perioda samo osvezava statuse.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import (
    AuditLog,
    Document,
    DocumentLine,
    DocumentType,
    ProcessState,
    RoutingSource,
    SefStatus,
    Setting,
    SyncRun,
    utcnow,
)
from ..routing.engine import RoutingEngine
from ..sef.client import SefClient, SefError
from ..textutil import shorten
from ..ubl.parser import UblDocument, UblParseError, parse_ubl

log = logging.getLogger(__name__)

LAST_SYNC_KEY = "last_sync_date"


# --------------------------------------------------------------------------- #
# pomocne funkcije
# --------------------------------------------------------------------------- #


def pick(data: dict[str, Any], *names: str) -> Any:
    """SEF ume da vrati i PascalCase i camelCase kljuceve."""
    for name in names:
        if name in data:
            return data[name]
    lowered = {k.lower(): v for k, v in data.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_dt(raw: Any) -> datetime | None:
    """'2025-08-01T11:01:10.5030093+00:00' -> aware datetime (UTC)."""
    if not raw or not isinstance(raw, str):
        return None
    text = _FRACTION_RE.sub(r"\1", raw.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        log.debug("Nepoznat format datuma: %r", raw)
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_d(raw: Any) -> date | None:
    dt = parse_dt(raw)
    return dt.date() if dt else None


def _as_date(value: date | str | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def as_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def to_enum(enum_cls, raw: Any, default):
    if raw is None:
        return default
    text = str(raw).strip()
    for member in enum_cls:
        if member.value.lower() == text.lower():
            return member
    return default


@dataclass
class SyncStats:
    seen: int = 0
    created: int = 0
    updated: int = 0
    errors: int = 0
    notified: int = 0
    accepted: int = 0
    messages: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.messages is None:
            self.messages = []

    def summary(self) -> str:
        return (
            f"pregledano={self.seen} novih={self.created} azurirano={self.updated} "
            f"obavesteno={self.notified} prihvaceno={self.accepted} greske={self.errors}"
        )


# --------------------------------------------------------------------------- #
# servis
# --------------------------------------------------------------------------- #


class IngestService:
    def __init__(
        self,
        client: SefClient | None = None,
        settings: Settings | None = None,
        notifier=None,
        acceptor=None,
    ):
        self.settings = settings or get_settings()
        self.client = client or SefClient(self.settings)
        # lazy import da bi moduli mogli da se testiraju nezavisno
        if notifier is None:
            from ..notify.dispatcher import Dispatcher

            notifier = Dispatcher(self.settings)
        self.notifier = notifier
        if acceptor is None:
            from .acceptance import AcceptanceService

            acceptor = AcceptanceService(self.client, self.settings)
        self.acceptor = acceptor

    # ------------------------------------------------------------------ #
    # javni ulaz
    # ------------------------------------------------------------------ #

    def sync(
        self, date_from: date | str | None = None, date_to: date | str | None = None
    ) -> SyncStats:
        """Sinhronizuje period; podrazumevano od poslednjeg uspesnog sync-a."""
        date_from = _as_date(date_from)
        date_to = _as_date(date_to)
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = self._default_date_from(today)

        stats = SyncStats()
        with session_scope() as session:
            run = SyncRun(date_from=date_from, date_to=date_to)
            session.add(run)
            session.flush()
            run_id = run.id

        log.info("SEF sync %s .. %s", date_from, date_to)
        try:
            records = self.client.purchase_overview(date_from, date_to)
        except SefError as exc:
            log.error("Neuspesno preuzimanje pregleda: %s", exc)
            with session_scope() as session:
                run = session.get(SyncRun, run_id)
                run.finished_at = utcnow()
                run.ok = False
                run.message = str(exc)
            stats.errors += 1
            stats.messages.append(str(exc))
            return stats

        stats.seen = len(records)
        for record in records:
            try:
                self._process_record(record, stats)
            except Exception as exc:  # noqa: BLE001 - jedan los dokument ne sme da obori sync
                stats.errors += 1
                invoice_id = pick(record, "InvoiceId")
                log.exception("Greska na dokumentu %s: %s", invoice_id, exc)
                self._mark_error(invoice_id, exc)

        with session_scope() as session:
            run = session.get(SyncRun, run_id)
            run.finished_at = utcnow()
            run.seen, run.created, run.updated, run.errors = (
                stats.seen,
                stats.created,
                stats.updated,
                stats.errors,
            )
            run.ok = stats.errors == 0
            run.message = stats.summary()
            if stats.errors == 0:
                self._set_setting(session, LAST_SYNC_KEY, date_to.isoformat())
        log.info("SEF sync gotov: %s", stats.summary())
        return stats

    def fetch_one(self, sef_invoice_id: int, force: bool = False) -> Document | None:
        """Preuzima/osvezava jedan dokument po SEF ID-u (npr. iz dashboarda).

        Dokument koji je na SEF-u jos "Nova" se NE dira dok je zastita ukljucena -
        ni poziv za detalje, jer se ne zna da li i on obara status. Dok kolege
        prate sta je novo na portalu, jedan klik u panelu im menja spisak.
        """
        stats = SyncStats()
        if self.settings.sef_preserve_new:
            with session_scope() as session:
                doc = session.scalar(
                    select(Document).where(Document.sef_invoice_id == sef_invoice_id)
                )
                if doc is not None and doc.sef_status is SefStatus.NEW:
                    log.info(
                        "Ne osvezavam %s - na SEF-u je jos 'Nova', a zastita je ukljucena",
                        sef_invoice_id,
                    )
                    return doc
        record = {"InvoiceId": sef_invoice_id}
        try:
            detail = self.client.purchase_invoice(sef_invoice_id)
            record.update(detail)
        except SefError as exc:
            log.warning("Ne mogu da preuzmem detalje za %s: %s", sef_invoice_id, exc)
        self._process_record(record, stats, force_refetch=force)
        with session_scope() as session:
            return session.scalar(select(Document).where(Document.sef_invoice_id == sef_invoice_id))

    # ------------------------------------------------------------------ #
    # obrada jednog zapisa iz /overview
    # ------------------------------------------------------------------ #

    def _process_record(
        self, record: dict[str, Any], stats: SyncStats, force_refetch: bool = False
    ) -> None:
        invoice_id = pick(record, "InvoiceId", "PurchaseInvoiceId", "invoiceId")
        if invoice_id is None:
            raise ValueError(f"Zapis bez InvoiceId: {record!r}")
        invoice_id = int(invoice_id)

        with session_scope() as session:
            doc = session.scalar(select(Document).where(Document.sef_invoice_id == invoice_id))
            is_new = doc is None
            if is_new:
                doc = Document(sef_invoice_id=invoice_id)
                session.add(doc)
            changed = self._apply_overview(doc, record)
            session.flush()

            need_ubl = force_refetch or is_new or not doc.ubl_path or not doc.lines
            # Preuzimanje UBL-a obara status "Nova" u "Vidjena" na SEF-u (mereno:
            # dogadjaj stigne 70-90 ms posle naseg poziva). Dok ljudi jos prate
            # sta je novo na portalu, to im remeti posao - pa se takav dokument
            # samo upisuje iz pregleda i ceka. UBL se povlaci kad status prestane
            # da bude "Nova", ili kad se prekidac ugasi.
            # Vazi i kad je trazeno osvezavanje: dugme u panelu je ranije bilo
            # izuzetak, pa je jedan klik operatera obarao status na portalu -
            # tacno ono sto ova zastita treba da spreci. Prekidac je jedini izuzetak.
            if need_ubl and self.settings.sef_preserve_new and doc.sef_status is SefStatus.NEW:
                need_ubl = False
                log.debug("Preskacem UBL za %s - status je Nova", doc.sef_invoice_id)

            if need_ubl:
                self._fetch_and_parse(session, doc)

            # dokument bez uspesno preuzetog UBL-a se ne razvrstava - nema po cemu
            razvrstan_sada = False
            if doc.business_unit_id is None and doc.state is not ProcessState.ERROR:
                self._route(session, doc)
                razvrstan_sada = doc.business_unit_id is not None

            session.flush()
            if is_new:
                stats.created += 1
                session.add(
                    AuditLog(
                        action="document.created",
                        document_id=doc.id,
                        detail=f"{doc.document_number} / {doc.supplier_name}",
                    )
                )
            elif changed:
                stats.updated += 1

            doc_id = doc.id
            treba_nacrt = (need_ubl or razvrstan_sada) and doc.business_unit_id is not None
            # Preuzimanje ne obavestava nikoga: dokument ide u red operatera,
            # koji ga posle pregleda prosledjuje poslovnoj jedinici.
            should_notify = (
                self.settings.notify_on_ingest
                and is_new
                and doc.state in (ProcessState.ROUTED, ProcessState.UNASSIGNED, ProcessState.FETCHED)
            )

        # Nacrt kalkulacije se racuna odmah, dok je dokument svez. Ranije ga je
        # racunala samo komanda `nacrti --osvezi`, pa je tab "Sifre" pokazivao
        # stanje od poslednjeg rucnog pokretanja: faktura koja stigne u 12:55, a
        # racunato je u 12:08, nije se videla iako joj nijedna stavka nije imala
        # nasu sifru. Racuna se samo kad ima sta novo (preuzet UBL ili tek
        # razvrstan), ne za svih 780 dokumenata u svakom prolazu.
        if treba_nacrt:
            self._osvezi_nacrt(doc_id)

        # notifikacije i prihvatanje - van glavne transakcije (mrezni pozivi)
        if should_notify:
            if self.notifier.notify_document(doc_id):
                stats.notified += 1
        if self.acceptor.maybe_auto_accept(doc_id):
            stats.accepted += 1

    @staticmethod
    def _osvezi_nacrt(doc_id: int) -> None:
        """Nacrt kalkulacije za jedan dokument; nedostupan ERP ne sme da obori sync."""
        try:
            from .nacrti import osvezi

            osvezi(doc_id)
        except Exception as exc:  # noqa: BLE001 - ERP je tudja baza i ume da bude dole
            log.info("Nacrt za #%s nije izracunat: %s", doc_id, str(exc)[:160])

    def _apply_overview(self, doc: Document, record: dict[str, Any]) -> bool:
        """Upisuje polja iz /overview (ili /purchase-invoice) zapisa. Vraca True ako se nesto promenilo."""
        before = (doc.sef_status, doc.sef_version, doc.amount)

        doc.raw_overview = {str(k): v for k, v in record.items() if not isinstance(v, (bytes, bytearray))}
        doc.glob_uniq_id = pick(record, "GlobUniqId") or doc.glob_uniq_id
        doc.cir_invoice_id = pick(record, "CirInvoiceId") or doc.cir_invoice_id
        doc.document_number = pick(record, "DocumentNumber", "InvoiceNumber") or doc.document_number
        doc.document_type = to_enum(DocumentType, pick(record, "DocumentType"), doc.document_type)
        doc.supplier_name = shorten(pick(record, "SupplierName") or doc.supplier_name, 300)
        doc.supplier_vat = pick(record, "SupplierVatRegistrationNumber") or doc.supplier_vat
        doc.supplier_reg_no = pick(record, "SupplierRegistrationNumber") or doc.supplier_reg_no

        # SEF u pregledu mesa valute: `Amount` je u valuti fakture, a
        # `SumWithoutVat` i `VatAmount` su u **dinarima**. Mereno 07.10. na
        # KR1384362/26: Amount 1.058,96 EUR, SumWithoutVat 124.426,85 RSD.
        #
        # Dok se to upisivalo u istu kolonu, na ekranu je pisalo „osnovica
        # 124.426,85 EUR“, a preracun u dinare je ispao kurs 1,0000 - jer je
        # delio dinare dinarima.
        valuta_zapisa = (pick(record, "Currency") or doc.currency or "RSD").strip().upper()
        devizna = bool(valuta_zapisa) and valuta_zapisa != "RSD"

        polja = [
            ("amount", ("Amount", "SumWithVat")),
            ("rounding_amount", ("RoundingAmount",)),
        ]
        if devizna:
            # Dinarski iznosi idu tamo gde im je mesto; iznose u valuti fakture
            # daje UBL, koji ih drzi dosledno.
            polja += [
                ("sum_without_vat_rsd", ("SumWithoutVat",)),
                ("vat_amount_rsd", ("VatAmount", "VatSum")),
            ]
        else:
            polja += [
                ("sum_without_vat", ("SumWithoutVat",)),
                ("vat_amount", ("VatAmount", "VatSum")),
            ]
        for attr, keys in polja:
            value = as_float(pick(record, *keys))
            if value is not None:
                setattr(doc, attr, value)

        doc.currency = pick(record, "Currency") or doc.currency
        doc.delivery_date = parse_d(pick(record, "DeliveryDate")) or doc.delivery_date
        doc.due_date = parse_d(pick(record, "DueDate")) or doc.due_date
        doc.prepayment_date = parse_d(pick(record, "PrepaymentDate")) or doc.prepayment_date
        doc.sent_date = parse_dt(pick(record, "SentDate")) or doc.sent_date

        status = pick(record, "Status")
        if status:
            doc.sef_status = to_enum(SefStatus, status, SefStatus.UNKNOWN)
        version = pick(record, "Version")
        if version is not None:
            doc.sef_version = int(version)

        if doc.sef_status is SefStatus.APPROVED and doc.state not in (
            ProcessState.ACCEPTED,
            ProcessState.CALCULATED,
        ):
            doc.state = ProcessState.ACCEPTED
        elif doc.sef_status is SefStatus.REJECTED:
            doc.state = ProcessState.REJECTED

        return before != (doc.sef_status, doc.sef_version, doc.amount)

    # ------------------------------------------------------------------ #
    # UBL
    # ------------------------------------------------------------------ #

    def _ubl_path(self, doc: Document) -> Path:
        ref = doc.sent_date or datetime.now(timezone.utc)
        folder = self.settings.ubl_dir / f"{ref.year:04d}" / f"{ref.month:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{doc.sef_invoice_id}.xml"

    def _fetch_and_parse(self, session: Session, doc: Document) -> None:
        try:
            payload = self.client.purchase_ubl(doc.sef_invoice_id)
        except SefError as exc:
            doc.state = ProcessState.ERROR
            doc.error_message = f"Preuzimanje UBL-a: {exc}"
            log.warning("UBL %s nije preuzet: %s", doc.sef_invoice_id, exc)
            return

        path = self._ubl_path(doc)
        path.write_bytes(payload)
        doc.ubl_path = str(path)

        try:
            ubl = parse_ubl(payload)
        except UblParseError as exc:
            doc.state = ProcessState.ERROR
            doc.error_message = f"Parsiranje UBL-a: {exc}"
            log.warning("UBL %s nije parsiran: %s", doc.sef_invoice_id, exc)
            return

        self._apply_ubl(session, doc, ubl)
        doc.error_message = None
        if doc.state in (ProcessState.NEW, ProcessState.ERROR):
            doc.state = ProcessState.FETCHED

        # Knjizno odobrenje zatvara otvoren manjak na fakturi koju ispravlja:
        # ostatak nikad nije stigao, pa nema sta vise da se ceka.
        if doc.document_type is DocumentType.CREDIT_NOTE:
            from .odobrenja import zatvori_manjak_odobrenjem

            session.flush()
            try:
                zatvoren = zatvori_manjak_odobrenjem(doc.id, sesija=session)
            except SQLAlchemyError:
                log.exception("Zatvaranje manjka odobrenjem %s nije proslo", doc.id)
            else:
                if zatvoren:
                    log.info("Odobrenje %s zatvorilo manjak na fakturi %s",
                             doc.document_number, zatvoren)

    def _apply_ubl(self, session: Session, doc: Document, ubl: UblDocument) -> None:
        doc.document_number = doc.document_number or ubl.document_number
        doc.invoice_type_code = ubl.type_code
        if ubl.document_type:
            doc.document_type = to_enum(DocumentType, ubl.document_type, doc.document_type)
        doc.ima_prilog = bool(ubl.attachments)
        doc.issue_date = doc.issue_date or ubl.issue_date
        doc.delivery_date = doc.delivery_date or ubl.delivery_date
        doc.due_date = doc.due_date or ubl.due_date
        doc.currency = doc.currency or ubl.currency
        doc.supplier_name = doc.supplier_name or shorten(
            ubl.supplier.name or ubl.supplier.registration_name, 300
        )
        doc.supplier_vat = doc.supplier_vat or ubl.supplier.vat
        doc.supplier_reg_no = doc.supplier_reg_no or ubl.supplier.registration_number

        devizna = (doc.currency or ubl.currency or "RSD").strip().upper() not in ("", "RSD")
        if doc.amount is None and ubl.payable_amount is not None:
            doc.amount = float(ubl.payable_amount)
        # Kod devizne fakture UBL je jedini izvor iznosa u valuti fakture, pa
        # se uzima i kad je nesto vec upisano - iz pregleda su stizali dinari.
        if (doc.sum_without_vat is None or devizna) and ubl.tax_exclusive_amount is not None:
            doc.sum_without_vat = float(ubl.tax_exclusive_amount)
        if (doc.vat_amount is None or devizna) and ubl.tax_amount is not None:
            doc.vat_amount = float(ubl.tax_amount)
        if ubl.taxable_amount_rsd is not None:
            doc.sum_without_vat_rsd = float(ubl.taxable_amount_rsd)
        if ubl.tax_amount_rsd is not None:
            doc.vat_amount_rsd = float(ubl.tax_amount_rsd)

        doc.delivery_name = shorten(ubl.delivery_name or ubl.delivery_location_id, 300)
        doc.delivery_address = shorten(ubl.delivery_address, 300)
        doc.delivery_city = shorten(ubl.delivery_city, 120)
        doc.buyer_address = shorten(ubl.customer.address, 300)
        doc.buyer_city = shorten(ubl.customer.city, 120)
        doc.buyer_reference = shorten(ubl.buyer_reference, 200)
        doc.order_reference = shorten(ubl.order_reference, 200)
        doc.contract_reference = shorten(ubl.contract_reference, 200)
        doc.additional_reference = shorten(" | ".join(ubl.additional_references), 500)
        # Broj se cuva radi operatera (koju fakturu odobrenje ispravlja), a broj
        # veza radi razvrstavanja - zato se broji pre skracivanja teksta.
        doc.billing_reference = shorten(" | ".join(ubl.billing_references) or None, 500)
        doc.billing_reference_count = len(ubl.billing_references)
        doc.attachment_text = ubl.attachment_text
        doc.payment_account = shorten(ubl.payment_account, 64)
        doc.note = ubl.note

        doc.lines.clear()
        session.flush()
        for line in ubl.lines:
            doc.lines.append(
                DocumentLine(
                    line_no=line.line_no,
                    line_ref=shorten(line.line_ref, 64),
                    name=shorten(line.name, 500),
                    description=line.description,
                    sellers_item_id=shorten(line.sellers_item_id, 100),
                    buyers_item_id=shorten(line.buyers_item_id, 100),
                    standard_item_id=shorten(line.standard_item_id, 100),
                    quantity=float(line.quantity) if line.quantity is not None else None,
                    unit_code=line.unit_code,
                    price=float(line.price) if line.price is not None else None,
                    base_quantity=float(line.base_quantity) if line.base_quantity is not None else None,
                    line_amount=float(line.line_amount) if line.line_amount is not None else None,
                    allowance_amount=float(line.allowance_amount)
                    if line.allowance_amount is not None
                    else None,
                    charge_amount=float(line.charge_amount) if line.charge_amount is not None else None,
                    vat_percent=float(line.vat_percent) if line.vat_percent is not None else None,
                    vat_category=line.vat_category,
                    note=line.note,
                )
            )

    # ------------------------------------------------------------------ #
    # razvrstavanje
    # ------------------------------------------------------------------ #

    def _route(self, session: Session, doc: Document) -> None:
        engine = RoutingEngine.from_db(session)
        fields = doc.routing_fields()
        decision = engine.decide(fields)
        # Promena postojece PJ ostavlja trag. Bez njega dokument moze da izadje
        # sa necijeg ekrana bez ikakvog zapisa - a upravo to se trazilo 08.10.
        # kod Systemsove 1386/26: ni jedan dogadjaj, pa se nije znalo da li je
        # neko nesto uradio ili je app presla preko toga. Prvo razvrstavanje se
        # ne belezi (to je redovan posao), samo promena i gubljenje PJ.
        if doc.business_unit_id is not None and decision.business_unit_id != doc.business_unit_id:
            from ..models import AuditLog, BusinessUnit

            staro_ime = session.get(BusinessUnit, doc.business_unit_id)
            novo_ime = (
                session.get(BusinessUnit, decision.business_unit_id)
                if decision.business_unit_id
                else None
            )
            session.add(
                AuditLog(
                    actor="system",
                    action="routing.promena" if novo_ime is not None else "routing.izgubljena",
                    document_id=doc.id,
                    detail=(
                        f"{staro_ime.code if staro_ime else '?'} -> "
                        f"{novo_ime.code if novo_ime else 'nerazvrstano'}: "
                        f"{decision.note or 'nijedno pravilo'}"
                    )[:2000],
                )
            )
        doc.business_unit_id = decision.business_unit_id
        doc.routing_source = decision.source
        doc.routing_rule_id = decision.rule_id
        doc.routing_note = shorten(decision.note, 500)
        if decision.assigned:
            doc.state = ProcessState.ROUTED
            if decision.rule_id:
                from ..models import RoutingRule

                rule = session.get(RoutingRule, decision.rule_id)
                if rule:
                    rule.hits += 1
        else:
            doc.state = ProcessState.UNASSIGNED

    def ponovo_parsiraj(self, doc_id: int) -> bool:
        """Ponovo cita vec sacuvani UBL sa diska — bez obracanja SEF-u.

        Koristi se kad parser nauci novo polje: istorija je preuzeta, fajl je tu,
        pa nema razloga da se skida ponovo. Stavke se grade iznova, ali se ERP
        sifra koju je operater potvrdio vraca na svoju stavku — ona nije iz UBL-a
        i ponovno citanje je ne sme pojesti.

        Razvrstavanje se NE dira: ako novo polje treba da promeni odluku, to radi
        `razvrstaj`, posle merenja.
        """
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                raise ValueError(f"Dokument {doc_id} ne postoji.")
            from .arhiva import ubl_bajtovi

            sadrzaj = ubl_bajtovi(doc)
            if not sadrzaj:
                return False
            ubl = parse_ubl(sadrzaj)
            ranije = {
                line.line_no: (line.erp_item_code, line.erp_mapping_source)
                for line in doc.lines
                if line.erp_item_code
            }
            self._apply_ubl(session, doc, ubl)
            session.flush()
            for line in doc.lines:
                if line.line_no in ranije:
                    line.erp_item_code, line.erp_mapping_source = ranije[line.line_no]
            return True

    def reroute(self, doc_id: int, *, i_vracene: bool = False) -> RoutingSource:
        """Ponovo primenjuje pravila na postojeci dokument (posle izmene pravila).

        **Vraceni dokument se preskace.** Kad covek iz jedinice klikne „Nije za
        moju jedinicu“, dokument ostaje bez PJ i ceka operatera - a to je tacno
        stanje koje `razvrstaj` trazi, pa ga je prvo sledece razvrstavanje istim
        pravilom vracalo istom coveku. Bogdan je tako uporno dobijao LANGOS
        fakture koje nisu njegove (Bane, 08.10.: „ako Bogdan vrati operateru onda
        app mora rucno da gleda“).

        `i_vracene=True` je za slucaj kad se svesno zeli i njih preracunati -
        npr. posle ispravke pravila.
        """
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                raise ValueError(f"Dokument {doc_id} ne postoji.")
            if doc.routing_corrected and not i_vracene:
                return RoutingSource.NONE
            self._route(session, doc)
            return doc.routing_source

    # ------------------------------------------------------------------ #
    # sitnice
    # ------------------------------------------------------------------ #

    def _default_date_from(self, today: date) -> date:
        with session_scope() as session:
            last = session.get(Setting, LAST_SYNC_KEY)
            if last and last.value:
                try:
                    return date.fromisoformat(last.value) - timedelta(
                        days=self.settings.sync_overlap_days
                    )
                except ValueError:
                    pass
        if self.settings.sync_start_date:
            try:
                return date.fromisoformat(self.settings.sync_start_date)
            except ValueError:
                log.warning("SYNC_START_DATE nije validan datum: %s", self.settings.sync_start_date)
        return today - timedelta(days=30)

    @staticmethod
    def _set_setting(session: Session, key: str, value: str) -> None:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value

    def _mark_error(self, sef_invoice_id: Any, exc: Exception) -> None:
        if sef_invoice_id is None:
            return
        try:
            with session_scope() as session:
                doc = session.scalar(
                    select(Document).where(Document.sef_invoice_id == int(sef_invoice_id))
                )
                if doc is not None:
                    doc.state = ProcessState.ERROR
                    doc.error_message = str(exc)[:2000]
        except Exception:  # noqa: BLE001
            log.exception("Ne mogu da upisem gresku za dokument %s", sef_invoice_id)
