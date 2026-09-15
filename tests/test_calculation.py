"""Faza 2 (nacrt): pretvaranje dokumenta u ulaznu kalkulaciju."""

from __future__ import annotations

from sefsync.erp.calculation import CsvWriter, build_draft
from sefsync.models import (
    BusinessUnit,
    Document,
    DocumentLine,
    DocumentType,
    ItemMapping,
    UnitKind,
)


def _document(session, doc_type=DocumentType.INVOICE) -> int:
    unit = BusinessUnit(code="MP02", name="Maloprodaja Pančevo", kind=UnitKind.RETAIL, erp_code="02")
    session.add(unit)
    session.flush()
    doc = Document(
        sef_invoice_id=1001,
        document_number="2026-114/25",
        document_type=doc_type,
        supplier_name="DOBAVLJAČ TRGOVINA DOO",
        supplier_vat="100200300",
        sum_without_vat=12000.0,
        vat_amount=2400.0,
        amount=14400.0,
        currency="RSD",
        business_unit_id=unit.id,
    )
    doc.lines.append(
        DocumentLine(line_no=1, name="Kabl PPY 3x1.5", sellers_item_id="ART-551",
                     standard_item_id="8600123456789", quantity=10, unit_code="H87",
                     price=1000.0, allowance_amount=1000.0, line_amount=9000.0, vat_percent=20.0)
    )
    doc.lines.append(
        DocumentLine(line_no=2, name="Gibljivo crevo", sellers_item_id="ART-772",
                     buyers_item_id="10045", quantity=50, unit_code="MTR",
                     price=60.0, line_amount=3000.0, vat_percent=20.0)
    )
    session.add(doc)
    session.flush()
    return doc.id


def test_nacrt_racuna_nabavnu_cenu_i_pdv(db):
    with db.session_scope() as session:
        doc_id = _document(session)
        draft = build_draft(session, doc_id)

    assert draft.erp_warehouse_code == "02"
    assert len(draft.lines) == 2

    prva = draft.lines[0]
    assert prva.quantity == 10
    assert prva.net_amount == 9000.0
    assert prva.net_unit_price == 900.0     # nabavna cena posle rabata
    assert prva.discount_amount == 1000.0
    assert prva.vat_amount == 1800.0
    assert not prva.mapped                  # nema mapiranja za ART-551

    druga = draft.lines[1]
    assert druga.mapped                     # BuyersItemIdentification je nasa sifra
    assert druga.erp_item_code == "10045"

    assert len(draft.unmapped_lines) == 1
    assert not draft.ready


def test_mapiranje_po_barkodu(db):
    with db.session_scope() as session:
        doc_id = _document(session)
        session.add(
            ItemMapping(supplier_vat="100200300", barcode="8600123456789", erp_item_code="10012")
        )
        session.flush()
        draft = build_draft(session, doc_id)

    assert draft.lines[0].erp_item_code == "10012"
    assert draft.ready


def test_knjizno_odobrenje_ima_negativne_kolicine(db):
    with db.session_scope() as session:
        doc_id = _document(session, doc_type=DocumentType.CREDIT_NOTE)
        draft = build_draft(session, doc_id)

    assert draft.lines[0].quantity == -10
    assert draft.lines[0].net_amount == -9000.0
    assert draft.lines[0].net_unit_price == 900.0  # cena ostaje pozitivna


def test_csv_izvoz(db, tmp_path):
    with db.session_scope() as session:
        doc_id = _document(session)
        draft = build_draft(session, doc_id)

    path = CsvWriter(tmp_path).write(draft)
    content = open(path, encoding="utf-8-sig").read()
    assert "Kabl PPY 3x1.5" in content
    assert "magacin;02" in content


def test_app_ne_sme_da_upise_zaknjizenu_kalkulaciju():
    """Knjizenje ide iskljucivo kroz ERP - ono povlaci i glavnu knjigu.

    Kalkulacija upisana kao vec zaknjizena izgleda gotovo, a nista se nije
    desilo: roba nije zaduzena, glavna knjiga ne zna za nju, poslovodja nema
    sta da klikne.
    """
    import pytest

    from sefsync.erp.kalkulacija import (PROKNJIZENO_JESTE, PROKNJIZENO_NIJE,
                                         PROKNJIZENO_ZAKLJUCANO, Nacrt, upisi)

    nacrt = Nacrt(
        document_id=1, tabela_zaglavlja="MKALKUL", tabela_stavki="MPRULK",
        magacin="005", zaglavlje={"PROKNJIZENO": PROKNJIZENO_JESTE},
        stavke=[{"ARTIKAL": "1"}],
    )
    with pytest.raises(ValueError, match="zaknjizenu"):
        upisi(nacrt)

    nacrt.zaglavlje["PROKNJIZENO"] = PROKNJIZENO_ZAKLJUCANO
    with pytest.raises(ValueError, match="zaknjizenu"):
        upisi(nacrt)

    assert PROKNJIZENO_NIJE == 0


def test_nacrt_podrazumevano_nije_zaknjizen():
    from sefsync.erp import kalkulacija

    izvor = (kalkulacija.__file__)
    with open(izvor, encoding="utf-8") as f:
        kod = f.read()
    # vrednost se ne sme zakucavati brojem na mestu gradjenja zaglavlja
    assert '"PROKNJIZENO": PROKNJIZENO_NIJE' in kod
    assert '"PROKNJIZENO": 1' not in kod
