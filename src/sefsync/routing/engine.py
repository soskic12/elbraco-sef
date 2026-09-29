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

from ..models import (
    BusinessUnit,
    DocumentType,
    MatchField,
    MatchOp,
    RoutingRule,
    RoutingSource,
    UnitKind,
)
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


# Magacin u koji ide roba koju je kupac vratio.
MAGACIN_POVRACAJ = "010"


class RoutingEngine:
    """Ucitava pravila jednom i primenjuje ih na vise dokumenata."""

    def __init__(
        self,
        rules: list[RoutingRule],
        single_unit_id: int | None = None,
        *,
        maloprodaje: set[int] | None = None,
        povracaj_id: int | None = None,
    ):
        self.rules = sorted(rules, key=lambda r: (r.priority, r.id or 0))
        self.single_unit_id = single_unit_id
        # Knjizno odobrenje nikad ne zavrsi u prodavnici (mereno: od 923 odobrenja
        # 568 je na 010, 355 na 088, nijedno drugde). Roba koju je kupac vratio
        # prelazi sa stanja objekta u magacin 010, gde je odobrenje ponistava -
        # pa pravilo po adresi isporuke, koje pokazuje na objekat, za odobrenje
        # znaci "povracaj iz tog objekta", a ne "dokument pripada objektu".
        self.maloprodaje = maloprodaje or set()
        self.povracaj_id = povracaj_id

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
        maloprodaje = set(
            session.scalars(
                select(BusinessUnit.id).where(BusinessUnit.kind == UnitKind.RETAIL)
            )
        )
        povracaj = session.scalar(
            select(BusinessUnit.id).where(BusinessUnit.erp_code == MAGACIN_POVRACAJ)
        )
        return cls(
            rules,
            single_unit_id=units[0] if len(units) == 1 else None,
            maloprodaje=maloprodaje,
            povracaj_id=povracaj,
        )

    def _ispravi_odobrenje(self, unit_id: int, doc_type: str) -> tuple[int, str]:
        """Odobrenje razvrstano u prodavnicu zapravo ide na magacin povracaja."""
        if (
            doc_type == DocumentType.CREDIT_NOTE.value
            and self.povracaj_id is not None
            and unit_id in self.maloprodaje
        ):
            return self.povracaj_id, " (odobrenje iz objekta → magacin povraćaja)"
        return unit_id, ""

    def pogodjena(self, fields: dict[str, str | None]):
        """Sva pravila koja pogadjaju dokument, redom kako ih motor gleda.

        Postoji da bi pitanje „zasto je otislo tu“ imalo odgovor iz ISTOG
        koda koji i odlucuje. Prva verzija objasnjenja imala je svoju kopiju
        uslova i u njoj ispustila proveru vrste dokumenta, pa je prikazivala
        pravila koja motor nikad ne bi pogledao - i na osnovu toga sam izveo
        pogresan zakljucak (29.09.). Dve kopije istog uslova se raziđu.
        """
        normalized = {k: normalize(v) for k, v in fields.items() if k != "document_type"}
        # Tekst PDF priloga NE ulazi u any_text: on sadrzi i nasu adresu sedista,
        # nazive artikala i zaglavlje dobavljaca, pa siri svako "any_text" pravilo
        # do lazno pozitivnih. Mereno: tacnost pada sa 97,4% na 91,8%.
        # Prilog se koristi samo kad pravilo izricito cilja attachment_text.
        # Broj ispravljene fakture (billing_reference) je takodje van any_text:
        # on ne moze da imenuje odrediste, a unosi nove cifre u koje bi se uhvatila
        # postojeca any_text pravila. Koristi se samo kad pravilo cilja to polje.
        _van_any_text = {
            MatchField.ATTACHMENT_TEXT.value,
            MatchField.BILLING_REFERENCE.value,
            MatchField.BILLING_REFERENCE_KIND.value,
        }
        any_text = " ".join(v for k, v in normalized.items() if v and k not in _van_any_text)
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
                if is_any:
                    # NAPOMENA: attachment_text ovde jos uvek ulazi u any_text, za
                    # razliku od ostalih operatora iznad. Razlika je zatecena, a
                    # ispravka bi promenila odluke postojecih regex pravila, pa ceka
                    # merenje naspram ERP-a. Novo polje se u tu nedoslednost ne dodaje.
                    raw_values = [
                        v for k, v in fields.items()
                        if k not in (MatchField.BILLING_REFERENCE.value,
                                     MatchField.BILLING_REFERENCE_KIND.value)
                    ]
                else:
                    raw_values = [fields.get(rule.field.value)]
                subject = " ".join(v for v in raw_values if v)
            else:
                subject = haystack
            if _matches(rule.op, subject, rule.pattern):
                yield rule

    def decide(self, fields: dict[str, str | None]) -> RoutingDecision:
        """`fields` je `UblDocument.routing_fields()` (ili isti oblik iz baze)."""
        doc_type = (fields.get("document_type") or "").strip()
        for rule in self.pogodjena(fields):
            source = (
                RoutingSource.SUPPLIER_DEFAULT
                if rule.field is MatchField.SUPPLIER_VAT
                else RoutingSource.RULE
            )
            jedinica, dodatak = self._ispravi_odobrenje(rule.business_unit_id, doc_type)
            return RoutingDecision(
                business_unit_id=jedinica,
                source=source,
                rule_id=rule.id,
                note=f"pravilo #{rule.id}: {rule.field.value} {rule.op.value} "
                     f"'{rule.pattern}'{dodatak}",
            )

        if self.single_unit_id is not None:
            return RoutingDecision(
                self.single_unit_id, RoutingSource.SINGLE_UNIT, None, "jedina aktivna poslovna jedinica"
            )
        return UNASSIGNED
