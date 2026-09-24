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
    # "po racunu", "po racun", "po fakturi", "po otpremnici" - dobavljaci ne paze
    # na padez, a 587FN-2026 glasi bas "po racun broj 2082 VP-2026".
    r"po\s+(?:ra[cč]un\w*|fakt\w*|otprem\w*)\s*[:\-]?\s*(?:broj|br\.?)?\s*[:\-]?\s*"
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


_BAZE: list[str] | None = None


def baze_po_godinama(conn: Connection) -> list[str]:
    """Baze ERP-a po godinama, od najnovije, samo one koje nas nalog sme da cita.

    ERP drzi godinu po bazi (ELBS_2026, ELBS_2025, ...). Knjizno odobrenje po
    reklamaciji stize za robu koja je kupljena ranije - Bane, 24.09.: "ovakva
    k.o. cisto stizu za robu kojoj je kalkulacija ulazna u prethodnim godinama".
    Pretraga samo tekuce godine bi ih zato promasila.

    Mereno 24.09.: nalog vidi ELBS_2026 i ELBS_2025; na starije nema pravo, pa se
    tiho preskacu umesto da svaki upit puca.
    """
    global _BAZE
    if _BAZE is None:
        kandidati = [
            r[0]
            for r in conn.execute(
                text(
                    "select name from sys.databases where name like 'ELBS[_]20%' "
                    "and state = 0 order by name desc"
                )
            )
        ]
        dostupne = []
        for baza in kandidati:
            try:
                conn.execute(text(f"select top 1 1 from [{baza}].dbo.ULKALKUL"))
                dostupne.append(baza)
            except Exception:  # noqa: BLE001 - nema prava ili baza nije podignuta
                continue
        _BAZE = dostupne
    return _BAZE


def artikli_sa_racuna(conn: Connection, dobavljac: str, broj_racuna: str) -> list[dict]:
    """Stavke ranije kalkulacije za taj broj racuna, kroz sve dostupne godine.

    Broj se poredi bez razmaka, jer ga dobavljac u napomeni pise razdvojeno
    ("851 VP-2026"), a u ERP-u stoji spojeno ("851VP-2026").
    """
    upiti = (
        ("ULKALKUL", "PRULKALK"),
        ("MKALKUL", "MPRULK"),
    )
    nadjeno: list[dict] = []
    for baza in baze_po_godinama(conn):
        for zaglavlje, stavke in upiti:
            red = conn.execute(
                text(
                    f"select RTRIM(z.MAGACIN) magacin, RTRIM(z.BROJ) broj, "
                    f"RTRIM(s.ARTIKAL) artikal, RTRIM(s.NAZIV) naziv, "
                    f"s.KOLICINA kolicina, s.NAB_CENA nabavna "
                    f"from [{baza}].dbo.{zaglavlje} z join [{baza}].dbo.{stavke} s "
                    f"  on RTRIM(z.MAGACIN)=RTRIM(s.MAGACIN) and RTRIM(z.BROJ)=RTRIM(s.BROJ) "
                    f" and RTRIM(z.VRSTAKNJIZ)=RTRIM(s.VRSTAKNJIZ) "
                    f"where RTRIM(z.DOBAVLJAC)=:d "
                    f"  and REPLACE(RTRIM(z.BROJ_RN_DOB),' ','')=:b and s.KOLICINA > 0"
                ),
                {"d": dobavljac, "b": broj_racuna},
            )
            nadjeno.extend(dict(r._mapping) | {"baza": baza} for r in red)
    return nadjeno


def sifra_postoji_danas(conn: Connection, artikal: str) -> bool:
    """Da li sifra iz stare godine i dalje postoji u tekucem sifarniku.

    Bez ove provere bi predlog iz 2025. mogao da vodi na sifru koje vise nema,
    pa bi kalkulacija pukla tek pri upisu.
    """
    return bool(
        conn.execute(
            text(
                "select top 1 1 from dbo.ARTIKLI "
                "where RTRIM(ARTIKAL)=:a and RTRIM(SIFRAMAG)='099'"
            ),
            {"a": artikal},
        ).first()
    )


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


def izaberi_stavku(nadjeno: list[dict], osnovica: float | None) -> tuple[dict | None, str]:
    """Bira stavku sa originalne fakture. Vraca (stavka, kako je izabrana).

    Faktura ume da ima vise redova, a odobrenje se odnosi na jedan. Iznos je taj
    koji razdvaja: kod svih razresenih odobrenja se nabavna cena poklopila sa
    osnovicom odobrenja u dinar (615FN-2026: 43.470,70).
    """
    if not nadjeno:
        return None, "nema"
    if len(nadjeno) == 1:
        return nadjeno[0], "jedina stavka"
    if osnovica is None:
        return None, "vise stavki, odobrenje bez osnovice"

    po_iznosu = [x for x in nadjeno if abs(float(x["nabavna"]) - float(osnovica)) < 1.0]
    if len(po_iznosu) == 1:
        return po_iznosu[0], "po iznosu"
    if len(po_iznosu) > 1:
        # Isti artikal u vise redova je i dalje jedan odgovor.
        sifre = {x["artikal"] for x in po_iznosu}
        if len(sifre) == 1:
            return po_iznosu[0], "po iznosu (isti artikal u vise redova)"
        return None, "vise stavki sa istim iznosom"
    return None, "nijedna stavka ne odgovara iznosu"


def predlog_iz_napomene(
    conn: Connection,
    *,
    napomena: str | None,
    dobavljac: str | None,
    osnovica: float | None,
) -> dict | None:
    """Predlog artikla za odobrenje koje ga ne navodi. None kad nema pouzdanog.

    Mereno na 967 odobrenja: razresava 53 slucaja, a predlog se poklapa sa onim
    sto je covek uneo u 50 od 52 provere. Zato ide na potvrdu, ne pravo u
    kalkulaciju - jedan od dva promasaja je bio stvarno drugi uredjaj.
    """
    if not dobavljac:
        return None
    broj = broj_originalnog_racuna(napomena)
    if not broj:
        return None
    stavka, kako = izaberi_stavku(artikli_sa_racuna(conn, dobavljac, broj), osnovica)
    if stavka is None:
        return None
    if not sifra_postoji_danas(conn, stavka["artikal"]):
        return None
    return {
        "sifra": stavka["artikal"],
        "naziv": stavka["naziv"],
        "racun": broj,
        "baza": stavka["baza"],
        "magacin": stavka["magacin"],
        "nabavna": float(stavka["nabavna"]),
        "kako": kako,
    }
