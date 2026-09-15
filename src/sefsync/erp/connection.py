"""Konekcija ka ERP bazi (MS SQL) - koristi se u fazi 2.

U fazi 1 sluzi samo za proveru pristupa i za istrazivanje seme
(`sefsync erp-check`, `sefsync erp-tables`) kako bi se mapiranje kalkulacije
radilo nad stvarnim tabelama, a ne po pretpostavci.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from ..config import get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None
_test_engine: Engine | None = None


class ErpNotConfigured(RuntimeError):
    pass


def resolve_erp_url() -> str | None:
    """Veza ka ERP bazi: prvo izricito podesena, pa user-secrets skladiste."""
    settings = get_settings()
    if settings.bu_source_db_url:
        return settings.bu_source_db_url
    if settings.erp_db_url:
        return settings.erp_db_url
    if settings.erp_secrets_file:
        from .dotnet_secrets import SecretsError, load_connection_url

        try:
            return load_connection_url(settings.erp_secrets_file, settings.erp_secrets_key)
        except SecretsError as exc:
            log.warning("ERP konekcija iz secrets fajla nije učitana: %s", exc)
    return None


def get_erp_engine() -> Engine:
    global _engine
    if _engine is None:
        url = resolve_erp_url()
        if not url:
            raise ErpNotConfigured(
                "Veza ka ERP bazi nije podešena. Postavi ERP_DB_URL, ili ERP_SECRETS_FILE "
                "(putanja do secrets.json projekta koji već drži kredencijale)."
            )
        _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


def resolve_erp_test_url() -> str | None:
    """Veza ka test kopiji ERP baze.

    Ako je zadato samo ime baze (podrazumevano ELBSX_2026), uzima se produkciona
    konekcija sa zamenjenim imenom - da se lozinka ne drzi na dva mesta.
    """
    settings = get_settings()
    if settings.erp_test_db_url:
        return settings.erp_test_db_url
    ime = settings.erp_test_db_name
    if not ime:
        return None
    osnovna = resolve_erp_url()
    if not osnovna:
        return None
    url = make_url(osnovna)
    # Ime baze stoji ili u putanji, ili u ODBC parametru "database".
    if url.database:
        url = url.set(database=ime)
    elif "database" in {k.lower() for k in url.query}:
        upit = {k: (ime if k.lower() == "database" else v) for k, v in url.query.items()}
        url = url.set(query=upit)
    else:
        return None
    return url.render_as_string(hide_password=False)


def get_erp_test_engine() -> Engine:
    global _test_engine
    if _test_engine is None:
        url = resolve_erp_test_url()
        if not url:
            raise ErpNotConfigured(
                "Test ERP baza nije podešena. Postavi ERP_TEST_DB_URL ili ERP_TEST_DB_NAME."
            )
        _test_engine = create_engine(url, pool_pre_ping=True, future=True)
    return _test_engine


def erp_engine_za_upis(produkcija: bool = False) -> tuple[Engine, str]:
    """Vraca (engine, ime_baze). Produkcija trazi izricitu dozvolu u podesavanjima."""
    if not produkcija:
        eng = get_erp_test_engine()
    else:
        if not get_settings().erp_allow_production_write:
            raise ErpNotConfigured(
                "Upis u produkcionu ERP bazu nije dozvoljen. "
                "Postavi ERP_ALLOW_PRODUCTION_WRITE=true kad budeš siguran."
            )
        eng = get_erp_engine()
    with eng.connect() as conn:
        ime = str(conn.execute(text("SELECT DB_NAME()")).scalar())
    return eng, ime


def check_connection() -> str:
    """Vraca verziju SQL Servera ili baca izuzetak."""
    with get_erp_engine().connect() as conn:
        return str(conn.execute(text("SELECT @@VERSION")).scalar())


def list_tables(schema: str | None = None, like: str | None = None) -> list[str]:
    inspector = inspect(get_erp_engine())
    names = inspector.get_table_names(schema=schema)
    if like:
        needle = like.lower()
        names = [n for n in names if needle in n.lower()]
    return sorted(names)


def describe_table(name: str, schema: str | None = None) -> list[dict]:
    inspector = inspect(get_erp_engine())
    return [
        {
            "column": col["name"],
            "type": str(col["type"]),
            "nullable": col.get("nullable"),
            "default": col.get("default"),
        }
        for col in inspector.get_columns(name, schema=schema)
    ]
