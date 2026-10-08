"""Uvoz sifarnika poslovnih jedinica iz CSV-a i iz postojece tabele na serveru."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select, text

from sefsync.config import get_settings
from sefsync.models import BusinessUnit, UnitKind
from sefsync.services.units import import_csv, sync_from_source, upsert_units

CSV = """code;name;kind;erp_code;address;city;emails;phones
MP01;Maloprodaja Centar;maloprodaja;01;Kneza Miloša 5;Beograd;mp01@elbraco.rs;+381641111111
MAG1;Centralni magacin;magacin;90;Industrijska 1;Beograd;magacin@elbraco.rs;
"""


def test_uvoz_iz_csv(db, tmp_path):
    path = tmp_path / "pj.csv"
    path.write_text(CSV, encoding="utf-8")

    result = import_csv(path)

    assert (result.created, result.updated) == (2, 0)
    with db.session_scope() as session:
        mp = session.scalar(select(BusinessUnit).where(BusinessUnit.code == "MP01"))
        assert mp.name == "Maloprodaja Centar"
        assert mp.kind is UnitKind.RETAIL
        assert mp.erp_code == "01"
        assert mp.email_list() == ["mp01@elbraco.rs"]
        assert session.scalar(
            select(BusinessUnit).where(BusinessUnit.code == "MAG1")
        ).kind is UnitKind.WAREHOUSE


def test_ponovni_uvoz_azurira_ne_duplira(db, tmp_path):
    path = tmp_path / "pj.csv"
    path.write_text(CSV, encoding="utf-8")
    import_csv(path)

    path.write_text(CSV.replace("Maloprodaja Centar", "MP Centar — Knez Miloš"), encoding="utf-8")
    result = import_csv(path)

    assert (result.created, result.updated) == (0, 2)
    with db.session_scope() as session:
        assert len(list(session.scalars(select(BusinessUnit)))) == 2
        assert session.scalar(
            select(BusinessUnit).where(BusinessUnit.code == "MP01")
        ).name == "MP Centar — Knez Miloš"


def test_nepoznat_tip_daje_upozorenje_ali_ne_pada(db):
    result = upsert_units([{"code": "X1", "name": "Nešto", "kind": "kiosk"}])

    assert result.created == 1
    assert any("kiosk" in w for w in result.warnings)
    with db.session_scope() as session:
        assert session.scalar(select(BusinessUnit).where(BusinessUnit.code == "X1")).kind is UnitKind.RETAIL


def test_sinhronizacija_iz_tabele_na_serveru(db, tmp_path, monkeypatch):
    """Izvorna tabela se cita proizvoljnim upitom - ovde SQLite umesto MS SQL-a."""
    source_url = f"sqlite:///{(tmp_path / 'izvor.db').as_posix()}"
    engine = create_engine(source_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE PoslovneJedinice (sifra TEXT, naziv TEXT, tip TEXT, mesto TEXT, email TEXT, aktivna INT)"))
        conn.execute(text(
            "INSERT INTO PoslovneJedinice VALUES "
            "('MP02','Maloprodaja Pančevo','maloprodaja','Pančevo','mp02@elbraco.rs',1),"
            "('MAG1','Centralni magacin','magacin','Beograd','magacin@elbraco.rs',1)"
        ))
    engine.dispose()

    settings = get_settings()
    monkeypatch.setattr(settings, "bu_source_db_url", source_url)
    monkeypatch.setattr(
        settings,
        "bu_source_sql",
        "SELECT sifra AS code, naziv AS name, tip AS kind, sifra AS erp_code, "
        "mesto AS city, email AS emails, aktivna AS active FROM PoslovneJedinice",
    )

    result = sync_from_source()

    assert result.created == 2
    with db.session_scope() as session:
        mp = session.scalar(select(BusinessUnit).where(BusinessUnit.code == "MP02"))
        assert mp.city == "Pančevo"
        assert mp.active is True


def test_sync_bez_konfiguracije_jasno_prijavljuje(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bu_source_sql", None)

    with pytest.raises(ValueError, match="BU_SOURCE_SQL"):
        sync_from_source()


def test_prazna_vrednost_iz_izvora_ne_brise_rucni_unos(db):
    """Šifarnik u ERP-u ima rupe; ono što je operater dopunio mora da preživi sync."""
    upsert_units([{"code": "MP071", "name": "OLD BRICK PUB", "city": "Sombor"}])
    upsert_units([{"code": "MP071", "name": "OLD BRICK PUB", "city": None, "address": "Matije Gupca 55"}])

    with db.session_scope() as session:
        pub = session.scalar(select(BusinessUnit).where(BusinessUnit.code == "MP071"))
        assert pub.city == "Sombor"              # ručni unos sačuvan
        assert pub.address == "Matije Gupca 55"  # nova vrednost iz izvora upisana


# --------------------------------------------------------------------------- #
# dvostruko kodiranje u imenu jedinice
# --------------------------------------------------------------------------- #

def test_razmotava_dvostruko_kodirano_ime():
    """„UPRAVA â€” FINANSIJE“ je ono što je Bogdan video, dvaput.

    Izvor (BU_SOURCE_SQL u .env-u na serveru) drži već pokvaren tekst, pa ga je
    svaki pj-sync ponovo upisivao a pravila-uvoz --i-jedinice popravljao.
    """
    from sefsync.services.units import ispravi_mojibake

    assert ispravi_mojibake("UPRAVA \u00e2\u20ac\u201d FINANSIJE") == "UPRAVA \u2014 FINANSIJE"
    assert ispravi_mojibake("\u00c5\u00a0ljiva") == "\u0160ljiva"
    assert ispravi_mojibake("Vrba\u00c5\u00a1") == "Vrba\u0161"


def test_ispravan_tekst_se_ne_dira():
    """Srpska latinica i obične reči moraju da prođu nedirnute."""
    from sefsync.services.units import ispravi_mojibake

    for tekst in (
        "UPRAVA \u2014 FINANSIJE",
        "Ma\u0161ina za \u0161ivenje",
        "Save Kova\u010devi\u0107a 1B",
        "Old Brick Pub",
        "\u0110ur\u0111evi\u0107",
        "MAG010",
        "",
    ):
        assert ispravi_mojibake(tekst) == tekst


def test_ime_jedinice_se_cisti_pri_uvozu():
    from sefsync.services.units import _clean

    assert _clean("  UPRAVA \u00e2\u20ac\u201d FINANSIJE  ") == "UPRAVA \u2014 FINANSIJE"
