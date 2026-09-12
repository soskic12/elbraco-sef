"""Predlaganje pravila uparivanjem adresa isporuke sa sifarnikom PJ.

Slucajevi su uzeti iz stvarnog uzorka ELBRACO faktura.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sefsync.models import BusinessUnit, MatchField, RoutingRule, UnitKind
from sefsync.routing.suggest import _street_key, apply_suggestions, suggest_rules
from sefsync.ubl.parser import Party, UblDocument


def _doc(address=None, city=None, postal=None, name=None, vat="100200300") -> UblDocument:
    doc = UblDocument()
    doc.delivery_street = address
    doc.delivery_city = city
    doc.delivery_postal_code = postal
    doc.delivery_name = name
    doc.supplier = Party(name="Dobavljač DOO", vat=vat)
    return doc


@pytest.fixture
def units(db):
    with db.session_scope() as session:
        session.add_all(
            [
                # tri jedinice u Somboru - razlikuju se samo po adresi
                BusinessUnit(code="MP001", name="PRODAJNO MESTO SOMBOR", kind=UnitKind.RETAIL,
                             address="Staparski put S16 tel.421 783", city="Sombor", postal_code="25000"),
                BusinessUnit(code="MP008", name="PRODAJNO MESTO SOMBOR 2", kind=UnitKind.RETAIL,
                             address="Vojvođanska 46", city="Sombor", postal_code="25000"),
                BusinessUnit(code="MAG021", name="Centralni magacin SOMBOR", kind=UnitKind.WAREHOUSE,
                             address="Sivački put 30", city="Sombor", postal_code="25000"),
                BusinessUnit(code="MP005", name="PRODAJNO MESTO B.PALANKA", kind=UnitKind.RETAIL,
                             address="Jugoslovenske armije 116", city="B.Palanka", postal_code="21400"),
                BusinessUnit(code="MP009", name="PRODAJNO MESTO SUBOTICA", kind=UnitKind.RETAIL,
                             address="Braće Radić 43", city="Subotica", postal_code="24000"),
                BusinessUnit(code="MP003", name="PRODAJNO MESTO ODZACI", kind=UnitKind.RETAIL,
                             address="Somborska 28", city="Odžaci", postal_code="25250"),
                # tehnicka jedinica - ne sme da bude cilj automatike
                BusinessUnit(code="MP011", name="SOMBOR USLUGA", kind=UnitKind.SERVICE,
                             address="Staparski put S16", city="Sombor", postal_code="25000",
                             routable=False),
            ]
        )
    return True


def test_street_key_cisti_telefon_i_prefikse():
    assert _street_key("Ul. Somborska br. 28") == "SOMBORSKA 28"
    assert _street_key("STAPARSKI PUT S16 tel.421 783") == "STAPARSKI PUT S16"
    assert _street_key("Војводе Мишића 12") == "VOJVODE MISICA 12"


def test_adresa_razlikuje_tri_jedinice_u_istom_gradu(db, units):
    docs = [
        _doc("Staparski put S16", "Sombor", "25000"),
        _doc("Vojvodjanska 46", "Sombor", "25101"),
        _doc("Sivački put 30", "Sombor", "25101"),
    ]
    with db.session_scope() as session:
        report = suggest_rules(session, docs)

    assert [s.unit_code for s in report.suggestions] == ["MP001", "MP008", "MAG021"]
    assert all(s.how == "adresa" for s in report.suggestions)
    assert not report.conflicts


def test_razlicit_ptt_za_isti_grad_ne_kvari_uparivanje(db, units):
    """Dobavljači za Sombor pišu i 25000 i 25101 — grad presuđuje."""
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("Sivački put 30", "Sombor", "25101")])

    assert report.suggestions[0].unit_code == "MAG021"


def test_ista_ulica_u_drugom_mestu_se_ne_uparuje(db, units):
    """'Braće Radić 43' postoji i u Subotici i u Laliću — bez potvrde mesta nema pravila."""
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("Braće Radić 43", "Lalic", "25234")])

    assert not report.suggestions
    assert report.conflicts[0].candidates == ["nema kandidata u šifarniku"]


def test_skraceni_naziv_mesta_ne_smeta(db, units):
    """Šifarnik ima 'B.Palanka', dobavljač šalje 'Bačka Palanka' — PTT presuđuje."""
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("Jugoslovenske armije 116", "Bačka Palanka", "21400")])

    assert report.suggestions[0].unit_code == "MP005"


def test_crtica_u_broju_zgrade(db, units):
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("Staparski put S-16", "SOMBOR", "25000")])

    assert report.suggestions[0].unit_code == "MP001"


def test_ptt_kad_ulica_ne_odgovara(db, units):
    """'Somborski put 28' nije 'Somborska 28', ali PTT 25250 ima samo jednu PJ."""
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("Somborski put 28", "Odzaci", "25250")])

    assert report.suggestions[0].unit_code == "MP003"
    assert report.suggestions[0].how == "ptt"


def test_nepoznata_adresa_u_gradu_sa_vise_pj_je_konflikt(db, units):
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("Matije Gupca 57", "Sombor", "25000")])

    assert not report.suggestions
    conflict = report.conflicts[0]
    assert sorted(c.split()[0] for c in conflict.candidates) == ["MAG021", "MP001", "MP008"]


def test_tehnicka_jedinica_nije_kandidat(db, units):
    """MP011 ima istu adresu kao MP001, ali routable=False."""
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("Staparski put S16", "Sombor", "25000")])

    assert report.suggestions[0].unit_code == "MP001"


def test_dokumenti_bez_isporuke_daju_predlog_po_dobavljacu(db, units):
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc(vat="111111111"), _doc(vat="111111111")])

    assert report.without_delivery == 2
    assert report.supplier_hints[0][1:] == ("111111111", 2)


def test_primena_upisuje_pravila_bez_duplikata(db, units):
    docs = [_doc("Staparski put S16", "Sombor", "25000")] * 3
    with db.session_scope() as session:
        report = suggest_rules(session, docs)
        assert apply_suggestions(session, report) == 1
        assert apply_suggestions(session, report) == 0  # drugi put nema šta da se doda

    with db.session_scope() as session:
        rule = session.scalar(select(RoutingRule))
        assert rule.field is MatchField.DELIVERY_ADDRESS
        assert rule.priority == 20
        assert rule.business_unit.code == "MP001"


def test_jedno_pravilo_pokriva_sve_varijante_zapisa_adrese(db, units):
    """Šablon je ulica i broj, ne cela adresa — inače isti objekat traži 5 pravila."""
    docs = [
        _doc("Glavna 18", "Bečej", "21220"),
        _doc("Glavna 18", "Bečej", None),
        _doc("Glavna 18", "Bečej", "21000"),
    ]
    with db.session_scope() as session:
        # jedinica u Bečeju za ovaj test
        session.add(BusinessUnit(code="MP006", name="PRODAJNO MESTO BECEJ", kind=UnitKind.RETAIL,
                                 address="Glavna 18", city="Bečej", postal_code="21220"))
        session.flush()
        report = suggest_rules(session, docs)

    assert len(report.suggestions) == 1
    assert report.suggestions[0].pattern == "Glavna 18"
    assert report.suggestions[0].count == 3


def test_varijanta_koju_kratak_sablon_ne_hvata_dobija_svoje_pravilo(db, units):
    """'ul.Somborska br.28' ne sadrži 'Somborska 28' — čuva se pun zapis."""
    with db.session_scope() as session:
        report = suggest_rules(session, [_doc("ul.Somborska br.28", "Odžaci", "25250")])

    assert report.suggestions[0].pattern == "ul.Somborska br.28, 25250, Odžaci"
    assert report.suggestions[0].unit_code == "MP003"
