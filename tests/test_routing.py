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


def _polja(**kw):
    osnovno = {
        "delivery_address": None, "delivery_city": None, "delivery_name": None,
        "buyer_address": None, "buyer_city": None, "buyer_reference": None,
        "order_reference": None, "contract_reference": None, "additional_reference": None,
        "attachment_text": None, "note": None, "supplier_vat": None, "supplier_name": None,
        "document_number": None, "item_text": None, "document_type": "Invoice",
    }
    osnovno.update(kw)
    return osnovno


def test_prilog_ne_ulazi_u_any_text():
    """Tekst PDF priloga sme da odlucuje samo kad ga pravilo izricito trazi.

    Prilog sadrzi i nasu adresu sedista i nazive artikala, pa bi kroz any_text
    prosirio svako pravilo do laznih pogodaka. Mereno: tacnost pada sa 97,4%
    na 91,8% ako se ukljuci.
    """
    pravilo = RoutingRule(
        id=1, priority=10, field=MatchField.ANY_TEXT, op=MatchOp.CONTAINS,
        pattern="BECEJ", business_unit_id=7, active=True,
    )
    engine = RoutingEngine([pravilo])

    # samo u prilogu -> ne sme da se upali
    odluka = engine.decide(_polja(attachment_text="Poslovnica B000067561 Glavna 18 21220 Becej"))
    assert not odluka.assigned

    # u napomeni -> pali se normalno
    odluka = engine.decide(_polja(note="BECEJ po otpremnici 26-30C-1"))
    assert odluka.business_unit_id == 7


def test_pravilo_moze_izricito_da_cilja_prilog():
    pravilo = RoutingRule(
        id=2, priority=10, field=MatchField.ATTACHMENT_TEXT, op=MatchOp.CONTAINS,
        pattern="B000067561", business_unit_id=7, supplier_vat="104211304", active=True,
    )
    engine = RoutingEngine([pravilo])
    odluka = engine.decide(_polja(
        supplier_vat="104211304",
        attachment_text="Poslovnica B000067561 Elbraco group Glavna 18 21220 Becej"))
    assert odluka.business_unit_id == 7
    assert odluka.rule_id == 2
