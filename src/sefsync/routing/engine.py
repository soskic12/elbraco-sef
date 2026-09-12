"""Razvrstavanje dokumenata na poslovne jedinice.

Mehanizam je namerno tabelaran (pravila u bazi) a ne zakucan u kodu: dok se ne
vidi sta dobavljaci stvarno popunjavaju u UBL-u (vidi `sefsync analyze`),
pravila ce se menjati cesto i to mora da radi operater kroz dashboard.

Redosled odlucivanja:
  1. aktivna pravila po prioritetu (manji broj = ranije)
  2. ako firma ima tacno jednu aktivnu PJ - sve ide na nju
  3. UNASSIGNED - dokument ide u red za rucno razvrstavanje
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessUnit, MatchField, MatchOp, RoutingRule, RoutingSource
from ..textutil import normalize

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RoutingDecision:
    business_unit_id: int | None
    source: RoutingSource
    rule_id: int | None = None
    note: str | None = None

    @property
    def assigned(self) -> bool:
        return self.business_unit_id is not None


UNASSIGNED = RoutingDecision(None, RoutingSource.NONE, None, "Nije prepoznata poslovna jedinica")


def _matches(op: MatchOp, haystack: str, pattern: str) -> bool:
    if not pattern:
        return False
    if op is MatchOp.REGEX:
        try:
            return re.search(pattern, haystack, re.IGNORECASE) is not None
        except re.error as exc:
            log.warning("Neispravan regex u pravilu (%s): %s", pattern, exc)
            return False
    norm_pattern = normalize(pattern)
    if not norm_pattern:
        return False
    if op is MatchOp.EQUALS:
        return haystack == norm_pattern
    if op is MatchOp.STARTSWITH:
        return haystack.startswith(norm_pattern)
    return norm_pattern in haystack  # CONTAINS


class RoutingEngine:
    """Ucitava pravila jednom i primenjuje ih na vise dokumenata."""

    def __init__(self, rules: list[RoutingRule], single_unit_id: int | None = None):
        self.rules = sorted(rules, key=lambda r: (r.priority, r.id or 0))
        self.single_unit_id = single_unit_id

    @classmethod
    def from_db(cls, session: Session) -> RoutingEngine:
        rules = list(session.scalars(select(RoutingRule).where(RoutingRule.active == True)))
        units = list(
            session.scalars(
                select(BusinessUnit.id).where(
                    BusinessUnit.active == True, BusinessUnit.routable == True
                )
            )
        )
        return cls(rules, single_unit_id=units[0] if len(units) == 1 else None)

    def decide(self, fields: dict[str, str | None]) -> RoutingDecision:
        """`fields` je `UblDocument.routing_fields()` (ili isti oblik iz baze)."""
        normalized = {k: normalize(v) for k, v in fields.items() if k != "document_type"}
        any_text = " ".join(v for v in normalized.values() if v)
        supplier_vat = (fields.get("supplier_vat") or "").strip()

        doc_type = (fields.get("document_type") or "").strip()
        for rule in self.rules:
            if rule.supplier_vat and rule.supplier_vat.strip() != supplier_vat:
                continue
            if rule.document_type is not None and rule.document_type.value != doc_type:
                continue
            is_any = rule.field is MatchField.ANY_TEXT
            haystack = any_text if is_any else normalized.get(rule.field.value, "")
            if not haystack:
                continue
            if rule.op is MatchOp.REGEX:
                # regex ide nad originalnim tekstom - interpunkcija i mala slova su bitni
                raw_values = fields.values() if is_any else [fields.get(rule.field.value)]
                subject = " ".join(v for v in raw_values if v)
            else:
                subject = haystack
            if _matches(rule.op, subject, rule.pattern):
                source = (
                    RoutingSource.SUPPLIER_DEFAULT
                    if rule.field is MatchField.SUPPLIER_VAT
                    else RoutingSource.RULE
                )
                return RoutingDecision(
                    business_unit_id=rule.business_unit_id,
                    source=source,
                    rule_id=rule.id,
                    note=f"pravilo #{rule.id}: {rule.field.value} {rule.op.value} '{rule.pattern}'",
                )

        if self.single_unit_id is not None:
            return RoutingDecision(
                self.single_unit_id, RoutingSource.SINGLE_UNIT, None, "jedina aktivna poslovna jedinica"
            )
        return UNASSIGNED
