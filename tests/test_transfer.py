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


def test_ugaseno_pravilo_se_gasi_i_na_odredistu(db, izvor):
    """Server ima pravilo aktivno, izvoz kaze da je ugaseno — mora da se ugasi.

    Ovo je slucaj #47 ("Srpskih vladara 46" — adresa sedista, tacnost 23,8 %):
    ugaseno je ovde posle merenja, a na serveru bi bez ovoga i dalje razvrstavalo.
    """
    podaci = izvezi()
    for pravilo in podaci["pravila"]:
        if pravilo["pattern"] == "Srpskih vladara 46":
            pravilo["active"] = False

    brojac = uvezi(podaci)

    assert brojac["pravila_ugasena"] == 1
    assert brojac["pravila_postojeca"] == 2
    with db.session_scope() as session:
        pravilo = session.scalar(
            select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
        )
        assert pravilo.active is False


def test_ponovo_upaljeno_pravilo_se_pali(db, izvor):
    """Isto vazi u drugom smeru — inace bi ispravka morala rucno na svaku instalaciju."""
    with db.session_scope() as session:
        session.scalar(
            select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
        ).active = False

    podaci = izvezi()
    for pravilo in podaci["pravila"]:
        if pravilo["pattern"] == "Srpskih vladara 46":
            pravilo["active"] = True

    brojac = uvezi(podaci)

    assert brojac["pravila_upaljena"] == 1
    with db.session_scope() as session:
        pravilo = session.scalar(
            select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
        )
        assert pravilo.active is True


def test_duplikati_istog_pravila_se_gase_svi(db, izvor):
    """Uvoz je uzimao "prvi koji nadje", pa je svaki prolaz palio jedan a gasio drugi.

    Na serveru se to videlo kao "pravila ugasena: 1, pravila upaljena: 1" pri
    svakom uvozu, iako se nista nije menjalo.
    """
    with db.session_scope() as session:
        original = session.scalar(
            select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
        )
        session.add(
            RoutingRule(
                priority=original.priority,
                field=original.field,
                op=original.op,
                pattern=original.pattern,
                business_unit_id=original.business_unit_id,
                document_type=original.document_type,
                active=True,
            )
        )

    podaci = izvezi()
    for pravilo in podaci["pravila"]:
        if pravilo["pattern"] == "Srpskih vladara 46":
            pravilo["active"] = False

    brojac = uvezi(podaci)

    with db.session_scope() as session:
        stanja = [
            r.active for r in session.scalars(
                select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
            )
        ]
    assert stanja == [False, False]      # oba reda, ne samo prvi
    assert brojac["pravila_upaljena"] == 0


def test_pravila_za_razlicite_dobavljace_nisu_isto_pravilo(db, izvor):
    """Dobavljac mora da bude deo kljuca - inace se rudarena pravila gube.

    Na serveru se to videlo tek 16.09.: 174 od 232 pravila vezana su za dobavljaca,
    a uvoz ih je prepoznavao samo po (polje, operator, sablon, PJ, vrsta). Prvo bi
    proslo, a ostala bi se prebrojala kao "vec postoji" i nikad ne bi stigla.
    """
    podaci = izvezi()
    sablon = next(p for p in podaci["pravila"] if p["pattern"] == "Srpskih vladara 46")
    podaci["pravila"] = [
        {**sablon, "supplier_vat": "100000104"},
        {**sablon, "supplier_vat": "106665031"},
    ]
    with db.session_scope() as session:
        session.query(RoutingRule).delete()

    brojac = uvezi(podaci)

    assert brojac["pravila_nova"] == 2
    with db.session_scope() as session:
        dobavljaci = {
            r.supplier_vat for r in session.scalars(
                select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
            )
        }
    assert dobavljaci == {"100000104", "106665031"}


def test_sablon_se_poredi_bez_obzira_na_velicinu_slova(db, izvor):
    """SQLite je razlikovao velicinu slova a MS SQL nije - pa se stanje razilazilo.

    Razvrstavanje ionako sve diže na velika slova, pa su "Srpskih vladara 46" i
    "SRPSKIH VLADARA 46" isto pravilo i moraju imati isto stanje.
    """
    podaci = izvezi()
    for pravilo in podaci["pravila"]:
        if pravilo["pattern"] == "Srpskih vladara 46":
            pravilo["pattern"] = "SRPSKIH VLADARA 46"
            pravilo["active"] = False

    brojac = uvezi(podaci)

    assert brojac["pravila_nova"] == 0        # nije napravljeno drugo pravilo
    assert brojac["pravila_ugasena"] == 1
    with db.session_scope() as session:
        pravilo = session.scalar(
            select(RoutingRule).where(RoutingRule.pattern == "Srpskih vladara 46")
        )
        assert pravilo.active is False
