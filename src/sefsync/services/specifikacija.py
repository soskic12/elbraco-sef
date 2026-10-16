"""Specifikacija artikla (ERP tabela `DEKA`) pri otvaranju nove sifre.

Bane, 15.10.: „na bazi ima tabela dbo.DEKA gde se nalaze specifikacije
artikala - kod otvaranja sifre bi trebalo Duletu ponuditi iste pre otvaranja,
pa da se upise u ERP u tom prolazu“.

Sta je izmereno 15.10. pre nego sto je ovo napisano:

* `DEKA` ima tri kolone: `ARTIKAL` (PK, varchar 7), `TEKSTDEK` (RTF, do 4.095
  znakova, prosek 766) i `SELEKTOVAN` (0 kod 6.928 od 6.929 redova). Nema
  trigera. Jedan red po artiklu.
* Specifikaciju ima 74 % artikala koji su 2026. ulazili u maloprodaju; neke
  kategorije je nemaju skoro nikad (pice 0 %, kablovi 7 %, baterije 3 %).
* Tekst je u 80 % slucajeva „Kljuc: vrednost“ po redu, RTF sa Arial Narrow 8,5
  pt i kvacicama u cp1250 (`\\'e8` = č).
* **Opis stavke u e-fakturi ne vredi**: ima ga 7 % stavki, i to su polise
  osiguranja, komunalije i ponovljen naziv. Izvor mora da bude spolja.
* Artikli iste serije (naziv bez poslednje reci) imaju razlicit tekst u 822
  od 1.033 grupe - kopija od „brata" je samo polazna tacka, ne gotov tekst.

Zato dva predloga, oba samo predlog - upisuje se ono sto covek ostavi u polju:

1. **Slican artikal koji vec ima specifikaciju** - besplatno, odmah.
2. **Claude sa pretragom weba** - na dugme, jer kosta i traje desetak sekundi.
   Dobija naziv, EAN, kategoriju i tri primera iz iste kategorije, da tekst
   izgleda kao ostali u ERP-u.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings

log = logging.getLogger(__name__)

TABELA = "DEKA"
NAJVISE_ZNAKOVA = 4095      # TEKSTDEK je varchar(4095) - zajedno sa RTF omotom
PRIMERA = 3

# Isti omot koji ERP-ov editor pise (Msftedit, cp1250, Arial Narrow, 8,5 pt).
# Mereno na 93827 Gorenje WPNEI 94A1SWIFI: tako izgleda i tekst koji je covek
# kucao, pa ga ERP prikazuje isto kao svaki drugi.
_GLAVA = (
    "{\\rtf1\\ansi\\ansicpg1250\\deff0\\deflang9242"
    "{\\fonttbl{\\f0\\fnil\\fcharset238 Arial Narrow;}}\r\n"
    "{\\colortbl ;\\red8\\green0\\blue0;}\r\n"
    "\\viewkind4\\uc1\\pard\\cf1\\highlight0\\f0\\fs17 "
)
_REP = "\\par\r\n}\r\n"


# --------------------------------------------------------------------------- #
# RTF
# --------------------------------------------------------------------------- #


def rtf_u_tekst(rtf: str | None) -> str:
    """Citljiv tekst iz RTF-a koji pise ERP. Redovi ostaju redovi."""
    if not rtf:
        return ""
    if not rtf.lstrip().startswith("{\\rtf"):
        return rtf.strip()
    t = rtf
    for grupa in (r"\{\\fonttbl.*?\}\}", r"\{\\colortbl[^}]*\}", r"\{\\\*\\[^{}]*\}"):
        t = re.sub(grupa, "", t, flags=re.S)
    t = re.sub(
        r"\\'([0-9a-fA-F]{2})",
        lambda m: bytes([int(m.group(1), 16)]).decode("cp1250", errors="replace"),
        t,
    )
    t = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), t)
    # \par je novi red; \pard (pocetak pasusa) nije - \b to razlikuje.
    t = re.sub(r"\\par\b ?", "\n", t)
    t = t.replace("\\{", "\x00").replace("\\}", "\x01").replace("\\\\", "\x02")
    t = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", t)
    t = t.replace("{", "").replace("}", "")
    t = t.replace("\x00", "{").replace("\x01", "}").replace("\x02", "\\")
    redovi = [r.strip() for r in t.replace("\r", "").split("\n")]
    return "\n".join(r for r in redovi if r)


def _znak(c: str) -> str:
    if c in "\\{}":
        return "\\" + c
    if ord(c) < 128:
        return c
    try:
        b = c.encode("cp1250")
        return "".join(f"\\'{x:02x}" for x in b)
    except UnicodeEncodeError:
        # Cirilica i ostalo van cp1250 - RTF unicode, sa „?" za stare citace.
        n = ord(c)
        return f"\\u{n - 65536 if n > 32767 else n}?"


def tekst_u_rtf(tekst: str) -> str:
    """RTF u istom obliku koji pise ERP-ov editor."""
    redovi = [r.strip() for r in (tekst or "").replace("\r", "").split("\n")]
    redovi = [r for r in redovi if r]
    if not redovi:
        return ""
    telo = "\\par\r\n".join("".join(_znak(c) for c in r) for r in redovi)
    return _GLAVA + telo + _REP


def proveri_duzinu(tekst: str) -> str:
    """RTF za upis, ili ValueError ako ne staje u kolonu."""
    rtf = tekst_u_rtf(tekst)
    if len(rtf) > NAJVISE_ZNAKOVA:
        visak = len(rtf) - NAJVISE_ZNAKOVA
        raise ValueError(
            f"Specifikacija je predugačka za ERP za oko {visak} znakova "
            "(kvačice se u ERP-u pišu dužim zapisom) — skrati je."
        )
    return rtf


# --------------------------------------------------------------------------- #
# predlog iz ERP-a
# --------------------------------------------------------------------------- #


def specifikacija_artikla(conn: Connection, artikal: str) -> str:
    red = conn.execute(
        text(f"SELECT TEKSTDEK tekst FROM dbo.{TABELA} WHERE RTRIM(ARTIKAL)=:a"),
        {"a": artikal.strip()},
    ).first()
    return rtf_u_tekst(red.tekst) if red is not None else ""


def od_slicnog(conn: Connection, slicni: list[dict]) -> dict | None:
    """Prvi slican artikal (najveceg poklapanja naziva) koji ima specifikaciju."""
    for s in slicni or []:
        tekst = specifikacija_artikla(conn, s["artikal"])
        if tekst:
            return {"artikal": s["artikal"], "naziv": s["naziv"], "tekst": tekst}
    return None


def primeri_kategorije(conn: Connection, shkat: str, n: int = PRIMERA) -> list[str]:
    """Nekoliko specifikacija iz iste kategorije, kao uzor oblika.

    Biraju se tekstovi srednje duzine u obliku „Kljuc: vrednost“ - kratki ne
    pokazuju nista, a predugi bi i Claude-a gurnuli u preteranu duzinu.
    Redosled je stalan (najvece sifre prve), pa isti upit daje iste primere.
    """
    if not shkat:
        return []
    primeri: list[str] = []
    # .all(): petlja izlazi rano, a nepročitani redovi bi zauzeli vezu (bez MARS-a).
    for r in conn.execute(
        text(
            f"SELECT TOP 30 d.TEKSTDEK tekst FROM dbo.{TABELA} d "
            "JOIN dbo.ARTIKLI a ON RTRIM(a.ARTIKAL)=RTRIM(d.ARTIKAL) AND RTRIM(a.SIFRAMAG)='099' "
            "WHERE RTRIM(a.SHKAT)=:k AND LEN(d.TEKSTDEK) BETWEEN 300 AND 1800 "
            "ORDER BY TRY_CAST(RTRIM(a.ARTIKAL) AS int) DESC"
        ),
        {"k": shkat.strip()},
    ).all():
        tekst = rtf_u_tekst(r.tekst)
        if sum(1 for x in tekst.splitlines() if ":" in x) >= 3:
            primeri.append(tekst)
        if len(primeri) >= n:
            break
    return primeri


# --------------------------------------------------------------------------- #
# predlog od Claude-a
# --------------------------------------------------------------------------- #


@dataclass
class PredlogSpecifikacije:
    tekst: str = ""
    izvori: list[dict] = field(default_factory=list)   # {"naslov", "url"}
    poruka: str = ""


SISTEM = """\
Pišeš specifikacije artikala za šifarnik maloprodajnog lanca bele tehnike, \
elektronike i opreme u Srbiji. Tekst ide u ERP i štampa se na deklaracijama, \
pa mora biti tačan.

Pravila:
- Pronađi tačan model na webu (po nazivu i EAN-u) i piši samo ono što izvori \
potvrđuju za TAJ model. Ako podatak ne nađeš ili nisi siguran da je za taj \
model, izostavi red — ne nagađaj i ne prepisuj sa sličnog modela.
- Oblik: jedan podatak po redu, „Ključ: vrednost“, srpski latinicom, kao u \
primerima. Bez naslova, nabrajanja, marketinških rečenica, cene i garancije.
- Najviše oko 20 redova; najvažnije prvo (tip, snaga/kapacitet, energetski \
razred, dimenzije, boja).
- Ceo odgovor stavi između <specifikacija> i </specifikacija>. Ako model ne \
možeš pouzdano da pronađeš, vrati prazno <specifikacija></specifikacija> i \
iza toga jednu rečenicu zašto."""


def _upit(naziv: str, ean: str, kategorija: str, dobavljac: str, primeri: list[str]) -> str:
    delovi = [
        f"Naziv sa fakture: {naziv}",
        f"EAN: {ean or 'nema'}",
        f"Kategorija u našem šifarniku: {kategorija or 'nepoznata'}",
        f"Dobavljač: {dobavljac or 'nepoznat'}",
    ]
    if primeri:
        delovi.append("\nOvako izgledaju specifikacije iz iste kategorije u našem ERP-u:")
        for i, p in enumerate(primeri, 1):
            delovi.append(f"<primer{i}>\n{p}\n</primer{i}>")
    return "\n".join(delovi)


def _klijent():
    import anthropic

    podesavanja = get_settings()
    kljuc = podesavanja.anthropic_api_key
    if not kljuc:
        raise ValueError("Claude nije podešen — u .env fali ANTHROPIC_API_KEY.")
    zaglavlja = {}
    if podesavanja.anthropic_workspace_id:
        zaglavlja["anthropic-workspace-id"] = podesavanja.anthropic_workspace_id.strip()
    return anthropic.Anthropic(
        api_key=kljuc, timeout=120.0, max_retries=2, default_headers=zaglavlja or None
    )


def predlozi_claude(
    naziv: str,
    *,
    ean: str = "",
    kategorija: str = "",
    dobavljac: str = "",
    primeri: list[str] | None = None,
    klijent=None,
) -> PredlogSpecifikacije:
    """Specifikacija sa weba, u obliku nasih tekstova. Covek je pregleda pre upisa."""
    import anthropic

    podesavanja = get_settings()
    klijent = klijent or _klijent()
    poruke: list[dict] = [
        {"role": "user", "content": _upit(naziv, ean, kategorija, dobavljac, primeri or [])}
    ]
    alati = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]

    odgovor = None
    try:
        # Pretraga ume da stane posle deset krugova (pause_turn) - tada se isti
        # razgovor salje ponovo i server nastavlja sam.
        for _ in range(3):
            odgovor = klijent.beta.messages.create(
                model=podesavanja.spec_model,
                max_tokens=16000,
                system=SISTEM,
                messages=poruke,
                tools=alati,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if odgovor.stop_reason != "pause_turn":
                break
            poruke = [poruke[0], {"role": "assistant", "content": odgovor.content}]
    except anthropic.AuthenticationError:
        return PredlogSpecifikacije(poruka="Claude ključ nije ispravan (ANTHROPIC_API_KEY).")
    except anthropic.RateLimitError:
        return PredlogSpecifikacije(poruka="Claude je trenutno zauzet — probaj za minut.")
    except anthropic.APIStatusError as exc:
        log.warning("Claude za specifikaciju: %s %s", exc.status_code, exc.message)
        if "workspace" in (exc.message or ""):
            return PredlogSpecifikacije(
                poruka="Claude ključ nije vezan za radni prostor — u .env treba "
                       "ANTHROPIC_WORKSPACE_ID, ili ključ napravljen u radnom prostoru."
            )
        return PredlogSpecifikacije(poruka=f"Claude nije odgovorio ({exc.status_code}).")
    except anthropic.APIConnectionError:
        return PredlogSpecifikacije(poruka="Nema veze sa Claude-om — server ne vidi internet?")

    if odgovor is None or odgovor.stop_reason == "refusal":
        return PredlogSpecifikacije(poruka="Claude je odbio zahtev — upiši specifikaciju ručno.")

    izvori: list[dict] = []
    videni: set[str] = set()
    tekst = ""
    for blok in odgovor.content:
        if blok.type == "text":
            tekst += blok.text
        elif blok.type == "web_search_tool_result" and isinstance(blok.content, list):
            for r in blok.content:
                url = getattr(r, "url", None)
                if url and url not in videni:
                    videni.add(url)
                    izvori.append({"naslov": getattr(r, "title", "") or url, "url": url})

    m = re.search(r"<specifikacija>(.*?)</specifikacija>(.*)", tekst, flags=re.S)
    if m is None:
        return PredlogSpecifikacije(
            izvori=izvori[:5], poruka="Claude nije vratio specifikaciju u očekivanom obliku."
        )
    spec = "\n".join(r.strip(" -•\t") for r in m.group(1).strip().splitlines() if r.strip())
    if not spec:
        zasto = m.group(2).strip()[:300]
        return PredlogSpecifikacije(
            izvori=izvori[:5], poruka=f"Model nije pouzdano pronađen. {zasto}".strip()
        )
    return PredlogSpecifikacije(tekst=spec, izvori=izvori[:5])
