"""Prijava istim nalogom kojim ljudi ulaze u ERP.

Preslikano iz projekta ELBRACO LOGISTIKA (`SqlLoginAuthenticator`, `AppAccess`),
namerno istim pravilima - da čovek ima jedan nalog, a ne po jedan po aplikaciji:

  - **Lozinka se ne čuva.** Proverava je SQL server tako što se njome otvori
    veza; posle toga vrednost prestaje da postoji. Ne ide ni u dnevnik ni u
    kolačić.
  - **Ko je ko** čita se iz `dbo.kontakti_osobe` po koloni `[User]`. Popunjena
    kolona JE davanje pristupa - nalog se dodaje i gasi tamo gde se ionako vodi
    ko gde radi.
  - **Uloga sledi iz podatka i imenovanog spiska**: osoba vezana za objekat
    (`SifraPovezanogObjekta`) vidi samo taj objekat. Operater je onaj ko je
    upisan u `WEB_OPERATORS` - prihvatanje fakture je pravni čin prema
    dobavljaču, pa se to pravo ne dobija samim postojanjem naloga.

Kad ERP baza nije dostupna, radi rezervna prijava iz `WEB_USERS` - da se
aplikacija može otvoriti i kad je server dole.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote_plus

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError

from ..config import Settings, get_settings
from ..erp.connection import ErpNotConfigured, get_erp_engine, resolve_erp_url

log = logging.getLogger(__name__)

# Ne stiti od pogadjanja nego od ZAKLJUCAVANJA naloga: ako na serveru stoji
# politika zakljucavanja, nekoliko promasaja ovde zakljucalo bi coveku i ERP.
MAX_ATTEMPTS = 4
ATTEMPT_WINDOW_S = 300
_failures: dict[str, list[float]] = {}


class Role(str, Enum):
    OPERATER = "operater"
    POSLOVODJA = "poslovodja"


@dataclass(frozen=True)
class User:
    login: str
    name: str
    role: Role
    unit_code: str | None = None   # sifra PJ u ERP-u, za poslovodju
    function: str = ""

    @property
    def is_operator(self) -> bool:
        return self.role is Role.OPERATER

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.login})"


class AuthError(Exception):
    """Prijava nije prosla; poruka je namenjena coveku na ekranu."""


def _blocked(key: str) -> bool:
    now = time.monotonic()
    attempts = [t for t in _failures.get(key, []) if now - t < ATTEMPT_WINDOW_S]
    _failures[key] = attempts
    return len(attempts) >= MAX_ATTEMPTS


def _record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.monotonic())


def _clear_failures(key: str) -> None:
    _failures.pop(key, None)


def _connection_for(login: str, password: str, base_url: str) -> str:
    """Ista veza, samo sa drugim nalogom."""
    without_auth = re.sub(r"://[^@/]*@", "://", base_url, count=1)
    scheme, _, rest = without_auth.partition("://")
    return f"{scheme}://{quote_plus(login)}:{quote_plus(password)}@{rest}"


def verify_sql_login(login: str, password: str, settings: Settings | None = None) -> None:
    """Baca AuthError ako veza tim nalogom ne prolazi."""
    s = settings or get_settings()
    base = resolve_erp_url()
    if not base:
        raise AuthError("Veza ka ERP bazi nije podešena, pa prijava nije moguća.")

    key = login.strip().lower()
    if _blocked(key):
        raise AuthError("Previše pokušaja. Sačekaj pet minuta — da se nalog ne zaključa u ERP-u.")

    engine = create_engine(
        _connection_for(login.strip(), password, base),
        connect_args={"timeout": 5},
        pool_pre_ping=False,
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - drajver baca razne tipove
        poruka = str(exc)
        _record_failure(key)
        if "Login failed" in poruka or "18456" in poruka:
            raise AuthError("Pogrešno korisničko ime ili lozinka.") from exc
        log.warning("Prijava za %s nije uspela: %s", key, poruka[:200])
        raise AuthError("Server nije dostupan, pokušaj ponovo.") from exc
    finally:
        engine.dispose()
    _clear_failures(key)


def load_person(login: str, settings: Settings | None = None) -> User:
    """Identitet iz sifarnika osoblja, uloga iz imenovanog spiska i objekta."""
    s = settings or get_settings()
    try:
        engine = get_erp_engine()
    except ErpNotConfigured as exc:
        raise AuthError(str(exc)) from exc
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT TOP 1 RTRIM(ImePrezime) AS ime, RTRIM(Funkcija) AS funkcija, "
                    "RTRIM(ISNULL(SifraPovezanogObjekta, '')) AS objekat, Aktivan AS aktivan "
                    "FROM dbo.kontakti_osobe WHERE LOWER(RTRIM([User])) = :prijava"
                ),
                {"prijava": login.strip().lower()},
            )
            .mappings()
            .first()
        )

    if row is None:
        raise AuthError(
            "Nalog postoji na serveru, ali nije upisan u šifarnik osoblja "
            "(kolona User u KONTAKTI_OSOBE). Traži da te upišu."
        )
    if not row["aktivan"]:
        raise AuthError("Nalog nije aktivan u šifarniku osoblja.")

    funkcija = (row["funkcija"] or "").upper()
    objekat = row["objekat"] or ""
    operateri = s.operator_logins()

    if login.strip().lower() in operateri:
        return User(login.strip(), row["ime"], Role.OPERATER, objekat or None, funkcija)
    if objekat:
        return User(login.strip(), row["ime"], Role.POSLOVODJA, objekat, funkcija)

    # Covek moze biti vezan za jedinicu i kod nas, spiskom prijava uz poslovnu
    # jedinicu. `SifraPovezanogObjekta` deli i projekat LOGISTIKA, pa se u njega
    # ne upisuje nasa sema - a neko poput Ilijane (reklamacije, magacin 010)
    # tamo nema objekat uopste.
    jedinica = _jedinica_po_prijavi(login)
    if jedinica is not None:
        return User(login.strip(), row["ime"], Role.POSLOVODJA, jedinica, funkcija)
    if "POSLOVODJA" in funkcija or "POSLOVOĐA" in funkcija:
        raise AuthError(
            "Poslovođa nema upisan objekat u šifarniku (SifraPovezanogObjekta), "
            "pa se ne zna čije dokumente bi video."
        )
    if operateri:
        # Prijava se ispisuje namerno: spisak operatera se pise rukom, pa je
        # neslaganje sa stvarnim nalogom najcesci uzrok. Bez nje covek javi ime,
        # a u spisku stoji nesto trece ("SelakDusan" naspram "DusanSelak").
        raise AuthError(
            f"Nalog „{login.strip()}“ radi, ali nema pristup ovoj aplikaciji. "
            f"Za rad u panelu treba da te upišu u spisak operatera — pod tačno "
            f"tim imenom."
        )
    return User(login.strip(), row["ime"], Role.OPERATER, None, funkcija)


def _jedinica_po_prijavi(login: str) -> str | None:
    """Sifra jedinice kojoj je prijava dodeljena u panelu, ako jeste."""
    from ..db import session_scope
    from ..models import BusinessUnit

    prijava = (login or "").strip().lower()
    if not prijava:
        return None
    try:
        with session_scope() as session:
            for jedinica in session.scalars(
                select(BusinessUnit).where(BusinessUnit.logins.is_not(None))
            ):
                if prijava in jedinica.login_list():
                    return (jedinica.erp_code or jedinica.code or "").strip() or None
    except (SQLAlchemyError, OSError) as exc:
        # Nedostupna baza ne sme da obori prijavu, ali greska u kodu sme - zato
        # se ne hvata sve: prvi pokusaj je progutao NameError i tiho vracao
        # "nema jedinice", pa je izgledalo kao da spisak prijava ne radi.
        log.warning("Spisak prijava po jedinicama nije procitan: %s", exc)
    return None


def authenticate(login: str, password: str, settings: Settings | None = None) -> User:
    """Provera lozinke pa ucitavanje uloge. Rezervna prijava iz WEB_USERS."""
    s = settings or get_settings()
    rezervni = s.web_credentials()
    if login in rezervni and secrets.compare_digest(password, rezervni[login]):
        log.info("Prijava rezervnim nalogom %s (bez ERP-a)", login)
        return User(login, login, Role.OPERATER, None, "REZERVNI NALOG")

    verify_sql_login(login, password, s)
    return load_person(login, s)
