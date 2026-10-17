"""Blagajna cekova: nalozi „da naplati“ / „da isplati“ i dnevnik blagajne.

Zamenjuje tri papirne knjige iz svakog objekta (slike u docs/, 17.10.):

* **Nalog blagajni da naplati** - jedan po danu pazara: „Cekovi pazar 18.05.“
  i zbir cekova primljenih tog dana. Pravi ga poslovodja jednim klikom kad su
  svi cekovi tog dana procitani i provereni.
* **Nalog blagajni da isplati** - jedan po odlasku u banku: „Cekovi naplata
  20.05.“ i zbir predatih cekova. Nastaje sam, u istom upisu kao predaja.
* **Dnevnik blagajne** - nalozi redom za period, sa saldom od, ukupnim
  primitkom, izdatkom i saldom do. Saldo je ono sto treba da je u sefu.

Pocetni saldo i sledeci brojevi naloga se upisuju jednom, iz papirne knjige,
da se nastavi postojeca numeracija (npr. 098/2026).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, select

from ..db import session_scope
from ..models import (
    AuditLog,
    Cek,
    NalogBlagajne,
    PocetnoStanjeBlagajne,
    StanjeCeka,
    VrstaNaloga,
    utcnow,
)


def postavi_pocetno(
    business_unit_id: int, *, datum: date, saldo: float, prvi_naplati: int,
    prvi_isplati: int, korisnik: str,
) -> None:
    if saldo < 0:
        raise ValueError("Saldo ne može biti negativan.")
    if prvi_naplati < 1 or prvi_isplati < 1:
        raise ValueError("Broj naloga počinje od 1.")
    with session_scope() as session:
        if session.scalar(select(func.count()).select_from(NalogBlagajne)
                          .where(NalogBlagajne.business_unit_id == business_unit_id)):
            raise ValueError("Nalozi su već pravljeni — početno stanje se više ne menja.")
        red = session.get(PocetnoStanjeBlagajne, business_unit_id) or PocetnoStanjeBlagajne(
            business_unit_id=business_unit_id)
        red.datum, red.saldo = datum, round(float(saldo), 2)
        red.prvi_broj_naplati, red.prvi_broj_isplati = int(prvi_naplati), int(prvi_isplati)
        red.upisao, red.upisano_u = korisnik, utcnow()
        session.merge(red)
        session.add(AuditLog(actor=korisnik, action="blagajna.pocetno",
                             detail=f"jedinica {business_unit_id}: {datum} saldo {saldo:,.2f}, "
                                    f"brojevi {prvi_naplati}/{prvi_isplati}"))


def sledeci_broj(session, business_unit_id: int, vrsta: VrstaNaloga, godina: int) -> int:
    """Sledeci broj u godini; prve godine nastavlja papirnu numeraciju."""
    najveci = session.scalar(
        select(func.max(NalogBlagajne.broj)).where(
            NalogBlagajne.business_unit_id == business_unit_id,
            NalogBlagajne.vrsta == vrsta, NalogBlagajne.godina == godina)
    )
    if najveci:
        return najveci + 1
    pocetno = session.get(PocetnoStanjeBlagajne, business_unit_id)
    if pocetno is not None and pocetno.datum.year == godina:
        return pocetno.prvi_broj_naplati if vrsta is VrstaNaloga.NAPLATI else pocetno.prvi_broj_isplati
    return 1


def pazari_bez_naloga(session, business_unit_id: int) -> list[dict]:
    """Dani pazara ciji cekovi jos nisu usli u blagajnu (nalog da naplati)."""
    redovi = session.execute(
        select(Cek.primljeno, func.count(Cek.id), func.sum(Cek.iznos))
        .where(Cek.business_unit_id == business_unit_id, Cek.primljeno.isnot(None),
               Cek.nalog_naplati_id.is_(None))
        .group_by(Cek.primljeno).order_by(Cek.primljeno)
    ).all()
    ishod = []
    for dan, n, iznos in redovi:
        crvenih = sum(1 for c in session.scalars(
            select(Cek).where(Cek.business_unit_id == business_unit_id, Cek.primljeno == dan,
                              Cek.nalog_naplati_id.is_(None))) if c.za_proveru)
        ishod.append({"dan": dan, "komada": n, "iznos": float(iznos or 0), "crvenih": crvenih})
    return ishod


def zakljuci_pazar(business_unit_id: int, dan: date, *, korisnik: str) -> int:
    """Nalog blagajni da naplati za cekove jednog dana pazara. Vraca id naloga."""
    with session_scope() as session:
        cekovi = session.scalars(
            select(Cek).where(Cek.business_unit_id == business_unit_id, Cek.primljeno == dan,
                              Cek.nalog_naplati_id.is_(None))
        ).all()
        if not cekovi:
            raise ValueError(f"Za pazar {dan:%d.%m.%Y} nema čekova bez naloga.")
        if any(c.za_proveru for c in cekovi):
            raise ValueError("Neki ček iz tog pazara ima crvena polja — prvo ih proveri.")
        nalog = NalogBlagajne(
            business_unit_id=business_unit_id, vrsta=VrstaNaloga.NAPLATI, godina=dan.year,
            broj=sledeci_broj(session, business_unit_id, VrstaNaloga.NAPLATI, dan.year),
            datum=dan, iznos=round(sum(c.iznos for c in cekovi), 2), komada=len(cekovi),
            opis=f"Čekovi pazar {dan:%d.%m.}", napravio=korisnik, napravljeno_u=utcnow(),
        )
        session.add(nalog)
        session.flush()
        for c in cekovi:
            c.nalog_naplati_id = nalog.id
        session.add(AuditLog(actor=korisnik, action="blagajna.naplati",
                             detail=f"{nalog.oznaka} {nalog.opis}: {nalog.komada} kom, {nalog.iznos:,.2f}"))
        return nalog.id


def nalog_za_predaju(session, predaja, *, korisnik: str) -> NalogBlagajne:
    """Nalog blagajni da isplati - u istoj transakciji kao predaja u banku."""
    nalog = NalogBlagajne(
        business_unit_id=predaja.business_unit_id, vrsta=VrstaNaloga.ISPLATI,
        godina=predaja.datum.year,
        broj=sledeci_broj(session, predaja.business_unit_id, VrstaNaloga.ISPLATI, predaja.datum.year),
        datum=predaja.datum, iznos=predaja.iznos, komada=predaja.komada,
        opis=f"Čekovi naplata {predaja.datum:%d.%m.}", predaja_id=predaja.id,
        napravio=korisnik, napravljeno_u=utcnow(),
    )
    session.add(nalog)
    session.flush()
    return nalog


@dataclass
class Dnevnik:
    od: date
    do: date
    saldo_od: float
    stavke: list[NalogBlagajne] = field(default_factory=list)
    primitak: float = 0.0
    izdatak: float = 0.0
    pocetno: PocetnoStanjeBlagajne | None = None
    # Provera: zbir cekova koji su po aplikaciji jos u sefu.
    u_sefu_komada: int = 0
    u_sefu_iznos: float = 0.0

    @property
    def saldo_do(self) -> float:
        return round(self.saldo_od + self.primitak - self.izdatak, 2)

    @property
    def razlika(self) -> float:
        """Saldo po dnevniku minus cekovi u sefu po aplikaciji.

        Dok se stari cekovi ne uslikaju, razlika je ono sto jos fali da se
        unese; posle toga treba da bude nula - inace nesto nije zapisano.
        """
        return round(self.saldo_do - self.u_sefu_iznos, 2)


def dnevnik(session, business_unit_id: int, od: date, do: date) -> Dnevnik:
    pocetno = session.get(PocetnoStanjeBlagajne, business_unit_id)
    saldo = pocetno.saldo if pocetno is not None else 0.0
    pre = session.scalars(
        select(NalogBlagajne).where(NalogBlagajne.business_unit_id == business_unit_id,
                                    NalogBlagajne.datum < od)
    ).all()
    for n in pre:
        saldo += n.iznos if n.vrsta is VrstaNaloga.NAPLATI else -n.iznos
    stavke = session.scalars(
        select(NalogBlagajne).where(NalogBlagajne.business_unit_id == business_unit_id,
                                    NalogBlagajne.datum >= od, NalogBlagajne.datum <= do)
        .order_by(NalogBlagajne.datum, NalogBlagajne.vrsta, NalogBlagajne.broj)
    ).all()
    d = Dnevnik(od=od, do=do, saldo_od=round(saldo, 2), stavke=list(stavke), pocetno=pocetno)
    d.primitak = round(sum(n.iznos for n in stavke if n.vrsta is VrstaNaloga.NAPLATI), 2)
    d.izdatak = round(sum(n.iznos for n in stavke if n.vrsta is VrstaNaloga.ISPLATI), 2)
    n, s = session.execute(
        select(func.count(Cek.id), func.sum(Cek.iznos))
        .where(Cek.business_unit_id == business_unit_id, Cek.stanje == StanjeCeka.U_SEFU,
               # u blagajni su: cekovi sa naloga za naplatu i stari (unazad),
               # koji su deo pocetnog salda - ne i pazar koji jos nije zatvoren
               or_(Cek.nalog_naplati_id.isnot(None), Cek.primljeno.is_(None)))
    ).one()
    d.u_sefu_komada, d.u_sefu_iznos = int(n or 0), round(float(s or 0), 2)
    return d
