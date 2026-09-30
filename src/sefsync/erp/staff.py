"""Podaci o osoblju iz ERP sifarnika (`dbo.kontakti_osobe`), samo citanje.

Koristi se da obavestenje o potvrdi prijema stigne bas onom operateru koji je
dokument prosledio - bez drugog spiska mejlova koji bi zastarevao.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from .connection import get_erp_engine

log = logging.getLogger(__name__)

UPIT = (
    "SELECT TOP 1 RTRIM(ImePrezime) AS ime, RTRIM(ISNULL(Email, '')) AS email "
    "FROM dbo.kontakti_osobe WHERE LOWER(RTRIM([User])) = :prijava"
)


def find_by_login(login: str) -> dict[str, str] | None:
    """Ime i mejl osobe po prijavi; None kad je nema ili baza nije dostupna."""
    if not login:
        return None
    try:
        with get_erp_engine().connect() as conn:
            row = conn.execute(text(UPIT), {"prijava": login.strip().lower()}).mappings().first()
    except Exception as exc:  # noqa: BLE001 - nedostupna baza ne sme da obori obavestenje
        log.warning("Ne mogu da pročitam osobu %s iz šifarnika: %s", login, str(exc)[:160])
        return None

    if row is None:
        return None
    email = row["email"]
    return {
        "ime": row["ime"],
        "email": "" if email.lower() in ("", "nepoznato") else email,
    }


def email_for_login(login: str) -> str:
    osoba = find_by_login(login)
    return osoba["email"] if osoba else ""


UPIT_PO_OBJEKTU = (
    "SELECT RTRIM(ISNULL(Email, '')) AS email FROM dbo.kontakti_osobe "
    "WHERE RTRIM(ISNULL(SifraPovezanogObjekta, '')) = :objekat "
    "AND RTRIM(ISNULL(Email, '')) <> ''"
)


def emails_for_unit(sifra_objekta: str) -> list[str]:
    """Mejlovi ljudi vezanih za taj objekat u sifarniku osoblja.

    Adresa ume da stoji samo ovde, a ne i na poslovnoj jedinici u panelu:
    magacin povrata je tako docekao prvi dokument i prosledjivanje je palo,
    iako je Ilijanin `reklamacije@elbraco.rs` sve vreme bio upisan uz objekat
    010 (30.09.).

    Sifarnik se samo cita - deli ga i projekat LOGISTIKA, pa se u njega ne
    upisuje nasa sema.
    """
    sifra = (sifra_objekta or "").strip()
    if not sifra:
        return []
    try:
        with get_erp_engine().connect() as conn:
            redovi = conn.execute(text(UPIT_PO_OBJEKTU), {"objekat": sifra}).mappings().all()
    except Exception as exc:  # noqa: BLE001 - nedostupna baza ne sme da obori obavestenje
        log.warning("Ne mogu da pročitam osoblje objekta %s: %s", sifra, str(exc)[:160])
        return []

    nadjeni: list[str] = []
    for red in redovi:
        adresa = (red["email"] or "").strip()
        if adresa and adresa.lower() != "nepoznato" and adresa not in nadjeni:
            nadjeni.append(adresa)
    return nadjeni
