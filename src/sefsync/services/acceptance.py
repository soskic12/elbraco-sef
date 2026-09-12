"""Prihvatanje i odbijanje ulaznih dokumenata na SEF-u.

Prihvatanje je pravno relevantna radnja, pa su ovde zastitne kocnice:
  - automatsko prihvatanje je podrazumevano ISKLJUCENO (AUTO_ACCEPT=off),
  - kad je ukljuceno, moze da vazi samo za razvrstane dokumente,
  - moze da se odlozi za N sati i ogranici iznosom,
  - svaka radnja se upisuje u audit log sa korisnikom koji ju je pokrenuo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import AuditLog, Document, ProcessState, SefStatus, utcnow
from ..sef.client import SefClient, SefError

log = logging.getLogger(__name__)


@dataclass
class AcceptResult:
    ok: bool
    message: str
    status: str | None = None


class AcceptanceService:
    def __init__(self, client: SefClient | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = client or SefClient(self.settings)

    # ------------------------------------------------------------------ #

    def accept(self, doc_id: int, actor: str = "system", comment: str = "") -> AcceptResult:
        return self._decide(doc_id, accepted=True, comment=comment, actor=actor)

    def reject(self, doc_id: int, actor: str, comment: str) -> AcceptResult:
        if not comment.strip():
            return AcceptResult(False, "Razlog odbijanja je obavezan.")
        return self._decide(doc_id, accepted=False, comment=comment, actor=actor)

    def _decide(self, doc_id: int, accepted: bool, comment: str, actor: str) -> AcceptResult:
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return AcceptResult(False, f"Dokument {doc_id} ne postoji.")
            if doc.sef_status in (SefStatus.APPROVED, SefStatus.REJECTED):
                return AcceptResult(
                    False, f"Dokument je vec u statusu {doc.sef_status.value} na SEF-u.",
                    doc.sef_status.value,
                )
            sef_id = doc.sef_invoice_id
            number = doc.document_number

        try:
            response = self.client.accept_reject(sef_id, accepted, comment)
        except (SefError, ValueError) as exc:
            log.warning("SEF je odbio zahtev za %s: %s", sef_id, exc)
            with session_scope() as session:
                session.add(
                    AuditLog(
                        actor=actor,
                        action="sef.accept.failed" if accepted else "sef.reject.failed",
                        document_id=doc_id,
                        detail=str(exc)[:2000],
                    )
                )
            return AcceptResult(False, f"SEF greska: {exc}")

        invoice = response.get("Invoice") or response.get("invoice") or {}
        new_status = invoice.get("Status") or invoice.get("status")
        success = bool(response.get("Success", response.get("success", True)))

        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is not None:
                if accepted:
                    doc.sef_status = SefStatus.APPROVED
                    doc.state = ProcessState.ACCEPTED
                else:
                    doc.sef_status = SefStatus.REJECTED
                    doc.state = ProcessState.REJECTED
                doc.accepted_at = utcnow()
                doc.accepted_by = actor
            session.add(
                AuditLog(
                    actor=actor,
                    action="sef.accept" if accepted else "sef.reject",
                    document_id=doc_id,
                    detail=f"{number}: {comment}"[:2000],
                )
            )
        action = "prihvacen" if accepted else "odbijen"
        log.info("Dokument %s (%s) %s na SEF-u od strane %s", sef_id, number, action, actor)
        return AcceptResult(success, f"Dokument je {action} na SEF-u.", new_status)

    # ------------------------------------------------------------------ #

    def maybe_auto_accept(self, doc_id: int) -> bool:
        """Primenjuje politiku iz konfiguracije. Vraca True ako je prihvaceno."""
        policy = self.settings.auto_accept
        if policy == "off":
            return False

        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return False
            if doc.sef_status in (SefStatus.APPROVED, SefStatus.REJECTED, SefStatus.STORNO):
                return False
            if policy == "routed" and doc.business_unit_id is None:
                return False
            limit = self.settings.auto_accept_max_amount
            if limit is not None and (doc.amount or 0) > limit:
                log.info("Auto-prihvatanje preskoceno (iznos %s > %s)", doc.amount, limit)
                return False
            delay = self.settings.auto_accept_delay_hours
            if delay:
                reference = doc.sent_date or doc.created_at
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=delay)
                if reference and reference > cutoff:
                    return False

        result = self.accept(doc_id, actor="auto", comment="Automatsko prihvatanje (ERP integracija)")
        return result.ok
