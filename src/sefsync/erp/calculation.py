"""FAZA 2 - priprema ulazne kalkulacije iz e-fakture.

Ovaj modul radi deo koji NE zavisi od konkretne ERP seme: iz dokumenta i
njegovih stavki pravi normalizovan "nacrt kalkulacije" (zaglavlje + stavke sa
nabavnom cenom, rabatom, PDV-om i mapiranom sifrom artikla).

Upis u ERP bazu je namerno iza `ErpWriter` interfejsa - implementira se kad se
dogovori tacna sema (nazivi tabela ulazne kalkulacije, dokumenta, veze na
magacin i partnera). Do tada je dostupan `CsvWriter` kao medjukorak.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document, DocumentLine, ItemMapping

log = logging.getLogger(__name__)


@dataclass
class DraftLine:
    line_no: int
    erp_item_code: str | None
    item_name: str | None
    supplier_item_id: str | None
    barcode: str | None
    quantity: float
    unit_code: str | None
    gross_price: float | None       # cena pre rabata
    discount_amount: float          # rabat na stavci
    net_amount: float               # osnovica stavke (posle rabata)
    net_unit_price: float | None    # nabavna cena po jedinici
    vat_percent: float | None
    vat_amount: float
    mapped: bool

    @property
    def needs_attention(self) -> bool:
        return not self.mapped or self.quantity == 0


@dataclass
class CalculationDraft:
    document_id: int
    sef_invoice_id: int
    supplier_name: str | None
    supplier_vat: str | None
    document_number: str | None
    document_type: str
    business_unit_code: str | None
    erp_warehouse_code: str | None
    delivery_date: date | None
    due_date: date | None
    currency: str | None
    total_net: float
    total_vat: float
    total_gross: float
    lines: list[DraftLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def unmapped_lines(self) -> list[DraftLine]:
        return [ln for ln in self.lines if not ln.mapped]

    @property
    def ready(self) -> bool:
        return not self.unmapped_lines and not self.warnings

    def to_dict(self) -> dict:
        data = asdict(self)
        data["delivery_date"] = self.delivery_date.isoformat() if self.delivery_date else None
        data["due_date"] = self.due_date.isoformat() if self.due_date else None
        return data


def _f(value: float | Decimal | None, default: float = 0.0) -> float:
    return float(value) if value is not None else default


def resolve_item(session: Session, supplier_vat: str | None, line: DocumentLine) -> tuple[str | None, str | None]:
    """Vraca (sifra_artikla_u_erp, izvor_mapiranja)."""
    if line.erp_item_code:
        return line.erp_item_code, line.erp_mapping_source or "manual"

    barcode = line.standard_item_id
    if barcode:
        hit = session.scalar(
            select(ItemMapping).where(
                ItemMapping.barcode == barcode, ItemMapping.active == True
            )
        )
        if hit:
            return hit.erp_item_code, "barcode"

    if supplier_vat and line.sellers_item_id:
        hit = session.scalar(
            select(ItemMapping).where(
                ItemMapping.supplier_vat == supplier_vat,
                ItemMapping.supplier_item_id == line.sellers_item_id,
                ItemMapping.active == True,
            )
        )
        if hit:
            return hit.erp_item_code, "supplier_item"

    # kupceva sifra na fakturi je nasa sifra - najpouzdaniji slucaj kad postoji
    if line.buyers_item_id:
        return line.buyers_item_id, "buyers_item_id"
    return None, None


def build_draft(session: Session, document_id: int) -> CalculationDraft:
    doc = session.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Dokument {document_id} ne postoji.")

    unit = doc.business_unit
    draft = CalculationDraft(
        document_id=doc.id,
        sef_invoice_id=doc.sef_invoice_id,
        supplier_name=doc.supplier_name,
        supplier_vat=doc.supplier_vat,
        document_number=doc.document_number,
        document_type=doc.document_type.value,
        business_unit_code=unit.code if unit else None,
        erp_warehouse_code=(unit.erp_code or unit.code) if unit else None,
        delivery_date=doc.delivery_date,
        due_date=doc.due_date,
        currency=doc.currency,
        total_net=_f(doc.sum_without_vat),
        total_vat=_f(doc.vat_amount),
        total_gross=_f(doc.amount),
    )

    if unit is None:
        draft.warnings.append("Dokument nije razvrstan na poslovnu jedinicu.")
    if not doc.lines:
        draft.warnings.append("Dokument nema stavke u UBL-u (usluga ili zbirna faktura).")

    sign = -1.0 if doc.is_credit_note else 1.0

    for line in doc.lines:
        code, source = resolve_item(session, doc.supplier_vat, line)
        quantity = _f(line.quantity) * sign
        net_amount = _f(line.line_amount) * sign
        vat_percent = line.vat_percent
        vat_amount = round(net_amount * _f(vat_percent) / 100.0, 2) if vat_percent else 0.0
        net_unit = (net_amount / quantity) if quantity else None

        draft.lines.append(
            DraftLine(
                line_no=line.line_no,
                erp_item_code=code,
                item_name=line.name,
                supplier_item_id=line.sellers_item_id,
                barcode=line.standard_item_id,
                quantity=quantity,
                unit_code=line.unit_code,
                gross_price=line.price,
                discount_amount=_f(line.allowance_amount),
                net_amount=net_amount,
                net_unit_price=net_unit,
                vat_percent=vat_percent,
                vat_amount=vat_amount,
                mapped=bool(code),
            )
        )
        if source and code and not line.erp_item_code:
            line.erp_item_code = code
            line.erp_mapping_source = source

    unmapped = len(draft.unmapped_lines)
    if unmapped:
        draft.warnings.append(f"{unmapped} stavki nema mapiranu ERP šifru artikla.")
    return draft


# --------------------------------------------------------------------------- #
# Upis
# --------------------------------------------------------------------------- #


class ErpWriter(Protocol):
    """Interfejs ka ERP-u; implementacija zavisi od seme baze."""

    def write(self, draft: CalculationDraft) -> str:
        """Upisuje kalkulaciju i vraca njen identifikator u ERP-u."""
        ...


class CsvWriter:
    """Medjukorak: kalkulacija kao CSV koji ERP uvozi (dok se ne dogovori upis u bazu)."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def write(self, draft: CalculationDraft) -> str:
        path = self.out_dir / f"kalkulacija_{draft.sef_invoice_id}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["magacin", draft.erp_warehouse_code or ""])
            writer.writerow(["dobavljac_pib", draft.supplier_vat or ""])
            writer.writerow(["broj_dokumenta", draft.document_number or ""])
            writer.writerow(["datum_prometa", draft.delivery_date or ""])
            writer.writerow([])
            writer.writerow(
                ["rb", "sifra", "naziv", "kolicina", "jm", "nab_cena", "rabat", "osnovica", "pdv%", "pdv"]
            )
            for ln in draft.lines:
                writer.writerow(
                    [
                        ln.line_no,
                        ln.erp_item_code or "",
                        ln.item_name or "",
                        f"{ln.quantity:g}",
                        ln.unit_code or "",
                        f"{ln.net_unit_price:.4f}" if ln.net_unit_price is not None else "",
                        f"{ln.discount_amount:.2f}",
                        f"{ln.net_amount:.2f}",
                        f"{ln.vat_percent:g}" if ln.vat_percent is not None else "",
                        f"{ln.vat_amount:.2f}",
                    ]
                )
        log.info("Kalkulacija upisana u %s", path)
        return str(path)
