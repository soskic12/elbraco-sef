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


def test_ponovno_pokretanje_staje_umesto_da_preskoci(db, tmp_path):
    """Drugo pokretanje se odbija, ne preskace tiho.

    Do 08.10. je tabela koja u cilju vec ima redova tiho preskakana. Tako se
    dobijala mesana baza - deo tabela iz jednog pokretanja, deo iz drugog - i to
    se tesko prepoznaje. Odbijanje je glasno i ne ostavlja nista za sobom.
    """
    import pytest

    from sefsync.services.seoba import SeobaNijeBezbedna

    _napuni(db)
    cilj = create_engine(f"sqlite:///{(tmp_path / 'novo.db').as_posix()}")

    prebaci(db.get_engine(), cilj)
    with pytest.raises(SeobaNijeBezbedna):
        prebaci(db.get_engine(), cilj)

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


def test_seoba_staje_kad_cilj_nije_prazan(tmp_path):
    """Mešana baza je gore od neurađene seobe.

    Do 08.10. je tabela koja u cilju već ima redova tiho preskakana, pa bi deo
    podataka bio iz stare baze a deo iz nove, sa stranim ključevima koji pokazuju
    u prazno. Bane, 08.10.: „NE SMEMO UĆI U REGRESIJU“.
    """
    import pytest
    from sqlalchemy import create_engine, select

    from sefsync.models import Base, BusinessUnit
    from sefsync.services.seoba import SeobaNijeBezbedna, prebaci, pregled_odredista

    izvor = create_engine(f"sqlite:///{tmp_path / 'izvor.db'}")
    cilj = create_engine(f"sqlite:///{tmp_path / 'cilj.db'}")
    Base.metadata.create_all(izvor)
    Base.metadata.create_all(cilj)

    from sqlalchemy.orm import Session

    with Session(izvor) as s:
        s.add_all([BusinessUnit(code="MP001", name="Sombor"),
                   BusinessUnit(code="MP002", name="Apatin")])
        s.commit()
    # U cilju vec stoji nesto tudje.
    with Session(cilj) as s:
        s.add(BusinessUnit(code="STARO", name="Zaostalo iz prethodne baze"))
        s.commit()

    assert pregled_odredista(cilj) == {"business_unit": 1}

    with pytest.raises(SeobaNijeBezbedna) as greska:
        prebaci(izvor, cilj)
    assert "business_unit" in str(greska.value)

    # Cilj je nedirnut - nista nije upisano.
    with Session(cilj) as s:
        assert [u.code for u in s.scalars(select(BusinessUnit))] == ["STARO"]

    # Sa izricitim pristankom prolazi i cilj se zamenjuje.
    izvestaj = prebaci(izvor, cilj, prepisi=True)
    assert izvestaj.prepisano["business_unit"] == 2
    with Session(cilj) as s:
        assert sorted(u.code for u in s.scalars(select(BusinessUnit))) == ["MP001", "MP002"]


def test_prazan_cilj_prolazi_bez_pristanka(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from sefsync.models import Base, BusinessUnit
    from sefsync.services.seoba import prebaci, pregled_odredista

    izvor = create_engine(f"sqlite:///{tmp_path / 'i2.db'}")
    cilj = create_engine(f"sqlite:///{tmp_path / 'c2.db'}")
    Base.metadata.create_all(izvor)
    with Session(izvor) as s:
        s.add(BusinessUnit(code="MP001", name="Sombor"))
        s.commit()

    assert pregled_odredista(cilj) == {}
    assert prebaci(izvor, cilj).prepisano["business_unit"] == 1
