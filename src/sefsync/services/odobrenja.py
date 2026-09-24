r"""Knjizno odobrenje koje ne navodi artikal nego broj fakture koju ispravlja.

Eurotehna (88 od 587 kalkulacija na 010) u stavku upise "Odobrenje po Saglasnosti
broj 278031/2026", sto nije artikal i nikad nece imati nasu sifru. Ali u napomeni
stoji: "Odobreno za ves masinu WPNA14A3PWIFI, po racunu broj 851 VP-2026 od
16.03.2026."

Dva puta do artikla, mereno 24.09.:

  model iz napomene   WPNA14A3PWIFI  ->  ne nalazi se, jer je kod nas upisan
                                         kao "Gorenje WPNA 14 A3PWIFI"
  broj fakture        851 VP-2026    ->  MKALKUL 008/00153, artikal 84678,
                                         nabavna 43.470,70 = osnovica odobrenja

Zato se ide preko broja fakture. Model ostaje kao rezervni put, uz brisanje
razmaka na obe strane.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

# "po racunu broj 851 VP-2026 od 16.03.2026." - broj ume da ima razmak u sebi,
# pa se hvata sve do " od " ili do zareza/tacke.
_BROJ_RACUNA = re.compile(
    r"po\s+(?:ra[cč]unu|fakturi|otpremnici)\s*(?:broj|br\.?)?\s*"
    r"([0-9][0-9A-Za-z\-/ ]{2,24}?)"
    r"(?=\s+od\b|\s*[,.;]|$)",
    re.IGNORECASE,
)


def broj_originalnog_racuna(napomena: str | None) -> str | None:
    """Vraca broj fakture koju odobrenje ispravlja, bez razmaka."""
    if not napomena:
        return None
    nadjeno = _BROJ_RACUNA.search(" ".join(napomena.split()))
    if not nadjeno:
        return None
    broj = nadjeno.group(1).replace(" ", "").strip(" -/.,")
    return broj or None


def artikli_sa_racuna(conn: Connection, dobavljac: str, broj_racuna: str) -> list[dict]:
    """Stavke ranije kalkulacije za taj broj racuna, iz veleprodaje i maloprodaje.

    Broj se poredi bez razmaka, jer ga dobavljac u napomeni pise razdvojeno
    ("851 VP-2026"), a u ERP-u stoji spojeno ("851VP-2026").
    """
    upiti = (
        ("ULKALKUL", "PRULKALK"),
        ("MKALKUL", "MPRULK"),
    )
    nadjeno: list[dict] = []
    for zaglavlje, stavke in upiti:
        red = conn.execute(
            text(
                f"select RTRIM(z.MAGACIN) magacin, RTRIM(z.BROJ) broj, "
                f"RTRIM(s.ARTIKAL) artikal, RTRIM(s.NAZIV) naziv, "
                f"s.KOLICINA kolicina, s.NAB_CENA nabavna "
                f"from dbo.{zaglavlje} z join dbo.{stavke} s "
                f"  on RTRIM(z.MAGACIN)=RTRIM(s.MAGACIN) and RTRIM(z.BROJ)=RTRIM(s.BROJ) "
                f" and RTRIM(z.VRSTAKNJIZ)=RTRIM(s.VRSTAKNJIZ) "
                f"where RTRIM(z.DOBAVLJAC)=:d "
                f"  and REPLACE(RTRIM(z.BROJ_RN_DOB),' ','')=:b and s.KOLICINA > 0"
            ),
            {"d": dobavljac, "b": broj_racuna},
        )
        nadjeno.extend(dict(r._mapping) for r in red)
    return nadjeno


def artikal_po_modelu(conn: Connection, napomena: str | None) -> list[dict]:
    """Rezervni put: model iz napomene naspram naziva artikla, bez razmaka.

    "WPNA14A3PWIFI" se ne poklapa sa "Gorenje WPNA 14 A3PWIFI" dok se razmaci ne
    uklone sa obe strane. Trazi se samo niz od bar sest znakova koji ima i slovo i
    cifru - inace bi "2026" iz datuma vukao pola sifarnika.
    """
    if not napomena:
        return []
    kandidati = [
        rec.strip(" ,.;")
        for rec in re.split(r"\s+", " ".join(napomena.split()))
        if len(rec.strip(" ,.;")) >= 6
        and any(z.isdigit() for z in rec)
        and any(z.isalpha() for z in rec)
    ]
    nadjeno: list[dict] = []
    for model in kandidati:
        red = conn.execute(
            text(
                "select RTRIM(ARTIKAL) artikal, RTRIM(NAZIV) naziv from dbo.ARTIKLI "
                "where RTRIM(SIFRAMAG)='099' "
                "  and REPLACE(REPLACE(NAZIV,' ',''),'-','') like :m"
            ),
            {"m": "%" + model.replace(" ", "").replace("-", "") + "%"},
        )
        nadjeno.extend(dict(r._mapping) | {"model": model} for r in red)
    return nadjeno
