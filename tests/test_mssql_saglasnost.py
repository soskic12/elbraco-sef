"""Upiti moraju da rade i na MS SQL-u, ne samo na SQLite-u.

Razvija se nad SQLite-om, a radi se nad MS SQL-om - i te dve baze se ne slazu
oko svega. Najskuplji primer: `kolona.is_(False)` SQLite prihvata, a SQL Server
javlja "Incorrect syntax near '0'", jer u T-SQL-u `IS` ide samo uz NULL i nema
logickog tipa. Greska se vidi tek na serveru, nad pravim podacima.

Ovde se upiti prevode u T-SQL bez ikakve baze, pa se takve stvari hvataju odmah.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mssql

from sefsync.models import BusinessUnit, Document, RoutingRule

IZVOR = Path(__file__).resolve().parents[1] / "src" / "sefsync"


def u_tsql(upit) -> str:
    return str(upit.compile(dialect=mssql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize(
    "upit",
    [
        select(Document.id).where(Document.archived == False),  # noqa: E712
        select(Document.id).where(Document.archived == True),  # noqa: E712
        select(RoutingRule).where(RoutingRule.active == False),  # noqa: E712
        select(BusinessUnit).where(
            BusinessUnit.active == True, BusinessUnit.routable == True  # noqa: E712
        ),
    ],
)
def test_logicki_uslovi_se_prevode_u_poredjenje(upit):
    sql = u_tsql(upit)

    assert " IS 0" not in sql and " IS 1" not in sql
    assert "= 0" in sql or "= 1" in sql


def test_provera_na_null_ostaje_is_null():
    """`is_(None)` je i dalje ispravno - menja se samo logicki tip."""
    sql = u_tsql(select(Document.id).where(Document.business_unit_id.is_(None)))

    assert "IS NULL" in sql


def test_nigde_u_kodu_nema_is_true_ili_is_false():
    """Cuvar za ubuduce: jedan takav poziv obori rad nad MS SQL-om."""
    greske = []
    for putanja in IZVOR.rglob("*.py"):
        for broj, red in enumerate(putanja.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.is_\(\s*(True|False)\s*\)", red):
                greske.append(f"{putanja.relative_to(IZVOR)}:{broj}")

    assert not greske, "logicki uslov preko is_() ne radi na MS SQL-u: " + ", ".join(greske)


def test_tekstualne_kolone_su_unicode():
    """Medju dobavljacima ima cirilice - VARCHAR je ne bi sacuvao."""
    from sqlalchemy.schema import CreateTable

    sql = str(CreateTable(Document.__table__).compile(dialect=mssql.dialect()))
    latinicne = [
        red.strip() for red in sql.splitlines()
        if "VARCHAR" in red and "NVARCHAR" not in red
    ]

    # jedino sifarnicka polja (Enum) smeju da budu VARCHAR - u njima su
    # unapred poznate ASCII vrednosti
    for red in latinicne:
        assert "VARCHAR(8)" in red or "VARCHAR(1" in red, f"ne-Unicode kolona: {red}"


def test_duga_polja_nisu_zastareli_ntext():
    from sqlalchemy.schema import CreateTable

    sql = str(CreateTable(Document.__table__).compile(dialect=mssql.dialect()))

    assert "NTEXT" not in sql
    assert "NVARCHAR(max)" in sql


def test_servis_i_komandna_linija_pisu_u_razlicite_logove(tmp_path, monkeypatch):
    """Windows ne da da se otvoren fajl preimenuje, pa bi rotacija pucala."""
    import logging

    from sefsync import logging_setup
    from sefsync.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "storage_dir", tmp_path)
    monkeypatch.setattr(s, "log_file", None)

    imena = []
    for naziv in ("sefsync", "sefsync-cli"):
        koren = logging.getLogger()
        stari = koren.handlers[:]
        koren.handlers.clear()
        try:
            logging_setup.setup_logging(naziv=naziv)
            imena += [
                h.baseFilename for h in koren.handlers if hasattr(h, "baseFilename")
            ]
        finally:
            for h in koren.handlers:
                h.close()
            koren.handlers[:] = stari

    assert len(set(imena)) == 2, f"isti fajl za oba procesa: {imena}"


def test_provera_prijavljuje_varchar_kolone(monkeypatch):
    """Da se ovo vidi odmah, a ne tek kad neko potraži dobavljača."""
    from sefsync import db as db_modul

    class LazniInspektor:
        def has_table(self, ime):
            return ime == "document"

        def get_columns(self, ime):
            return [
                {"name": "supplier_name", "type": "VARCHAR(300)"},
                {"name": "delivery_address", "type": "NVARCHAR(300)"},
                {"name": "amount", "type": "FLOAT"},
            ]

    class LazniEngine:
        class dialect:
            name = "mssql"

    monkeypatch.setattr(db_modul, "get_engine", lambda: LazniEngine())
    monkeypatch.setattr(db_modul, "inspect", lambda engine: LazniInspektor())

    sumnjive = db_modul.proveri_unicode()

    assert sumnjive == ["document.supplier_name (VARCHAR(300))"]


def test_provera_cuti_na_sqlite(db):
    """SQLite nema tu razliku, pa nema ni sta da prijavi."""
    from sefsync.db import proveri_unicode

    assert proveri_unicode() == []
