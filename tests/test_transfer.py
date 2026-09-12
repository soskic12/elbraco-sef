"""Prenos pravila sa razvojne masine na server."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sefsync.models import (
    BusinessUnit,
    DocumentType,
    ItemMapping,
    MatchField,
    MatchOp,
    RoutingRule,
    UnitKind,
)
from sefsync.services.transfer import izvezi, uvezi


@pytest.fixture
def izvor(db):
    with db.session_scope() as session:
        mp = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002",
                          city="Apatin", postal_code="25260")
        office = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add_all([mp, office])
        session.flush()
        session.add_all([
            RoutingRule(priority=20, field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
                        pattern="Srpskih vladara 46", business_unit_id=mp.id),
            RoutingRule(priority=60, field=MatchField.SUPPLIER_VAT, op=MatchOp.EQUALS,
                        pattern="100001159", business_unit_id=office.id,
                        comment="Banca Intesa"),
            RoutingRule(priority=90, field=MatchField.ANY_TEXT, op=MatchOp.REGEX, pattern=".",
                        document_type=DocumentType.CREDIT_NOTE, business_unit_id=office.id),
        ])
        session.add(ItemMapping(supplier_vat="100042618", barcode="860012", erp_item_code="10012"))
    return True


def test_izvoz_nosi_sifru_pj_a_ne_interni_id(db, izvor):
    podaci = izvezi()

    assert podaci["verzija"] == 1
    assert {r["business_unit"] for r in podaci["pravila"]} == {"MP002", "OFFICE"}
    assert all("business_unit_id" not in r for r in podaci["pravila"])


def test_uvoz_u_praznu_bazu(db, izvor):
    podaci = izvezi()
    with db.session_scope() as session:  # ocisti sve, kao da je nova instalacija
        session.query(RoutingRule).delete()
        session.query(ItemMapping).delete()
        session.query(BusinessUnit).delete()

    brojac = uvezi(podaci, i_jedinice=True)

    assert brojac["jedinice_nove"] == 2
    assert brojac["pravila_nova"] == 3
    assert brojac["mapiranja_nova"] == 1
    with db.session_scope() as session:
        pravilo = session.scalar(
            select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
        )
        assert pravilo.business_unit.code == "MP002"
        assert pravilo.priority == 20


def test_ponovni_uvoz_ne_duplira(db, izvor):
    podaci = izvezi()
    uvezi(podaci)
    brojac = uvezi(podaci)

    assert brojac["pravila_nova"] == 0
    assert brojac["pravila_postojeca"] == 3


def test_uslov_po_vrsti_dokumenta_prezivi_prenos(db, izvor):
    podaci = izvezi()
    with db.session_scope() as session:
        session.query(RoutingRule).delete()

    uvezi(podaci)

    with db.session_scope() as session:
        pravilo = session.scalar(select(RoutingRule).where(RoutingRule.pattern == "."))
        assert pravilo.document_type is DocumentType.CREDIT_NOTE


def test_pravilo_za_nepoznatu_pj_se_preskace_uz_upozorenje(db, izvor):
    podaci = izvezi()
    podaci["pravila"].append({
        "priority": 10, "field": "delivery_city", "op": "equals", "pattern": "Niš",
        "supplier_vat": None, "document_type": None, "business_unit": "MP999",
        "active": True, "comment": None,
    })

    brojac = uvezi(podaci)

    assert brojac["preskoceno"] == 1
