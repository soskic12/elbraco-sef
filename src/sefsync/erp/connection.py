"""Konekcija ka ERP bazi (MS SQL) - koristi se u fazi 2.

U fazi 1 sluzi samo za proveru pristupa i za istrazivanje seme
(`sefsync erp-check`, `sefsync erp-tables`) kako bi se mapiranje kalkulacije
radilo nad stvarnim tabelama, a ne po pretpostavci.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from ..config import get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None


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
