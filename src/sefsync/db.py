"""SQLAlchemy engine / sesija / lagano usaglasavanje seme."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        kwargs: dict = {"echo": s.db_echo, "future": True}
        if s.db_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_pre_ping"] = True
        _engine = create_engine(s.db_url, **kwargs)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transakcija: commit na izlazu, rollback na gresci."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _default_literal(column) -> str | None:
    """SQL literal podrazumevane vrednosti kolone (za ALTER TABLE ... NOT NULL)."""
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def sync_schema() -> list[str]:
    """Dodaje kolone koje postoje u modelu a nema ih u bazi.

    Namerno mala zamena za alembic: projekat ima jednu bazu i dodaju se samo
    nove kolone. Nista se ne brise niti menja tip - to bi islo rucnom migracijom.
    """
    engine = get_engine()
    inspector = inspect(engine)
    dialect = engine.dialect
    keyword = "COLUMN " if dialect.name == "sqlite" else ""
    changes: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD {keyword}{column.name} "
                ddl += column.type.compile(dialect)
                literal = _default_literal(column)
                if not column.nullable:
                    if literal is None:
                        log.warning(
                            "Kolona %s.%s je NOT NULL bez podrazumevane vrednosti — "
                            "dodajem je kao NULL-abilnu.",
                            table.name,
                            column.name,
                        )
                    else:
                        ddl += f" NOT NULL DEFAULT {literal}"
                elif literal is not None:
                    ddl += f" DEFAULT {literal}"
                conn.execute(text(ddl))
                changes.append(f"{table.name}.{column.name}")
                log.info("Šema dopunjena: %s", changes[-1])
    return changes


def proveri_unicode() -> list[str]:
    """Na MS SQL-u tekstualne kolone moraju biti NVARCHAR.

    VARCHAR ne moze da sacuva cirilicu ("ЈП Водоканал"), a to se ne vidi kao
    greska - slova se tiho pretvore u upitnike. Tip postojece kolone se ne moze
    promeniti dopunom seme, pa je jedini ispravan ishod da covek sazna odmah.
    """
    engine = get_engine()
    if engine.dialect.name != "mssql":
        return []

    inspector = inspect(engine)
    sumnjive: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        ocekivano = {
            c.name for c in table.columns
            if c.type.__class__.__name__ in ("Unicode", "UnicodeText", "String", "Text")
        }
        for kolona in inspector.get_columns(table.name):
            if kolona["name"] not in ocekivano:
                continue
            tip = str(kolona["type"]).upper()
            if tip.startswith("VARCHAR") or tip.startswith("TEXT"):
                sumnjive.append(f"{table.name}.{kolona['name']} ({tip})")
    if sumnjive:
        log.warning(
            "Kolone su VARCHAR umesto NVARCHAR (%s): cirilica se nece sacuvati. "
            "Tip se ne moze promeniti dopunom seme - bazu treba napraviti iznova.",
            len(sumnjive),
        )
    return sumnjive


def init_db() -> list[str]:
    Base.metadata.create_all(get_engine())
    promene = sync_schema()
    proveri_unicode()
    return promene
