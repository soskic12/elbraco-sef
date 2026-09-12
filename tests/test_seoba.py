"""Seoba baze panela na drugi server."""

from __future__ import annotations

from sqlalchemy import create_engine, func, select

from sefsync.models import Base, BusinessUnit, Document, DocumentLine, SefStatus, UnitKind
from sefsync.services.seoba import prebaci


def _napuni(db):
    with db.session_scope() as session:
        unit = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        session.add(unit)
        session.flush()
        doc = Document(sef_invoice_id=777, document_number="1/26", supplier_name="Dobavljač",
                       amount=1000.0, sef_status=SefStatus.SEEN, business_unit_id=unit.id)
        doc.lines.append(DocumentLine(line_no=1, name="Artikal", quantity=2, line_amount=500.0))
        session.add(doc)


def test_seli_dokumente_stavke_i_veze(db, tmp_path):
    _napuni(db)
    cilj = create_engine(f"sqlite:///{(tmp_path / 'novo.db').as_posix()}")

    izvestaj = prebaci(db.get_engine(), cilj)

    assert izvestaj.prepisano["document"] == 1
    assert izvestaj.prepisano["document_line"] == 1
    with cilj.connect() as veza:
        red = veza.execute(select(Document.__table__)).mappings().one()
        stavka = veza.execute(select(DocumentLine.__table__)).mappings().one()
        jedinica = veza.execute(select(BusinessUnit.__table__)).mappings().one()
    # veze moraju da prezive: stavka pokazuje na dokument, dokument na PJ
    assert stavka["document_id"] == red["id"]
    assert red["business_unit_id"] == jedinica["id"]
    assert red["sef_invoice_id"] == 777
    cilj.dispose()


def test_ponovno_pokretanje_ne_duplira(db, tmp_path):
    _napuni(db)
    cilj = create_engine(f"sqlite:///{(tmp_path / 'novo.db').as_posix()}")

    prebaci(db.get_engine(), cilj)
    drugi = prebaci(db.get_engine(), cilj)

    assert drugi.prepisano.get("document", 0) == 0
    assert drugi.preskoceno["document"] == 1
    with cilj.connect() as veza:
        assert veza.execute(select(func.count()).select_from(Document.__table__)).scalar_one() == 1
    cilj.dispose()


def test_prepisi_ocisti_pa_upise(db, tmp_path):
    _napuni(db)
    cilj = create_engine(f"sqlite:///{(tmp_path / 'novo.db').as_posix()}")
    prebaci(db.get_engine(), cilj)

    izvestaj = prebaci(db.get_engine(), cilj, prepisi=True)

    assert izvestaj.prepisano["document"] == 1
    with cilj.connect() as veza:
        assert veza.execute(select(func.count()).select_from(Document.__table__)).scalar_one() == 1
    cilj.dispose()


def test_prazna_baza_daje_praznu_ali_ispravnu_semu(db, tmp_path):
    cilj = create_engine(f"sqlite:///{(tmp_path / 'novo.db').as_posix()}")

    prebaci(db.get_engine(), cilj)

    from sqlalchemy import inspect

    tabele = set(inspect(cilj).get_table_names())
    assert {"document", "document_line", "business_unit", "routing_rule"} <= tabele
    cilj.dispose()
