"""Radnje operatera i poslovodje nad dokumentom.

Tok je namerno u dva nezavisna koraka, jer se u praksi ne poklapaju uvek:

    prihvatanje/odbijanje na SEF-u   <-- pravni cin prema dobavljacu
    prosledjivanje poslovnoj jedinici <-- radni nalog nasoj kuci

Operater bira redosled. Dokument stoji u njegovom redu dok se oba ne zavrse.
Poslovodja potom potvrdjuje da je roba stvarno stigla.

`forward()` je mesto na koje se u fazi 2 kaci kreiranje ulazne kalkulacije:
isti klik koji obavesti poslovodju treba da mu spremi i kalkulaciju za knjizenje.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import (
    AuditLog,
    BusinessUnit,
    Document,
    MatchField,
    MatchOp,
    ProcessState,
    RoutingRule,
    RoutingSource,
    utcnow,
)
from ..textutil import shorten

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    ok: bool
    message: str
    done: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.problems:
            return f"{self.message} ({'; '.join(self.problems[:3])})"
        return self.message


class Workflow:
    def __init__(self, settings: Settings | None = None, notifier=None):
        self.settings = settings or get_settings()
        if notifier is None:
            from ..notify.dispatcher import Dispatcher

            notifier = Dispatcher(self.settings)
        self.notifier = notifier

    # ------------------------------------------------------------------ #
    # prosledjivanje
    # ------------------------------------------------------------------ #

    def forward(self, doc_ids: list[int], actor: str) -> ActionResult:
        """Salje dokumente poslovnim jedinicama. Nerazvrstani se preskacu."""
        spremni: list[int] = []
        problemi: list[str] = []

        with session_scope() as session:
            for doc_id in doc_ids:
                doc = session.get(Document, doc_id)
                if doc is None:
                    problemi.append(f"#{doc_id}: ne postoji")
                elif doc.business_unit_id is None:
                    problemi.append(f"{doc.document_number or doc_id}: nije razvrstan")
                else:
                    spremni.append(doc_id)

        poslato = 0
        for doc_id in spremni:
            if self._forward_one(doc_id, actor):
                poslato += 1
            else:
                problemi.append(f"#{doc_id}: slanje nije uspelo")

        if poslato == 0 and problemi:
            return ActionResult(False, "Ništa nije prosleđeno.", 0, problemi)
        return ActionResult(True, f"Prosleđeno: {poslato}.", poslato, problemi)

    def _forward_one(self, doc_id: int, actor: str) -> bool:
        ok = self.notifier.notify_document(doc_id)
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return False
            if ok:
                doc.forwarded_at = utcnow()
                doc.forwarded_by = actor
                doc.state = ProcessState.NOTIFIED
                # Prosledjivanje je ujedno potvrda razvrstavanja: niko ne salje
                # dokument objektu za koji misli da nije njegov.
                if doc.routing_confirmed_at is None:
                    doc.routing_confirmed_at = utcnow()
                    doc.routing_confirmed_by = actor
                    if doc.routing_rule_id:
                        pravilo = session.get(RoutingRule, doc.routing_rule_id)
                        if pravilo is not None:
                            pravilo.hits += 1
            session.add(
                AuditLog(
                    actor=actor,
                    action="document.forwarded" if ok else "document.forward.failed",
                    document_id=doc_id,
                    detail=f"PJ={doc.business_unit.code if doc.business_unit else '-'}",
                )
            )
        # FAZA 2: ovde ide kreiranje ulazne kalkulacije u ERP-u
        return ok

    # ------------------------------------------------------------------ #
    # potvrda prijema (poslovodja)
    # ------------------------------------------------------------------ #

    def confirm_receipt(self, doc_id: int, actor: str, note: str = "") -> ActionResult:
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return ActionResult(False, "Dokument ne postoji.")
            if doc.forwarded_at is None:
                return ActionResult(False, "Dokument ti još nije prosleđen.")
            if doc.received_at is not None:
                return ActionResult(False, f"Prijem je već potvrdio {doc.received_by}.")
            doc.received_at = utcnow()
            doc.received_by = actor
            doc.received_note = shorten(note, 500)
            session.add(
                AuditLog(actor=actor, action="document.received", document_id=doc_id, detail=note)
            )

        # operater koji je prosledio treba da zna da je roba stigla
        try:
            self.notifier.notify_receipt(doc_id, confirmed_by=actor)
        except Exception:  # noqa: BLE001 - potvrda vazi i ako mejl ne prodje
            log.exception("Obaveštenje o potvrdi prijema (dokument %s) nije poslato", doc_id)
        return ActionResult(True, "Prijem je potvrđen.", 1)

    # ------------------------------------------------------------------ #
    # razvrstavanje i arhiva
    # ------------------------------------------------------------------ #

    def assign_unit(
        self,
        doc_id: int,
        business_unit_id: int,
        actor: str,
        remember: bool = False,
        field_name: str | None = None,
    ) -> ActionResult:
        """Operater dodeljuje PJ. Kad menja predlog, to je gradja za ucenje."""
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return ActionResult(False, "Dokument ne postoji.")
            unit = session.get(BusinessUnit, business_unit_id)
            if unit is None:
                return ActionResult(False, "Poslovna jedinica ne postoji.")

            # Ispravka predloga: pravilo koje je pogresilo dobija promasaj, da bi
            # se na spisku pravila videlo koje ne valja. Brojka promasaja je
            # jedini pouzdan znak - pravilo ume da radi tacno godinu dana pa da
            # se objekat preseli.
            prethodna = doc.business_unit_id
            if prethodna is not None and prethodna != business_unit_id:
                doc.routing_corrected = True
                if doc.routing_rule_id:
                    krivo = session.get(RoutingRule, doc.routing_rule_id)
                    if krivo is not None:
                        krivo.misses += 1
                        session.add(
                            AuditLog(
                                actor=actor,
                                action="routing.rule.miss",
                                document_id=doc_id,
                                detail=f"pravilo #{krivo.id} ({krivo.field.value}) "
                                       f"dalo je pogresnu PJ",
                            )
                        )

            doc.business_unit_id = business_unit_id
            doc.routing_source = RoutingSource.MANUAL
            doc.routing_rule_id = None
            doc.routing_note = f"ručno dodelio {actor}"
            doc.routing_confirmed_at = utcnow()
            doc.routing_confirmed_by = actor
            if doc.state in (ProcessState.UNASSIGNED, ProcessState.FETCHED, ProcessState.NEW):
                doc.state = ProcessState.ROUTED
            session.add(
                AuditLog(actor=actor, action="routing.manual", document_id=doc_id,
                         detail=f"PJ={unit.code}")
            )

            poruka = f"Dodeljeno: {unit.code}."
            if remember:
                poruka += " " + self._remember_rule(session, doc, unit, field_name, actor)
        return ActionResult(True, poruka, 1)

    def confirm_routing(self, doc_ids: list[int], actor: str) -> ActionResult:
        """Operater potvrdjuje da je predlog tacan, bez menjanja icega.

        Potvrda je jedini podatak koji razlikuje "razvrstano tacno" od
        "niko jos nije pogledao" - bez nje se tacnost ne moze meriti.
        """
        potvrdjeno = 0
        with session_scope() as session:
            for doc_id in doc_ids:
                doc = session.get(Document, doc_id)
                if doc is None or doc.business_unit_id is None:
                    continue
                if doc.routing_confirmed_at is not None:
                    continue
                doc.routing_confirmed_at = utcnow()
                doc.routing_confirmed_by = actor
                if doc.routing_rule_id:
                    pravilo = session.get(RoutingRule, doc.routing_rule_id)
                    if pravilo is not None:
                        pravilo.hits += 1
                potvrdjeno += 1
        return ActionResult(True, f"Potvrđeno razvrstavanje: {potvrdjeno}.", potvrdjeno)

    # Redom po pouzdanosti: adresa isporuke je najjaci trag, PIB dobavljaca
    # najsiri. Bira se prvo polje koje na ovom dokumentu uopste postoji.
    REDOSLED_POLJA = (
        MatchField.DELIVERY_ADDRESS,
        MatchField.DELIVERY_NAME,
        MatchField.ORDER_REFERENCE,
        MatchField.BUYER_REFERENCE,
        MatchField.CONTRACT_REFERENCE,
        MatchField.SUPPLIER_VAT,
    )

    @classmethod
    def predlozi_polje(cls, doc: Document) -> MatchField | None:
        """Po kom podatku ovaj dokument uopste moze da se prepozna sledeci put."""
        for polje in cls.REDOSLED_POLJA:
            if getattr(doc, polje.value, None):
                return polje
        return None

    @classmethod
    def _remember_rule(
        cls,
        session: Session,
        doc: Document,
        unit: BusinessUnit,
        field_name: str | None,
        actor: str,
    ) -> str:
        if field_name:
            try:
                field = MatchField(field_name)
            except ValueError:
                return "Pravilo nije napravljeno (nepoznato polje)."
        else:
            field = cls.predlozi_polje(doc)
            if field is None:
                return (
                    "Pravilo nije napravljeno — dokument nema nijedan podatak "
                    "po kom bi se prepoznao sledeći put."
                )
        value = getattr(doc, field.value, None)
        if not value:
            return f"Pravilo nije napravljeno — polje {field.value} je prazno."

        postoji = session.scalar(
            select(RoutingRule).where(
                RoutingRule.field == field,
                RoutingRule.pattern == str(value),
                RoutingRule.business_unit_id == unit.id,
            )
        )
        if postoji is not None:
            return "Takvo pravilo već postoji."
        session.add(
            RoutingRule(
                priority=50,
                field=field,
                op=MatchOp.CONTAINS,
                pattern=str(value)[:500],
                business_unit_id=unit.id,
                comment=f"napravio {actor} sa dokumenta {doc.document_number}",
            )
        )
        return f"Zapamćeno pravilo: {field.value} = „{str(value)[:60]}”."

    def archive(self, doc_ids: list[int], actor: str, reason: str = "") -> ActionResult:
        with session_scope() as session:
            broj = 0
            for doc_id in doc_ids:
                doc = session.get(Document, doc_id)
                if doc is None or doc.archived:
                    continue
                doc.archived = True
                broj += 1
                session.add(
                    AuditLog(actor=actor, action="document.archived", document_id=doc_id,
                             detail=reason)
                )
        return ActionResult(True, f"Arhivirano: {broj}.", broj)

    def unarchive(self, doc_id: int, actor: str) -> ActionResult:
        with session_scope() as session:
            doc = session.get(Document, doc_id)
            if doc is None:
                return ActionResult(False, "Dokument ne postoji.")
            doc.archived = False
            session.add(AuditLog(actor=actor, action="document.unarchived", document_id=doc_id))
        return ActionResult(True, "Vraćeno u red.", 1)
