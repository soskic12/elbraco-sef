"""Sastavljanje i slanje obavestenja poslovnoj jedinici."""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import (
    BusinessUnit,
    Document,
    DocumentType,
    Notification,
    NotifyChannel,
    NotifyStatus,
    ProcessState,
    utcnow,
)
from .base import Message, SendResult
from .email_channel import EmailChannel
from .push_channel import PushChannel

log = logging.getLogger(__name__)

TYPE_LABEL = {
    DocumentType.INVOICE: "Faktura",
    DocumentType.CREDIT_NOTE: "Knjižno odobrenje",
    DocumentType.DEBIT_NOTE: "Knjižno zaduženje",
    DocumentType.PREPAYMENT: "Avansni račun",
    DocumentType.OTHER: "Dokument",
}


def money(value: float | None, currency: str | None = "RSD") -> str:
    if value is None:
        return "-"
    return f"{value:,.2f} {currency or ''}".replace(",", " ").replace(".", ",", 1).strip()


class Dispatcher:
    def __init__(
        self,
        settings: Settings | None = None,
        email: EmailChannel | None = None,
        push: PushChannel | None = None,
    ):
        self.settings = settings or get_settings()
        self.email = email or EmailChannel(self.settings)
        self.push = push or PushChannel(self.settings)

    # ------------------------------------------------------------------ #

    def notify_document(self, doc_id: int) -> bool:
        """Salje obavestenje za dokument. Vraca True ako je bar jedan kanal uspeo."""
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return False
            unit = doc.business_unit
            message = self.build_message(doc, unit)
            emails = unit.email_list() if unit else self._fallback_emails()
            phones = unit.phone_list() if unit else []
            proba = self.settings.notify_override_to.strip()
            if proba:
                # poruka nosi oznaku kome bi u redovnom radu otisla
                message.subject = f"[PROBA → {unit.code if unit else 'NERAZVRSTANO'}] {message.subject}"
                emails = [x.strip() for x in proba.split(",") if x.strip()]
                phones = []
                log.info("Probni režim: obaveštenje za dokument %s ide na %s", doc_id, emails)
            meta = {
                "document_id": doc.id,
                "sef_invoice_id": doc.sef_invoice_id,
                "unit": unit.code if unit else None,
            }
            unit_id = unit.id if unit else None

        sent_any = False
        records: list[tuple[NotifyChannel, SendResult]] = []

        for result in self.email.send(emails, message):
            records.append((NotifyChannel.EMAIL, result))
            sent_any = sent_any or result.ok
        for result in self.push.send(phones, message, meta):
            records.append((NotifyChannel.PUSH, result))
            sent_any = sent_any or result.ok

        with session_scope() as session:
            doc = session.get(Document, doc_id)
            for channel, result in records:
                session.add(
                    Notification(
                        document_id=doc_id,
                        business_unit_id=unit_id,
                        channel=channel,
                        target=result.target,
                        status=NotifyStatus.SENT if result.ok else NotifyStatus.FAILED,
                        error=result.error,
                        attempts=1,
                        sent_at=utcnow() if result.ok else None,
                    )
                )
            # dashboard je uvek "dostavljen" - dokument je vidljiv u aplikaciji
            target_unit = session.get(BusinessUnit, unit_id) if unit_id is not None else None
            session.add(
                Notification(
                    document_id=doc_id,
                    business_unit_id=unit_id,
                    channel=NotifyChannel.DASHBOARD,
                    target=target_unit.code if target_unit else "operater",
                    status=NotifyStatus.SENT,
                    sent_at=utcnow(),
                )
            )
            if doc is not None:
                doc.notified_at = utcnow()
                if sent_any and doc.state is ProcessState.ROUTED:
                    doc.state = ProcessState.NOTIFIED
        return sent_any

    def notify_receipt(self, doc_id: int, confirmed_by: str) -> bool:
        """Javlja operateru koji je prosledio da je poslovođa potvrdila prijem.

        Adresa se traži u istom šifarniku osoblja iz kog ide i prijava, pa nema
        drugog spiska mejlova koji bi zastarevao. Kad je čovek bez upisanog
        mejla, poruka ide na rezervnu adresu (NOTIFY_BCC).
        """
        from ..erp.staff import find_by_login

        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return False
            unit = doc.business_unit
            unit_name = f"{unit.code} — {unit.name}" if unit else "?"
            potvrdio = find_by_login(confirmed_by)
            ime = potvrdio["ime"] if potvrdio else confirmed_by
            link = f"{self.settings.public_base_url.rstrip('/')}/dokument/{doc.id}"
            napomena = doc.received_note or ""
            poruka = Message(
                subject=(
                    f"Prijem potvrđen · {unit_name} · "
                    f"{TYPE_LABEL.get(doc.document_type, 'Dokument')} "
                    f"{doc.document_number or doc.sef_invoice_id}"
                ),
                body_text="\n".join(
                    [
                        f"{ime} je potvrdio/la prijem robe.",
                        "",
                        f"Poslovna jedinica: {unit_name}",
                        f"Dokument: {doc.document_number or doc.sef_invoice_id}",
                        f"Dobavljač: {doc.supplier_name or '-'}",
                        f"Iznos: {money(doc.amount, doc.currency)}",
                        f"Napomena: {napomena or '-'}",
                        "",
                        link,
                    ]
                ),
                body_html=(
                    f"<p><b>{escape(ime)}</b> je potvrdio/la prijem robe.</p>"
                    f"<p>{escape(unit_name)} · {escape(doc.document_number or '')} · "
                    f"{escape(doc.supplier_name or '-')} · "
                    f"{escape(money(doc.amount, doc.currency))}</p>"
                    + (f"<p>Napomena: {escape(napomena)}</p>" if napomena else "")
                    + f'<p><a href="{escape(link)}">Otvori dokument</a></p>'
                ),
                short_text=f"Prijem potvrđen: {unit_name} · {doc.document_number or ''} ({ime})",
            )
            primaoci = self._operator_emails(doc.forwarded_by)

        proba = self.settings.notify_override_to.strip()
        if proba:
            primaoci = [x.strip() for x in proba.split(",") if x.strip()]

        ishodi = self.email.send(primaoci, poruka)
        uspeh = any(i.ok for i in ishodi)
        with session_scope() as session:
            for ishod in ishodi:
                session.add(
                    Notification(
                        document_id=doc_id,
                        channel=NotifyChannel.EMAIL,
                        target=ishod.target,
                        status=NotifyStatus.SENT if ishod.ok else NotifyStatus.FAILED,
                        error=ishod.error,
                        attempts=1,
                        sent_at=utcnow() if ishod.ok else None,
                    )
                )
        return uspeh

    def _operator_emails(self, login: str | None) -> list[str]:
        from ..erp.staff import email_for_login

        if login:
            adresa = email_for_login(login)
            if adresa:
                return [adresa]
        return self._fallback_emails()

    def retry_failed(self, limit: int = 50) -> int:
        """Ponovo salje obavestenja za dokumente ciji su svi pokusaji pali."""
        with session_scope() as session:
            failed_docs = [
                n.document_id
                for n in session.query(Notification)
                .filter(Notification.status == NotifyStatus.FAILED)
                .order_by(Notification.id.desc())
                .limit(limit)
            ]
        done = 0
        for doc_id in dict.fromkeys(failed_docs):
            if self.notify_document(doc_id):
                done += 1
        return done

    # ------------------------------------------------------------------ #

    def _fallback_emails(self) -> list[str]:
        """Nerazvrstani dokumenti idu operateru (BCC adresa ili posiljalac)."""
        raw = self.settings.notify_bcc or self.settings.smtp_from
        return [x.strip() for x in raw.split(",") if x.strip()]

    def build_message(self, doc: Document, unit: BusinessUnit | None) -> Message:
        label = TYPE_LABEL.get(doc.document_type, "Dokument")
        unit_name = f"{unit.code} - {unit.name}" if unit else "NERAZVRSTANO"
        prefix = "" if unit else "[NERAZVRSTANO] "
        subject = (
            f"{prefix}{label} {doc.document_number or doc.sef_invoice_id} — "
            f"{doc.supplier_name or 'nepoznat dobavljač'} — {money(doc.amount, doc.currency)}"
        )
        link = f"{self.settings.public_base_url.rstrip('/')}/dokument/{doc.id}"

        rows = [
            ("Poslovna jedinica", unit_name),
            ("Vrsta dokumenta", label),
            ("Broj dokumenta", doc.document_number or "-"),
            ("Dobavljač", f"{doc.supplier_name or '-'} (PIB {doc.supplier_vat or '-'})"),
            ("Datum prometa", doc.delivery_date.isoformat() if doc.delivery_date else "-"),
            ("Rok plaćanja", doc.due_date.isoformat() if doc.due_date else "-"),
            ("Osnovica", money(doc.sum_without_vat, doc.currency)),
            ("PDV", money(doc.vat_amount, doc.currency)),
            ("Za plaćanje", money(doc.amount, doc.currency)),
            ("Adresa isporuke", doc.delivery_address or doc.delivery_name or "-"),
            ("Status na SEF-u", doc.sef_status.value),
            ("SEF ID", str(doc.sef_invoice_id)),
        ]
        if doc.routing_note:
            rows.append(("Razvrstano po", doc.routing_note))

        lines_preview = [
            f"  {ln.line_no:>3}. {ln.name or '-'}  |  {ln.quantity or 0:g} {ln.unit_code or ''}"
            f"  |  {money(ln.line_amount, doc.currency)}"
            for ln in doc.lines[:15]
        ]
        if len(doc.lines) > 15:
            lines_preview.append(f"  ... i još {len(doc.lines) - 15} stavki")

        text_parts = [f"{k}: {v}" for k, v in rows]
        if lines_preview:
            text_parts += ["", f"Stavke ({len(doc.lines)}):", *lines_preview]
        if not unit:
            text_parts += [
                "",
                "Dokument nije automatski razvrstan na poslovnu jedinicu - "
                "potrebno je rucno dodeliti PJ u aplikaciji.",
            ]
        text_parts += ["", f"Detalji i prihvatanje: {link}"]
        body_text = "\n".join(text_parts)

        table = "".join(
            f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{escape(k)}</td>"
            f"<td style='padding:4px 0'><b>{escape(str(v))}</b></td></tr>"
            for k, v in rows
        )
        items = "".join(
            f"<tr><td style='padding:2px 8px 2px 0'>{ln.line_no}</td>"
            f"<td style='padding:2px 8px 2px 0'>{escape(ln.name or '-')}</td>"
            f"<td style='padding:2px 8px 2px 0;text-align:right'>{ln.quantity or 0:g} {escape(ln.unit_code or '')}</td>"
            f"<td style='padding:2px 0;text-align:right'>{escape(money(ln.line_amount, doc.currency))}</td></tr>"
            for ln in doc.lines[:15]
        )
        body_html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">
<h2 style="margin:0 0 12px">{escape(label)} {escape(doc.document_number or '')}</h2>
<table>{table}</table>
{'<h3 style="margin:16px 0 6px">Stavke</h3><table>' + items + '</table>' if items else ''}
<p style="margin-top:16px"><a href="{escape(link)}"
   style="background:#0b5cad;color:#fff;padding:8px 14px;border-radius:4px;text-decoration:none">
   Otvori dokument</a></p>
</body></html>"""

        short = (
            f"{prefix}{label} {doc.document_number or ''} | {doc.supplier_name or ''} | "
            f"{money(doc.amount, doc.currency)} | {unit_name} | {link}"
        )[:320]

        attachments: list[Path] = []
        for path_str in (doc.ubl_path, doc.pdf_path):
            if path_str:
                path = Path(path_str)
                if path.exists():
                    attachments.append(path)

        return Message(
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            short_text=short,
            attachments=attachments,
        )
