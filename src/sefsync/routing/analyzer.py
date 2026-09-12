"""Analiza realnih UBL-ova: koja polja dobavljaci zaista popunjavaju.

Prvi korak pre pisanja pravila razvrstavanja. Prolazi kroz preuzete UBL fajlove
i pravi izvestaj:
  - koliko procenata dokumenata ima popunjeno koje polje,
  - koje se konkretne vrednosti pojavljuju (kandidati za pravila),
  - presek po dobavljacu (ko salje adresu isporuke, ko ne salje nista).

Koristi se kao `sefsync analyze` (vidi cli.py).
"""

from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..textutil import normalize
from ..ubl.parser import UblDocument, UblParseError, parse_ubl

log = logging.getLogger(__name__)

# Polja korisna za razvrstavanje, redom po ocekivanoj upotrebljivosti
FIELDS = [
    "delivery_address",
    "delivery_city",
    "delivery_name",
    "order_reference",
    "buyer_reference",
    "contract_reference",
    "note",
    "buyer_address",
    "buyer_city",
    "supplier_name",
    "supplier_vat",
    "document_number",
]


@dataclass
class FieldStat:
    name: str
    filled: int = 0
    total: int = 0
    values: Counter = field(default_factory=Counter)

    @property
    def pct(self) -> float:
        return 100.0 * self.filled / self.total if self.total else 0.0


@dataclass
class SupplierStat:
    vat: str
    name: str
    docs: int = 0
    fields: Counter = field(default_factory=Counter)
    delivery_values: Counter = field(default_factory=Counter)

    def best_field(self) -> str | None:
        """Polje koje ovaj dobavljac najpouzdanije popunjava (osim identiteta)."""
        for name in FIELDS:
            if name in ("supplier_name", "supplier_vat", "document_number"):
                continue
            if self.fields.get(name, 0) == self.docs and self.docs > 0:
                return name
        return None


@dataclass
class AnalysisReport:
    documents: int = 0
    parse_errors: list[str] = field(default_factory=list)
    stats: dict[str, FieldStat] = field(default_factory=dict)
    suppliers: dict[str, SupplierStat] = field(default_factory=dict)
    rows: list[dict[str, str]] = field(default_factory=list)

    # ---------------------------------------------------------------- #

    def to_text(self, top: int = 12) -> str:
        out: list[str] = []
        out.append(f"Analizirano dokumenata: {self.documents}")
        if self.parse_errors:
            out.append(f"Neuspesno parsiranih: {len(self.parse_errors)}")
        out.append("")
        out.append("POPUNJENOST POLJA (kandidati za pravila razvrstavanja)")
        out.append("-" * 72)
        for name in FIELDS:
            st = self.stats.get(name)
            if not st:
                continue
            out.append(f"{name:<20} {st.filled:>5}/{st.total:<5} {st.pct:5.1f}%  razlicitih: {len(st.values)}")
        out.append("")
        out.append("NAJCESCE VREDNOSTI PO POLJU")
        out.append("-" * 72)
        for name in (
            "delivery_address",
            "delivery_city",
            "delivery_name",
            "buyer_address",
            "buyer_city",
            "order_reference",
            "buyer_reference",
            "contract_reference",
        ):
            st = self.stats.get(name)
            if not st or not st.values:
                continue
            out.append(f"\n[{name}]")
            for value, count in st.values.most_common(top):
                out.append(f"  {count:>4}x  {value}")
        out.append("")
        out.append("PO DOBAVLJACU")
        out.append("-" * 72)
        for sup in sorted(self.suppliers.values(), key=lambda s: -s.docs):
            best = sup.best_field() or "-"
            out.append(f"{sup.docs:>4} dok.  PIB {sup.vat or '?':<12} {sup.name[:40]:<40} pouzdano polje: {best}")
            for value, count in sup.delivery_values.most_common(5):
                out.append(f"           {count:>3}x  {value}")
        return "\n".join(out)

    def to_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = ["file", "document_number", "document_type", *FIELDS]
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.rows)


def analyze(documents: list[tuple[str, UblDocument]]) -> AnalysisReport:
    report = AnalysisReport(documents=len(documents))
    stats = {name: FieldStat(name=name, total=len(documents)) for name in FIELDS}
    suppliers: dict[str, SupplierStat] = {}

    for source, doc in documents:
        fields = doc.routing_fields()
        row = {
            "file": source,
            "document_number": doc.document_number or "",
            "document_type": doc.document_type,
        }
        vat = doc.supplier.vat or "?"
        sup = suppliers.get(vat)
        if sup is None:
            sup = suppliers[vat] = SupplierStat(
                vat=vat, name=doc.supplier.name or doc.supplier.registration_name or "?"
            )
        sup.docs += 1

        for name in FIELDS:
            value = (fields.get(name) or "").strip()
            row[name] = value
            if not value:
                continue
            stats[name].filled += 1
            stats[name].values[normalize(value)] += 1
            sup.fields[name] += 1
        delivery = fields.get("delivery_address") or fields.get("delivery_name")
        if delivery:
            sup.delivery_values[normalize(delivery)] += 1
        report.rows.append(row)

    report.stats = stats
    report.suppliers = suppliers
    return report


def load_ubl_dir(directory: Path, limit: int | None = None) -> tuple[list[tuple[str, UblDocument]], list[str]]:
    """Ucitava sve .xml fajlove iz direktorijuma (rekurzivno)."""
    parsed: list[tuple[str, UblDocument]] = []
    errors: list[str] = []
    files = sorted(directory.rglob("*.xml"))
    if limit:
        files = files[:limit]
    for path in files:
        try:
            parsed.append((path.name, parse_ubl(path.read_bytes())))
        except (UblParseError, OSError) as exc:
            errors.append(f"{path.name}: {exc}")
            log.warning("Preskacem %s: %s", path, exc)
    return parsed, errors


def analyze_dir(directory: Path, limit: int | None = None) -> AnalysisReport:
    parsed, errors = load_ubl_dir(directory, limit)
    report = analyze(parsed)
    report.parse_errors = errors
    return report
