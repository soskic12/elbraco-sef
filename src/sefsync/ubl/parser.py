"""Parser UBL 2.1 dokumenata sa SEF-a (srpski profil, srbdt).

Parser je namerno "labav": koristi `local-name()` XPath izraze pa mu ne smetaju
razlicite verzije namespace-a, omot (`DocumentEnvelope`) niti to da li je
dokument poslat kao `Invoice` (sa TypeCode 381/383/386) ili kao `CreditNote`.
Ono sto ne postoji vraca se kao None umesto da puca - dobavljaci popunjavaju
opciona polja vrlo neujednaceno.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable

from lxml import etree

log = logging.getLogger(__name__)

# UBL InvoiceTypeCode -> tip dokumenta (isti nazivi kao u SEF /overview odgovoru)
TYPE_CODE_MAP = {
    "380": "Invoice",         # faktura
    "381": "CreditNote",      # knjizno odobrenje
    "383": "DebitNote",       # knjizno zaduzenje
    "386": "Prepayment",      # avansni racun
    "384": "CreditNote",      # korigovana faktura (retko, tretiramo kao KO)
}


class UblParseError(ValueError):
    pass


@dataclass(slots=True)
class Party:
    name: str | None = None
    registration_name: str | None = None
    vat: str | None = None            # PIB bez "RS" prefiksa
    registration_number: str | None = None  # maticni broj
    jbkjs: str | None = None
    street: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country: str | None = None
    email: str | None = None

    @property
    def address(self) -> str | None:
        parts = [p for p in (self.street, self.postal_code, self.city) if p]
        return ", ".join(parts) or None


@dataclass(slots=True)
class UblLine:
    line_no: int                    # pozicija u dokumentu (1, 2, 3...)
    line_ref: str | None = None     # cbc:ID stavke kako ga dobavljac salje
    name: str | None = None
    description: str | None = None
    sellers_item_id: str | None = None
    buyers_item_id: str | None = None
    standard_item_id: str | None = None
    quantity: Decimal | None = None
    unit_code: str | None = None
    price: Decimal | None = None
    base_quantity: Decimal | None = None
    line_amount: Decimal | None = None
    allowance_amount: Decimal | None = None
    charge_amount: Decimal | None = None
    vat_percent: Decimal | None = None
    vat_category: str | None = None
    note: str | None = None

    @property
    def net_unit_price(self) -> Decimal | None:
        """Neto nabavna cena po jedinici (posle rabata na stavci)."""
        if self.line_amount is not None and self.quantity:
            try:
                return self.line_amount / self.quantity
            except (ZeroDivisionError, InvalidOperation):
                return None
        if self.price is not None and self.base_quantity:
            return self.price / self.base_quantity
        return self.price


@dataclass(slots=True)
class TaxSubtotal:
    taxable_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    percent: Decimal | None = None
    category: str | None = None
    exemption_reason: str | None = None


@dataclass(slots=True)
class Attachment:
    filename: str | None
    mime_type: str | None
    content: bytes


@dataclass(slots=True)
class UblDocument:
    """Isparsiran ulazni dokument."""

    document_number: str | None = None
    document_type: str = "Invoice"
    type_code: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    tax_point_date: date | None = None
    delivery_date: date | None = None
    currency: str | None = None
    note: str | None = None

    supplier: Party = field(default_factory=Party)
    customer: Party = field(default_factory=Party)

    delivery_name: str | None = None
    delivery_location_id: str | None = None
    delivery_street: str | None = None
    delivery_city: str | None = None
    delivery_postal_code: str | None = None

    buyer_reference: str | None = None
    order_reference: str | None = None
    contract_reference: str | None = None
    despatch_reference: str | None = None
    project_reference: str | None = None
    billing_references: list[str] = field(default_factory=list)  # KO/KZ -> broj originalne fakture

    payment_account: str | None = None
    payment_reference: str | None = None

    line_extension_amount: Decimal | None = None
    tax_exclusive_amount: Decimal | None = None
    tax_inclusive_amount: Decimal | None = None
    allowance_total: Decimal | None = None
    charge_total: Decimal | None = None
    prepaid_amount: Decimal | None = None
    rounding_amount: Decimal | None = None
    payable_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    tax_subtotals: list[TaxSubtotal] = field(default_factory=list)

    lines: list[UblLine] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def delivery_address(self) -> str | None:
        parts = [p for p in (self.delivery_street, self.delivery_postal_code, self.delivery_city) if p]
        return ", ".join(parts) or None

    @property
    def item_text(self) -> str:
        return " | ".join(x.name for x in self.lines if x.name)

    def routing_fields(self) -> dict[str, str | None]:
        """Polja koja koristi mehanizam razvrstavanja (i alat za analizu)."""
        return {
            "delivery_address": self.delivery_address,
            "delivery_city": self.delivery_city,
            "delivery_name": self.delivery_name or self.delivery_location_id,
            "buyer_address": self.customer.address,
            "buyer_city": self.customer.city,
            "buyer_reference": self.buyer_reference,
            "order_reference": self.order_reference,
            "contract_reference": self.contract_reference,
            "note": self.note,
            "supplier_vat": self.supplier.vat,
            "supplier_name": self.supplier.name or self.supplier.registration_name,
            "document_number": self.document_number,
            "item_text": self.item_text or None,
            "document_type": self.document_type,
        }


# --------------------------------------------------------------------------- #
# pomocne funkcije za XML
# --------------------------------------------------------------------------- #


def _ln(name: str) -> str:
    """XPath uslov po lokalnom imenu elementa (ignorise namespace)."""
    return f"*[local-name()='{name}']"


def _find(node, path: str):
    """`path` je oblika 'Delivery/DeliveryLocation/Address'."""
    expr = "/".join(_ln(p) for p in path.split("/"))
    found = node.xpath(f"./{expr}")
    return found[0] if found else None


def _find_all(node, path: str) -> list:
    expr = "/".join(_ln(p) for p in path.split("/"))
    return node.xpath(f"./{expr}")


def _text(node, path: str | None = None) -> str | None:
    target = node if path is None else _find(node, path)
    if target is None or target.text is None:
        return None
    value = " ".join(target.text.split())
    return value or None


def _attr(node, path: str, attr: str) -> str | None:
    target = _find(node, path)
    if target is None:
        return None
    return target.get(attr)


def _dec(node, path: str | None = None) -> Decimal | None:
    raw = _text(node, path)
    if raw is None:
        return None
    raw = raw.replace(" ", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        log.debug("Ne mogu da parsiram broj %r", raw)
        return None


_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _date(node, path: str) -> date | None:
    raw = _text(node, path)
    if not raw:
        return None
    m = _DATE_RE.search(raw)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _clean_vat(raw: str | None) -> str | None:
    """'RS107775252' -> '107775252'; ostavlja strane PIB-ove kakvi jesu."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.upper().startswith("RS"):
        return raw[2:].strip() or None
    return raw


def _join_notes(values: Iterable[str | None]) -> str | None:
    seen: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.append(v)
    return "\n".join(seen) or None


# --------------------------------------------------------------------------- #
# parsiranje
# --------------------------------------------------------------------------- #


def _parse_party(node) -> Party:
    p = Party()
    if node is None:
        return p
    party = _find(node, "Party")
    if party is None:
        party = node
    p.name = _text(party, "PartyName/Name")
    p.registration_name = _text(party, "PartyLegalEntity/RegistrationName")
    p.registration_number = _text(party, "PartyLegalEntity/CompanyID")
    p.vat = _clean_vat(_text(party, "PartyTaxScheme/CompanyID")) or _clean_vat(
        _text(party, "EndpointID")
    )
    for ident in _find_all(party, "PartyIdentification/ID"):
        value = _text(ident)
        if value and value.upper().startswith("JBKJS:"):
            p.jbkjs = value.split(":", 1)[1].strip()
    address = _find(party, "PostalAddress")
    if address is not None:
        p.street = _text(address, "StreetName")
        extra = _text(address, "AdditionalStreetName")
        if extra:
            p.street = f"{p.street} {extra}".strip() if p.street else extra
        p.city = _text(address, "CityName")
        p.postal_code = _text(address, "PostalZone")
        p.country = _text(address, "Country/IdentificationCode")
    p.email = _text(party, "Contact/ElectronicMail")
    return p


def _parse_line(node, index: int, quantity_tags: tuple[str, ...]) -> UblLine:
    # Redni broj je UVEK pozicija u dokumentu. cbc:ID stavke nije pouzdan kao
    # redni broj: neki dobavljaci tamo upisuju sifru artikla, pa se ista
    # vrednost ponovi na dve stavke (primer: 392740 kod MP patrona).
    line = UblLine(line_no=index, line_ref=_text(node, "ID"))

    for tag in quantity_tags:
        qty_node = _find(node, tag)
        if qty_node is not None:
            line.quantity = _dec(qty_node)
            line.unit_code = qty_node.get("unitCode")
            break

    line.line_amount = _dec(node, "LineExtensionAmount")
    line.note = _text(node, "Note")

    item = _find(node, "Item")
    if item is not None:
        line.name = _text(item, "Name")
        line.description = _text(item, "Description")
        line.sellers_item_id = _text(item, "SellersItemIdentification/ID")
        line.buyers_item_id = _text(item, "BuyersItemIdentification/ID")
        line.standard_item_id = _text(item, "StandardItemIdentification/ID")
        line.vat_percent = _dec(item, "ClassifiedTaxCategory/Percent")
        line.vat_category = _text(item, "ClassifiedTaxCategory/ID")

    price = _find(node, "Price")
    if price is not None:
        line.price = _dec(price, "PriceAmount")
        line.base_quantity = _dec(price, "BaseQuantity")

    # samo rabati/troskovi na nivou stavke; Price/AllowanceCharge je vec sadrzan
    # u PriceAmount pa se ne sabira (XPath gleda samo direktnu decu stavke)
    allowance = Decimal(0)
    charge = Decimal(0)
    for ac in _find_all(node, "AllowanceCharge"):
        amount = _dec(ac, "Amount") or Decimal(0)
        indicator = (_text(ac, "ChargeIndicator") or "false").lower()
        if indicator in ("true", "1"):
            charge += amount
        else:
            allowance += amount
    line.allowance_amount = allowance if allowance else None
    line.charge_amount = charge if charge else None
    return line


def _parse_attachments(root) -> list[Attachment]:
    out: list[Attachment] = []
    for ref in _find_all(root, "AdditionalDocumentReference"):
        binary = _find(ref, "Attachment/EmbeddedDocumentBinaryObject")
        if binary is None or not binary.text:
            continue
        try:
            content = base64.b64decode(binary.text)
        except Exception:  # noqa: BLE001 - los prilog ne sme da obori parsiranje
            log.warning("Prilog nije validan base64, preskacem.")
            continue
        out.append(
            Attachment(
                filename=binary.get("filename") or _text(ref, "ID"),
                mime_type=binary.get("mimeCode"),
                content=content,
            )
        )
    return out


def parse_ubl(data: bytes | str) -> UblDocument:
    """Parsira UBL (Invoice ili CreditNote), uz podrsku za SEF omot."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data.strip():
        raise UblParseError("Prazan UBL dokument.")

    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise UblParseError(f"Neispravan XML: {exc}") from exc
    if root is None:
        raise UblParseError("XML se ne moze parsirati.")

    # SEF omot: <DocumentEnvelope><DocumentBody><Invoice .../></DocumentBody></DocumentEnvelope>
    local = etree.QName(root).localname
    if local not in ("Invoice", "CreditNote", "DebitNote"):
        for candidate in ("Invoice", "CreditNote", "DebitNote"):
            found = root.xpath(f".//{_ln(candidate)}")
            if found:
                root = found[0]
                local = candidate
                break
        else:
            raise UblParseError(f"U dokumentu nema Invoice/CreditNote elementa (koren: {local}).")

    doc = UblDocument()
    doc.document_number = _text(root, "ID")
    doc.issue_date = _date(root, "IssueDate")
    doc.due_date = _date(root, "DueDate")
    doc.tax_point_date = _date(root, "TaxPointDate")
    doc.currency = _text(root, "DocumentCurrencyCode")
    doc.note = _join_notes(_text(n) for n in _find_all(root, "Note"))
    doc.buyer_reference = _text(root, "BuyerReference")

    type_code = _text(root, "InvoiceTypeCode") or _text(root, "CreditNoteTypeCode")
    doc.type_code = type_code
    if type_code and type_code in TYPE_CODE_MAP:
        doc.document_type = TYPE_CODE_MAP[type_code]
    elif local == "CreditNote":
        doc.document_type = "CreditNote"
    else:
        doc.document_type = "Invoice"

    doc.supplier = _parse_party(_find(root, "AccountingSupplierParty"))
    doc.customer = _parse_party(_find(root, "AccountingCustomerParty"))

    delivery = _find(root, "Delivery")
    if delivery is not None:
        doc.delivery_date = _date(delivery, "ActualDeliveryDate")
        doc.delivery_name = _text(delivery, "DeliveryParty/PartyName/Name")
        doc.delivery_location_id = _text(delivery, "DeliveryLocation/ID")
        address = _find(delivery, "DeliveryLocation/Address")
        if address is None:
            address = _find(delivery, "DeliveryAddress")
        if address is not None:
            street = _text(address, "StreetName")
            extra = _text(address, "AdditionalStreetName")
            doc.delivery_street = " ".join(x for x in (street, extra) if x) or None
            doc.delivery_city = _text(address, "CityName")
            doc.delivery_postal_code = _text(address, "PostalZone")

    doc.order_reference = _text(root, "OrderReference/ID")
    doc.contract_reference = _text(root, "ContractDocumentReference/ID")
    doc.despatch_reference = _text(root, "DespatchDocumentReference/ID")
    doc.project_reference = _text(root, "ProjectReference/ID")
    doc.billing_references = [
        v
        for v in (_text(n, "InvoiceDocumentReference/ID") for n in _find_all(root, "BillingReference"))
        if v
    ]

    payment = _find(root, "PaymentMeans")
    if payment is not None:
        doc.payment_account = _text(payment, "PayeeFinancialAccount/ID")
        doc.payment_reference = _text(payment, "PaymentID")

    totals = _find(root, "LegalMonetaryTotal")
    if totals is not None:
        doc.line_extension_amount = _dec(totals, "LineExtensionAmount")
        doc.tax_exclusive_amount = _dec(totals, "TaxExclusiveAmount")
        doc.tax_inclusive_amount = _dec(totals, "TaxInclusiveAmount")
        doc.allowance_total = _dec(totals, "AllowanceTotalAmount")
        doc.charge_total = _dec(totals, "ChargeTotalAmount")
        doc.prepaid_amount = _dec(totals, "PrepaidAmount")
        doc.rounding_amount = _dec(totals, "PayableRoundingAmount")
        doc.payable_amount = _dec(totals, "PayableAmount")

    tax_total = _find(root, "TaxTotal")
    if tax_total is not None:
        doc.tax_amount = _dec(tax_total, "TaxAmount")
        for sub in _find_all(tax_total, "TaxSubtotal"):
            category = _find(sub, "TaxCategory")
            doc.tax_subtotals.append(
                TaxSubtotal(
                    taxable_amount=_dec(sub, "TaxableAmount"),
                    tax_amount=_dec(sub, "TaxAmount"),
                    percent=_dec(category, "Percent") if category is not None else None,
                    category=_text(category, "ID") if category is not None else None,
                    exemption_reason=_text(category, "TaxExemptionReason")
                    if category is not None
                    else None,
                )
            )

    line_nodes = _find_all(root, "InvoiceLine") or _find_all(root, "CreditNoteLine")
    quantity_tags = ("InvoicedQuantity", "CreditedQuantity", "Quantity")
    doc.lines = [_parse_line(n, i, quantity_tags) for i, n in enumerate(line_nodes, start=1)]
    doc.attachments = _parse_attachments(root)
    return doc
