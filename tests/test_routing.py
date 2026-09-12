from sefsync.models import BusinessUnit, MatchField, MatchOp, RoutingRule, RoutingSource, UnitKind
from sefsync.routing import RoutingEngine
from sefsync.textutil import normalize
from sefsync.ubl import parse_ubl


def _units(session):
    mp = BusinessUnit(code="MP02", name="Maloprodaja Pančevo", kind=UnitKind.RETAIL)
    mag = BusinessUnit(code="MAG1", name="Centralni magacin", kind=UnitKind.WAREHOUSE)
    session.add_all([mp, mag])
    session.flush()
    return mp, mag


def test_normalizacija_cirilice():
    assert normalize("Војводе Мишића 12") == "VOJVODE MISICA 12"
    assert normalize("Vojvode Mišića br. 12") == "VOJVODE MISICA BR 12"
    assert normalize("  Đački   trg  ") == "DJACKI TRG"


def test_pravilo_po_adresi_isporuke_radi_i_za_cirilicu(db, faktura_xml):
    with db.session_scope() as session:
        mp, _ = _units(session)
        session.add(
            RoutingRule(
                priority=10,
                field=MatchField.DELIVERY_ADDRESS,
                op=MatchOp.CONTAINS,
                pattern="Vojvode Misica 12",   # pravilo pisano latinicom
                business_unit_id=mp.id,
            )
        )
        session.flush()

        engine = RoutingEngine.from_db(session)
        decision = engine.decide(parse_ubl(faktura_xml).routing_fields())

        assert decision.business_unit_id == mp.id
        assert decision.source is RoutingSource.RULE


def test_prioritet_odlucuje(db, faktura_xml):
    with db.session_scope() as session:
        mp, mag = _units(session)
        session.add_all(
            [
                RoutingRule(priority=90, field=MatchField.DELIVERY_CITY, op=MatchOp.CONTAINS,
                            pattern="Pancevo", business_unit_id=mag.id),
                RoutingRule(priority=10, field=MatchField.BUYER_REFERENCE, op=MatchOp.EQUALS,
                            pattern="MP-02", business_unit_id=mp.id),
            ]
        )
        session.flush()

        decision = RoutingEngine.from_db(session).decide(parse_ubl(faktura_xml).routing_fields())
        assert decision.business_unit_id == mp.id


def test_pravilo_ograniceno_na_dobavljaca(db, faktura_xml):
    with db.session_scope() as session:
        _, mag = _units(session)
        session.add(
            RoutingRule(priority=10, field=MatchField.ANY_TEXT, op=MatchOp.CONTAINS,
                        pattern="Kabl", supplier_vat="999999999", business_unit_id=mag.id)
        )
        session.flush()

        decision = RoutingEngine.from_db(session).decide(parse_ubl(faktura_xml).routing_fields())
        assert decision.business_unit_id is None  # PIB se ne poklapa


def test_regex_nad_brojem_narudzbenice(db, faktura_xml):
    with db.session_scope() as session:
        mp, _ = _units(session)
        session.add(
            RoutingRule(priority=10, field=MatchField.ORDER_REFERENCE, op=MatchOp.REGEX,
                        pattern=r"^NAR-\d{4}$", business_unit_id=mp.id)
        )
        session.flush()

        decision = RoutingEngine.from_db(session).decide(parse_ubl(faktura_xml).routing_fields())
        assert decision.business_unit_id == mp.id


def test_bez_pravila_dokument_je_nerazvrstan(db, odobrenje_xml):
    with db.session_scope() as session:
        _units(session)
        session.flush()

        decision = RoutingEngine.from_db(session).decide(parse_ubl(odobrenje_xml).routing_fields())
        assert not decision.assigned
        assert decision.source is RoutingSource.NONE


def test_jedina_poslovna_jedinica_uzima_sve(db, odobrenje_xml):
    with db.session_scope() as session:
        only = BusinessUnit(code="MAG1", name="Magacin", kind=UnitKind.WAREHOUSE)
        session.add(only)
        session.flush()

        decision = RoutingEngine.from_db(session).decide(parse_ubl(odobrenje_xml).routing_fields())
        assert decision.business_unit_id == only.id
        assert decision.source is RoutingSource.SINGLE_UNIT


def test_pravilo_moze_da_vazi_samo_za_jednu_vrstu_dokumenta(db, faktura_xml, odobrenje_xml):
    """Rabatna knjižna odobrenja idu u finansije, a fakture istog dobavljača u objekat."""
    from sefsync.models import DocumentType

    with db.session_scope() as session:
        mp, office = _units(session)
        session.add_all(
            [
                RoutingRule(priority=10, field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
                            pattern="Vojvode Misica 12", business_unit_id=mp.id),
                RoutingRule(priority=60, field=MatchField.SUPPLIER_VAT, op=MatchOp.EQUALS,
                            pattern="100200300", document_type=DocumentType.CREDIT_NOTE,
                            business_unit_id=office.id),
            ]
        )
        session.flush()
        engine = RoutingEngine.from_db(session)

        faktura = engine.decide(parse_ubl(faktura_xml).routing_fields())
        odobrenje = engine.decide(parse_ubl(odobrenje_xml).routing_fields())

        assert faktura.business_unit_id == mp.id       # faktura -> objekat po adresi
        assert odobrenje.business_unit_id == office.id  # KO istog dobavljača -> finansije
