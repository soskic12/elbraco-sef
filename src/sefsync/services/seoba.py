"""Prebacivanje baze aplikacije na drugi server (SQLite -> MS SQL).

Dokumenti se mogu ponovo preuzeti sa SEF-a, ali ne i rad koji je vec ura(c)en:
razvrstavanje, prosle(dj)eno, potvrde prijema, istorija radnji. Zato se baza
seli, a ne pravi iznova.

Tabele se prepisuju redom kojim ih SQLAlchemy sortira po stranim kljucevima,
sa zadrzavanjem istih identifikatora - da veze izme(dj)u dokumenata, stavki i
obavestenja ostanu iste. Na MS SQL-u to trazi IDENTITY_INSERT.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import Engine

from ..models import Base

log = logging.getLogger(__name__)

PAKET = 500  # koliko redova po upisu


@dataclass
class Izvestaj:
    prepisano: dict[str, int] = field(default_factory=dict)
    preskoceno: dict[str, int] = field(default_factory=dict)

    @property
    def ukupno(self) -> int:
        return sum(self.prepisano.values())

    def tekst(self) -> str:
        redovi = [f"  {tabela:<18} {broj:>7}" for tabela, broj in self.prepisano.items() if broj]
        for tabela, broj in self.preskoceno.items():
            redovi.append(f"  {tabela:<18} {broj:>7}  (preskočeno - već postoji)")
        redovi.append(f"  {'UKUPNO':<18} {self.ukupno:>7}")
        return "\n".join(redovi)


def _je_mssql(engine: Engine) -> bool:
    return engine.dialect.name == "mssql"


class SeobaNijeBezbedna(RuntimeError):
    """Cilj nije prazan - prepisivanje bi dalo mesanu bazu."""


def pregled_odredista(odrediste: Engine) -> dict[str, int]:
    """Koliko redova cilj vec ima, po tabeli. Samo cita (i napravi semu ako je nema)."""
    Base.metadata.create_all(odrediste)
    nalaz: dict[str, int] = {}
    with odrediste.connect() as veza:
        for tabela in Base.metadata.sorted_tables:
            broj = veza.execute(select(func.count()).select_from(tabela)).scalar_one()
            if broj:
                nalaz[tabela.name] = broj
    return nalaz


def prebaci(izvor: Engine, odrediste: Engine, prepisi: bool = False) -> Izvestaj:
    """Prepisuje sve tabele iz jedne baze u drugu.

    **Staje ako cilj nije prazan.** Do 08.10. je tabelu koja vec ima redova tiho
    preskakala, pa bi se dobila mesana baza: deo tabela iz stare, deo iz nove, a
    strani kljucevi pokazuju na id-ove kojih u drugoj tabeli nema. To je gore od
    neuradjene seobe i tesko se prepoznaje. Bane, 08.10.: "NE SMEMO UCI U
    REGRESIJU".

    `prepisi=True` je izricit pristanak da se cilj obrise i upise iznova.
    """
    if not prepisi:
        vec_ima = pregled_odredista(odrediste)
        if vec_ima:
            spisak = ", ".join(f"{t} ({n})" for t, n in sorted(vec_ima.items()))
            raise SeobaNijeBezbedna(
                "Ciljna baza nije prazna: " + spisak + ". Seoba bi preskočila te "
                "tabele i ostavila mešanu bazu — deo podataka iz stare, deo iz "
                "nove, sa vezama koje pokazuju u prazno. Isprazni cilj ili uzmi "
                "drugu bazu; „--prepisi“ briše i upisuje iznova, ali tek kad si "
                "siguran da u cilju nema ničega što ti treba."
            )
    Base.metadata.create_all(odrediste)
    izvestaj = Izvestaj()
    inspektor = inspect(izvor)

    with izvor.connect() as veza_izvor, odrediste.begin() as veza_cilj:
        for tabela in Base.metadata.sorted_tables:
            if not inspektor.has_table(tabela.name):
                continue

            postojeci = veza_cilj.execute(
                select(func.count()).select_from(tabela)
            ).scalar_one()
            if postojeci and not prepisi:
                izvestaj.preskoceno[tabela.name] = postojeci
                continue
            if postojeci and prepisi:
                veza_cilj.execute(tabela.delete())

            redovi = [dict(r) for r in veza_izvor.execute(select(tabela)).mappings()]
            if not redovi:
                izvestaj.prepisano[tabela.name] = 0
                continue

            ima_identitet = _je_mssql(odrediste) and any(c.autoincrement and c.primary_key
                                                         for c in tabela.columns)
            if ima_identitet:
                veza_cilj.execute(text(f"SET IDENTITY_INSERT {tabela.name} ON"))
            try:
                for pocetak in range(0, len(redovi), PAKET):
                    veza_cilj.execute(insert(tabela), redovi[pocetak:pocetak + PAKET])
            finally:
                if ima_identitet:
                    veza_cilj.execute(text(f"SET IDENTITY_INSERT {tabela.name} OFF"))

            izvestaj.prepisano[tabela.name] = len(redovi)
            log.info("Prepisano %s: %s redova", tabela.name, len(redovi))

    return izvestaj


def prebaci_na(url_odredista: str, url_izvora: str | None = None, prepisi: bool = False) -> Izvestaj:
    from ..db import get_engine

    izvor = create_engine(url_izvora) if url_izvora else get_engine()
    odrediste = create_engine(url_odredista, pool_pre_ping=True)
    try:
        return prebaci(izvor, odrediste, prepisi=prepisi)
    finally:
        odrediste.dispose()
        if url_izvora:
            izvor.dispose()
