"""Automatsko predlaganje pravila razvrstavanja iz uzorka UBL-ova.

Uparuje adrese isporuke koje dobavljaci stvarno salju sa sifarnikom poslovnih
jedinica, redom po pouzdanosti:

  1. ADRESA  - ulica i broj poslovne jedinice se nalaze u adresi isporuke
  2. PTT     - postanski broj iz adrese isporuke je jedinstven za jednu PJ
  3. NAZIV   - naziv lokacije isporuke (DeliveryParty) prepoznaje PJ
  4. GRAD    - u tom gradu postoji tacno jedna PJ

Kad u istom mestu postoji vise jedinica (npr. Sombor: magacin + dve
prodavnice), a adresa se ne poklapa ni sa jednom, predlog se ne pravi nego se
prijavljuje KONFLIKT sa spiskom kandidata - to je pitanje za coveka, ne za
heuristiku.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessUnit, MatchField, MatchOp, RoutingRule
from ..textutil import normalize
from ..ubl.parser import UblDocument

log = logging.getLogger(__name__)

# prioriteti pravila po nacinu uparivanja (manji broj = ranije)
PRIORITY = {"adresa": 20, "ptt": 30, "naziv": 35, "grad": 40}

_STREET_NOISE = re.compile(r"\b(UL|ULICA|BR|BB|BROJ)\b")
# u ERP-u adresa nosi rep sa telefonom: "STAPARSKI PUT S16 tel.421 783"
_PHONE_TAIL = re.compile(r"\bTEL\b.*$")


def _street_key(value: str | None) -> str:
    """'Ul. Somborska br. 28 tel.421 783' -> 'SOMBORSKA 28'."""
    text = normalize(value)
    text = _PHONE_TAIL.sub(" ", text)
    text = _STREET_NOISE.sub(" ", text)
    return " ".join(text.split())


def clean_address(value: str | None) -> str:
    """Adresa jedinice bez repa sa telefonom, u originalnom pisanju.

    Koristi se kao sablon pravila: kratko 'Staparski put S16' hvata sve varijante
    koje dobavljaci pisu oko njega (sa PTT-om, bez njega, sa 'ul.' ispred).
    """
    if not value:
        return ""
    text = re.split(r"(?i)\btel\b", value)[0]
    return " ".join(text.replace(",", " ").split()).strip(" .,-")


@dataclass
class Suggestion:
    field: MatchField
    pattern: str                    # tekst kakav dobavljac zaista salje
    count: int                      # koliko dokumenata iz uzorka pokriva
    unit_code: str | None = None
    unit_name: str | None = None
    business_unit_id: int | None = None
    how: str = "grad"
    candidates: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.business_unit_id is not None

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.how, 50)


@dataclass
class SuggestReport:
    suggestions: list[Suggestion] = field(default_factory=list)
    conflicts: list[Suggestion] = field(default_factory=list)
    without_delivery: int = 0
    supplier_hints: list[tuple[str, str, int]] = field(default_factory=list)
    documents: int = 0

    @property
    def covered(self) -> int:
        return sum(s.count for s in self.suggestions)

    def to_text(self) -> str:
        out = [f"Uzorak: {self.documents} dokumenata", ""]
        out.append(f"PREDLOZI PRAVILA ({len(self.suggestions)}, pokrivaju {self.covered} dokumenata)")
        out.append("-" * 78)
        for s in sorted(self.suggestions, key=lambda x: (x.priority, -x.count)):
            out.append(
                f"{s.count:>4} dok.  [{s.how:<6}] {s.field.value:<17} "
                f"{s.pattern[:38]:<38} -> {s.unit_code} {s.unit_name or ''}"
            )
        if self.conflicts:
            out.append("")
            out.append(f"KONFLIKTI ({len(self.conflicts)}) — traže odluku, pravilo se ne pravi")
            out.append("-" * 78)
            for c in self.conflicts:
                out.append(f"{c.count:>4} dok.  {c.pattern}")
                out.append(f"           kandidati: {', '.join(c.candidates)}")
        if self.without_delivery:
            out.append("")
            out.append(
                f"BEZ PODATKA O ISPORUCI: {self.without_delivery} dokumenata "
                f"({100.0 * self.without_delivery / max(1, self.documents):.0f}%)"
            )
            out.append("-" * 78)
            out.append("Za ove treba pravilo po dobavljaču (supplier_vat -> PJ). Najčešći:")
            for name, vat, count in self.supplier_hints[:15]:
                out.append(f"{count:>4} dok.  PIB {vat:<12} {name[:48]}")
        return "\n".join(out)


# --------------------------------------------------------------------------- #


class _UnitIndex:
    """Pretraga poslovnih jedinica po adresi, PTT-u, nazivu i gradu."""

    def __init__(self, units: list[BusinessUnit]):
        self.units = units
        self.by_street: dict[str, BusinessUnit] = {}
        self.by_postal: dict[str, list[BusinessUnit]] = {}
        self.by_city: dict[str, list[BusinessUnit]] = {}
        for unit in units:
            street = _street_key(unit.address)
            if street:
                self.by_street.setdefault(street, unit)
            if unit.postal_code:
                self.by_postal.setdefault(normalize(unit.postal_code), []).append(unit)
            if unit.city:
                self.by_city.setdefault(normalize(unit.city), []).append(unit)

    @staticmethod
    def _wrong_place(unit: BusinessUnit, postal: str | None, city: str | None) -> bool:
        """Ista ulica i broj postoje u vise mesta (Brace Radic 43 u Subotici i u Lalicu).

        Poklapanje ulice se odbacuje tek ako se NI postanski broj NI grad ne slazu:
        dobavljaci za isto mesto umeju da pisu razlicite PTT-ove (Sombor 25000/25101),
        a nazivi mesta se pisu skraceno ("B.Palanka" vs "Bačka Palanka").
        """
        postal_known = bool(unit.postal_code and postal)
        city_known = bool(unit.city and city)
        if postal_known and normalize(unit.postal_code) == normalize(postal):
            return False
        if city_known and normalize(unit.city) == normalize(city):
            return False
        return postal_known or city_known

    def match(
        self, address: str | None, postal: str | None, city: str | None, location_name: str | None
    ) -> tuple[BusinessUnit | None, str, list[BusinessUnit]]:
        address_key = _street_key(address)
        if address_key:
            compact = address_key.replace(" ", "")
            for street, unit in self.by_street.items():
                if not street:
                    continue
                # "S-16" i "S16" su ista adresa - poredi se i oblik bez razmaka
                hit = street in address_key or street.replace(" ", "") in compact
                if hit and not self._wrong_place(unit, postal, city):
                    return unit, "adresa", []

        in_postal = self.by_postal.get(normalize(postal), []) if postal else []
        if len(in_postal) == 1:
            return in_postal[0], "ptt", []

        if location_name:
            name_key = normalize(location_name)
            hits = [u for u in self.units if normalize(u.name) and normalize(u.name) in name_key]
            if len(hits) == 1:
                return hits[0], "naziv", []

        in_city = self.by_city.get(normalize(city), []) if city else []
        if len(in_city) == 1:
            return in_city[0], "grad", []

        return None, "konflikt", in_postal or in_city


def suggest_rules(session: Session, documents: list[UblDocument]) -> SuggestReport:
    units = list(
        session.scalars(
            select(BusinessUnit).where(
                BusinessUnit.active == True, BusinessUnit.routable == True
            )
        )
    )
    if not units:
        raise ValueError("Šifarnik poslovnih jedinica je prazan — prvo pokreni pj-sync / pj-import.")
    index = _UnitIndex(units)

    report = SuggestReport(documents=len(documents))
    # grupisanje po tekstu koji dobavljac salje, da jedno pravilo pokrije vise dokumenata
    groups: dict[tuple[str, str], dict] = {}
    no_delivery: Counter = Counter()
    supplier_names: dict[str, str] = {}

    for doc in documents:
        address = doc.delivery_address
        location = doc.delivery_name or doc.delivery_location_id
        if not address and not location:
            report.without_delivery += 1
            vat = doc.supplier.vat or "?"
            no_delivery[vat] += 1
            supplier_names[vat] = doc.supplier.name or doc.supplier.registration_name or "?"
            continue

        if address:
            key_field, key_text = MatchField.DELIVERY_ADDRESS, address
        else:
            key_field, key_text = MatchField.DELIVERY_NAME, location  # type: ignore[assignment]

        key = (key_field.value, normalize(key_text))
        entry = groups.setdefault(
            key,
            {"field": key_field, "pattern": key_text, "count": 0, "doc": doc},
        )
        entry["count"] += 1

    merged: dict[tuple[str, str, int], Suggestion] = {}
    for entry in groups.values():
        doc: UblDocument = entry["doc"]
        unit, how, candidates = index.match(
            doc.delivery_address,
            doc.delivery_postal_code,
            doc.delivery_city,
            doc.delivery_name or doc.delivery_location_id,
        )
        suggestion = Suggestion(
            field=entry["field"],
            pattern=entry["pattern"],
            count=entry["count"],
            how=how,
        )
        if unit is not None:
            suggestion.business_unit_id = unit.id
            suggestion.unit_code = unit.code
            suggestion.unit_name = unit.name
            # Umesto cele adrese ("Glavna 18, 21220, Bečej") sablon je ulica i broj
            # ("Glavna 18") - jedno pravilo pokriva sve varijante zapisa. Kratki
            # sablon se koristi samo ako stvarno pokriva ono sto dobavljac salje.
            if how == "adresa":
                short = clean_address(unit.address)
                if short and normalize(short) in normalize(entry["pattern"]):
                    suggestion.pattern = short
            key = (suggestion.field.value, normalize(suggestion.pattern), unit.id)
            existing = merged.get(key)
            if existing is None:
                merged[key] = suggestion
            else:
                existing.count += suggestion.count
        else:
            suggestion.candidates = [f"{u.code} {u.name}" for u in candidates] or [
                "nema kandidata u šifarniku"
            ]
            report.conflicts.append(suggestion)

    report.suggestions = list(merged.values())
    report.supplier_hints = sorted(
        ((supplier_names[vat], vat, count) for vat, count in no_delivery.items()),
        key=lambda x: -x[2],
    )
    return report


def apply_suggestions(session: Session, report: SuggestReport) -> int:
    """Upisuje predloge kao pravila; preskace ono sto vec postoji."""
    created = 0
    for s in sorted(report.suggestions, key=lambda x: x.priority):
        if not s.resolved:
            continue
        exists = session.scalar(
            select(RoutingRule).where(
                RoutingRule.field == s.field,
                RoutingRule.pattern == s.pattern,
                RoutingRule.business_unit_id == s.business_unit_id,
            )
        )
        if exists is not None:
            continue
        session.add(
            RoutingRule(
                priority=s.priority,
                field=s.field,
                op=MatchOp.CONTAINS,
                pattern=s.pattern,
                business_unit_id=s.business_unit_id,
                comment=f"automatski predlog ({s.how}, {s.count} dok. iz uzorka)",
            )
        )
        created += 1
    return created
