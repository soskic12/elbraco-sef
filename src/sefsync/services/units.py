"""Uvoz i sinhronizacija sifarnika poslovnih jedinica.

Dva izvora, isti upis:
  - CSV fajl (`sefsync pj-import`),
  - tabela u postojecoj bazi na serveru (`sefsync pj-sync`), preko upita
    zadatog u `BU_SOURCE_SQL` - tako se ne zakucava tudja sema u kod.

Ocekivane kolone (nedostajuce su opcione): code, name, kind, erp_code,
address, city, emails, phones, active.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, select, text

from ..config import get_settings
from ..db import session_scope
from ..erp.connection import resolve_erp_url
from ..models import BusinessUnit, UnitKind

log = logging.getLogger(__name__)

COLUMNS = (
    "code", "name", "kind", "erp_code", "address", "city", "postal_code",
    "emails", "phones", "active", "routable",
)

# tolerantna imena kolona iz postojecih tabela
ALIASES = {
    "ptt": "postal_code",
    "pttbroj": "postal_code",
    "mesto": "city",
    "region": "city",
    "adresa": "address",
    "naziv": "name",
    "sifra": "code",
    "aktivan": "active",
}


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []

    def summary(self) -> str:
        return f"novih {self.created}, ažurirano {self.updated}, preskočeno {self.skipped}"


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "da", "yes", "y", "t", "aktivna")


def upsert_units(rows: Iterable[dict[str, Any]], deactivate_missing: bool = False) -> ImportResult:
    """Upisuje jedinice po `code`; postojece azurira, nove dodaje."""
    result = ImportResult()
    seen_codes: set[str] = set()

    for row in rows:
        normalized = {}
        for key, value in row.items():
            name = str(key).strip().lower()
            normalized[ALIASES.get(name, name)] = value
        code = _clean(normalized.get("code"))
        if not code:
            result.skipped += 1
            continue
        seen_codes.add(code)

        with session_scope() as session:
            unit = session.scalar(select(BusinessUnit).where(BusinessUnit.code == code))
            if unit is None:
                unit = BusinessUnit(code=code, name=_clean(normalized.get("name")) or code)
                session.add(unit)
                result.created += 1
            else:
                result.updated += 1

            unit.name = _clean(normalized.get("name")) or unit.name
            kind = _clean(normalized.get("kind"))
            if kind:
                try:
                    unit.kind = UnitKind(kind.lower())
                except ValueError:
                    result.warnings.append(
                        f"{code}: nepoznat tip '{kind}' (dozvoljeno: "
                        f"{', '.join(k.value for k in UnitKind)})"
                    )
            # Prazna vrednost iz izvora NE brise postojeci podatak: izvorne tabele
            # imaju rupe (npr. Old Brick Pub nema Region), a te rupe se popunjavaju
            # rucno kroz dashboard i moraju da prezive sledecu sinhronizaciju.
            for field in ("erp_code", "address", "city", "postal_code", "emails", "phones"):
                value = _clean(normalized.get(field))
                if value is not None:
                    setattr(unit, field, value)
            if "active" in normalized:
                unit.active = _as_bool(normalized.get("active"))
            if "routable" in normalized:
                unit.routable = _as_bool(normalized.get("routable"))

    if deactivate_missing and seen_codes:
        with session_scope() as session:
            for unit in session.scalars(select(BusinessUnit).where(BusinessUnit.active == True)):
                if unit.code not in seen_codes:
                    unit.active = False
                    result.warnings.append(f"{unit.code}: nema je više u izvoru — deaktivirana")
    return result


def import_csv(path: Path, delimiter: str = ";") -> ImportResult:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        if delimiter not in sample and "," in sample:
            delimiter = ","
        return upsert_units(list(csv.DictReader(fh, delimiter=delimiter)))


def sync_from_source(deactivate_missing: bool = False) -> ImportResult:
    """Cita jedinice iz postojece tabele na serveru (BU_SOURCE_SQL)."""
    settings = get_settings()
    query = (settings.bu_source_sql or "").strip()
    if not query:
        raise ValueError(
            "BU_SOURCE_SQL nije podešen. Primer:\n"
            "  BU_SOURCE_SQL=SELECT sifra AS code, naziv AS name, tip AS kind, "
            "sifra AS erp_code, adresa AS address, mesto AS city, email AS emails, "
            "telefon AS phones FROM dbo.PoslovneJedinice WHERE aktivna = 1"
        )
    url = resolve_erp_url()
    if not url:
        raise ValueError(
            "Nije podešena veza ka bazi. Postavi BU_SOURCE_DB_URL ili ERP_DB_URL, "
            "ili ERP_SECRETS_FILE (putanja do secrets.json projekta koji već ima kredencijale)."
        )

    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(query)).mappings()]
    finally:
        engine.dispose()

    log.info("Iz izvorne tabele pročitano %s poslovnih jedinica", len(rows))
    return upsert_units(rows, deactivate_missing=deactivate_missing)
