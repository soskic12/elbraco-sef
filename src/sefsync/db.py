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

# Ko trenutno drzi vezu ka bazi panela: id veze -> (kad je uzeta, gde).
#
# Bazen se dvaput napunio u produkciji i ljudi nisu mogli da se prijave, a iz
# koda se nije videlo ko ga drzi: nijedna sesija se ne otvara bez `with`. Zato
# se pri svakom uzimanju veze zabelezi mesto u nasem kodu, pa `/health/veze`
# moze da kaze sta stoji i koliko dugo. Cena je jedan prolaz kroz stek po
# uzimanju veze - zanemarljivo prema tome da panel pada.
_uzete: dict[int, tuple[float, str]] = {}

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
            kwargs["pool_size"] = s.db_pool_size
            kwargs["max_overflow"] = s.db_max_overflow
            kwargs["pool_timeout"] = s.db_pool_timeout
            kwargs["pool_recycle"] = s.db_pool_recycle
        _engine = create_engine(s.db_url, **kwargs)
        _prati_veze(_engine)
        # SQLite zaklju(c)ava celu bazu pri upisu. Dok je jedan covek i par
        # hiljada dokumenata godisnje - dovoljno je, i tako je i odluceno
        # 21.09. u `.env.server`. Kad je ukljucen upis u produkciju, znaci da
        # radi cela firma: 08.10. je bilo 8.467 dokumenata u 27 dana, 95
        # jedinica i petnaestominutni ciklus uz IIS. Zato ovde stoji red u logu
        # pri svakom pokretanju - da odluka ne ostane neprimecena kao do sada.
        if s.db_url.startswith("sqlite") and s.erp_allow_production_write:
            log.warning(
                "Panel radi na SQLite (%s), a upis u produkciju je uključen. "
                "Više ljudi i ciklus se bore za istu datoteku — prebaci na MS SQL "
                "(`sefsync prebaci-bazu`, pa DB_URL u .env).",
                s.db_url.split("///")[-1],
            )
    return _engine


def _kratko(putanja: str) -> str:
    """Samo deo putanje unutar paketa, da red stane u jedan ispis."""
    rep = putanja.rsplit("sefsync", 1)[-1]
    return rep.replace("\\", "/").lstrip("/")


def _prati_veze(engine: Engine) -> None:
    """Belezi odakle je veza uzeta i koji upiti traju predugo."""
    import time
    import traceback

    from sqlalchemy import event

    @event.listens_for(engine, "checkout")
    def _uzeta(dbapi_con, zapis, proxy):  # noqa: ANN001, ARG001
        mesta = [
            f"{_kratko(r.filename)}:{r.lineno} {r.name}"
            for r in traceback.extract_stack()[:-1]
            if "sefsync" in r.filename and not r.filename.endswith("db.py")
        ]
        _uzete[id(zapis)] = (time.monotonic(), " < ".join(reversed(mesta[-4:])) or "?")

    @event.listens_for(engine, "checkin")
    def _vracena(dbapi_con, zapis):  # noqa: ANN001, ARG001
        _uzete.pop(id(zapis), None)

    # Spor upit je jedini nacin da se sazna sta stvarno traje. Merac veza kaze
    # gde je veza uzeta, a ne gde se zaglavila - a stranica reda je drzala vezu
    # pedesetak sekundi, sto je oborilo bazen.
    prag = get_settings().log_spor_upit_s

    @event.listens_for(engine, "before_cursor_execute")
    def _pre(conn, cursor, izraz, parametri, kontekst, mnogo):  # noqa: ANN001, ARG001
        conn.info["_pocetak"] = time.monotonic()

    @event.listens_for(engine, "after_cursor_execute")
    def _posle(conn, cursor, izraz, parametri, kontekst, mnogo):  # noqa: ANN001, ARG001
        pocetak = conn.info.pop("_pocetak", None)
        if pocetak is None:
            return
        trajalo = time.monotonic() - pocetak
        if trajalo >= prag:
            log.warning("Spor upit %.1fs: %s", trajalo, " ".join(izraz.split())[:300])


def ko_drzi_veze(najstarijih: int = 10) -> list[dict]:
    """Veze koje su trenutno uzete, najduze zauzete prve."""
    import time

    sada = time.monotonic()
    redovi = [
        {"sekundi": round(sada - kad, 1), "gde": gde}
        for kad, gde in _uzete.values()
    ]
    redovi.sort(key=lambda r: -r["sekundi"])
    return redovi[:najstarijih]


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


def _alter_sirina(tabela: str, kolona: str, tip: str, duzina: int, nullable: bool) -> str:
    """ALTER koji siri kolonu, a zadrzava njen tip i NOT NULL."""
    kraj = "NULL" if nullable else "NOT NULL"
    return f"ALTER TABLE {tabela} ALTER COLUMN {kolona} {tip}({duzina}) {kraj}"


def prosiri_uske_kolone() -> list[str]:
    """MS SQL: siri kolone koje su uze nego sto model trazi.

    Enum kolona se pravi kao VARCHAR duzine najduze vrednosti u tom trenutku.
    Kad se enumu doda duza vrednost, stara kolona je vise ne prima - a dopuna
    seme dodaje samo nove kolone i ovo joj promice.

    Mereno 16.09. na serveru: `routing_rule.field` je bio varchar(18) jer je u
    trenutku pravljenja najduza vrednost bila CONTRACT_REFERENCE. Uvoz pravila
    je pukao na ADDITIONAL_REFERENCE (20) porukom o skracivanju, i to tek posle
    pola posla. Sirenje je bezbedno: nijedan postojeci podatak ne staje.
    """
    engine = get_engine()
    if engine.dialect.name != "mssql":
        # SQLite ne primenjuje duzinu, a za druge baze ovo nije provereno.
        return []

    inspector = inspect(engine)
    promene: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            u_bazi = {c["name"]: c for c in inspector.get_columns(table.name)}
            for column in table.columns:
                trazi = getattr(column.type, "length", None)
                postoji = u_bazi.get(column.name)
                if trazi is None or postoji is None:
                    continue
                ima = getattr(postoji["type"], "length", None)
                if ima is None or ima >= trazi:
                    continue
                tip = postoji["type"].__class__.__name__.upper()
                if tip not in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR"):
                    continue
                conn.execute(text(_alter_sirina(
                    table.name, column.name, tip, trazi, bool(postoji.get("nullable", True))
                )))
                promene.append(f"{table.name}.{column.name}: {ima} -> {trazi}")
                log.info("Kolona proširena: %s", promene[-1])
    return promene


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
    promene += prosiri_uske_kolone()
    proveri_unicode()
    return promene
