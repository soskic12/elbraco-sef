from decimal import Decimal

import pytest

from sefsync.ubl import UblParseError, parse_ubl


def test_zaglavlje_fakture(faktura_xml):
    doc = parse_ubl(faktura_xml)

    assert doc.document_number == "2026-114/25"
    assert doc.document_type == "Invoice"
    assert doc.type_code == "380"
    assert doc.issue_date.isoformat() == "2026-09-01"
    assert doc.due_date.isoformat() == "2026-10-01"
    assert doc.delivery_date.isoformat() == "2026-08-31"
    assert doc.currency == "RSD"
    assert doc.buyer_reference == "MP-02"
    assert doc.order_reference == "NAR-4471"
    assert doc.contract_reference == "UG-2026-8"
    assert doc.payment_account == "160-123456-78"


def test_ucesnici(faktura_xml):
    doc = parse_ubl(faktura_xml)

    assert doc.supplier.vat == "100200300"          # "RS" prefiks je uklonjen
    assert doc.supplier.registration_number == "20123456"
    assert doc.supplier.name == "DOBAVLJAČ TRGOVINA DOO"
    assert doc.supplier.email == "prodaja@dobavljac.rs"
    assert doc.customer.vat == "111222333"
    assert doc.customer.city == "Beograd"


def test_adresa_isporuke_u_cirilici(faktura_xml):
    doc = parse_ubl(faktura_xml)

    assert doc.delivery_street == "Војводе Мишића 12"
    assert doc.delivery_city == "Панчево"
    assert doc.delivery_location_id == "MP02"
    assert doc.delivery_name == "ELBRACO MP Pančevo"
    assert doc.delivery_address == "Војводе Мишића 12, 26000, Панчево"


def test_iznosi(faktura_xml):
    doc = parse_ubl(faktura_xml)

    assert doc.tax_exclusive_amount == Decimal("12000.00")
    assert doc.tax_amount == Decimal("2400.00")
    assert doc.payable_amount == Decimal("14400.00")
    assert len(doc.tax_subtotals) == 1
    assert doc.tax_subtotals[0].percent == Decimal("20")


def test_stavke(faktura_xml):
    doc = parse_ubl(faktura_xml)
    assert len(doc.lines) == 2

    prva = doc.lines[0]
    assert prva.line_no == 1
    assert prva.name == "Kabl PPY 3x1.5"
    assert prva.quantity == Decimal("10")
    assert prva.unit_code == "H87"
    assert prva.sellers_item_id == "ART-551"
    assert prva.standard_item_id == "8600123456789"
    assert prva.line_amount == Decimal("9000.00")
    assert prva.allowance_amount == Decimal("1000.00")   # rabat na stavci
    assert prva.vat_percent == Decimal("20")
    # neto nabavna cena je osnovica/kolicina, ne PriceAmount
    assert prva.net_unit_price == Decimal("900")

    druga = doc.lines[1]
    assert druga.buyers_item_id == "10045"
    assert druga.unit_code == "MTR"


def test_knjizno_odobrenje_u_omotu(odobrenje_xml):
    doc = parse_ubl(odobrenje_xml)

    assert doc.document_type == "CreditNote"
    assert doc.type_code == "381"
    assert doc.document_number == "KO-33/26"
    assert doc.billing_references == ["2026-114/25"]
    assert len(doc.lines) == 1
    # nema Delivery elementa - razvrstavanje mora da padne na druga polja
    assert doc.delivery_address is None
    assert doc.customer.address == "Kneza Miloša 5, Beograd"


def test_routing_fields(faktura_xml):
    fields = parse_ubl(faktura_xml).routing_fields()

    assert fields["delivery_city"] == "Панчево"
    assert fields["supplier_vat"] == "100200300"
    assert "Kabl PPY 3x1.5" in fields["item_text"]


def test_prazan_i_neispravan_ulaz():
    with pytest.raises(UblParseError):
        parse_ubl(b"")
    with pytest.raises(UblParseError):
        parse_ubl(b"<html><body>nije UBL</body></html>")


def test_redni_broj_je_pozicija_a_ne_oznaka_dobavljaca():
    """Neki dobavljači u cbc:ID stavke upisuju šifru artikla, i to dvaput istu."""
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
             xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
             xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
      <cbc:ID>X-1</cbc:ID>
      <cac:InvoiceLine><cbc:ID>392740</cbc:ID>
        <cac:Item><cbc:Name>MP PATRONE ZA SLAG 24/1</cbc:Name></cac:Item></cac:InvoiceLine>
      <cac:InvoiceLine><cbc:ID>392740</cbc:ID>
        <cac:Item><cbc:Name>MP PATRONE ZA SLAG 24/1</cbc:Name></cac:Item></cac:InvoiceLine>
    </Invoice>"""

    lines = parse_ubl(xml).lines

    assert [x.line_no for x in lines] == [1, 2]          # jedinstveno u dokumentu
    assert [x.line_ref for x in lines] == ["392740", "392740"]
