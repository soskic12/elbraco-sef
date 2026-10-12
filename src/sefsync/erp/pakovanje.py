"""Koliko komada, litara ili grama stoji iza jedne stavke fakture.

Pub (071) robu kupuje u pakovanju, a **troši i vodi u jedinici posluzivanja**:
bure od 20 l ulazi kao 20 litara, kutija caja kao 40 kesica, kilogram kafe kao
1000 grama. Poslovodja to preracunava rukom, a aplikacija je upisivala kolicinu
sa fakture - pa je `HAUSBRANDT HERMAN 1kg` 6 KGM zavrsio kao **6 grama** umesto
6.000 (kalkulacija 00194, 09.10.).

Mereno na **159 stavki** iz 18 faktura (jun-oktobar 2026), gde se zna i sta je na
fakturi i sta je covek upisao:

| faktor | stavki | sta je |
|---|---|---|
| x1 | 125 (79 %) | sokovi, voda, flasirano pivo, cigarete, med |
| x20 | 24 | burad i kutije od 20 kesica |
| x40 | 5 | topla cokolada `1/40`, caj `40X2,5` |
| x9 | 5 | `KAFA AMIGO 9G` |

Pravila ispod pogadjaju **158 od 159**; jedino neslaganje je bas ona pogresna
kalkulacija, dakle protiv ispravnih podataka su 159 od 159.

**Zamka:** `24/1`, `50/1`, `200/1` (`ROSA 0,33 24/1`, `MED 15G 200/1`) su
transportno pakovanje i ne znace nista - sve te stavke idu x1. Obrnuto, `1/40`
znaci kutiju od 40. Isto i zapremina u nazivu: `COCA COLA 0,25L` nije pakovanje.
Zato se broj litara trazi samo uz rec `KEG`, `BURE` ili `MODULAR`.

**Kutija i kesica se citaju samo u pubu.** Mereno 12.10. na celom sifarniku
(763.949 artikala): ta dva pravila daju predlog za 6.763 artikla, i to gotovo
sve dimenzije i oznake modela - `Klupa sklopiva 18x30x43` kao kutija od 18,
`Bosch KGN 34X04` od 34, `Unior 481 1/2 inca` od 2. Tesla RS0880HE je na
fakturi nosio dimenzije u nazivu i stajao Duletu na ekranu. Kilogram, bure i
grami vaze svuda, jer ih cuva jedinica artikla (litar, gram) - a burad ima i
021 (Paulaner i Kozel 30 l).

Ovaj modul **samo predlaze**. Mnozi se iskljucivo faktorom koji stoji uz vezu -
onim koji je covek potvrdio ili koji je izveden iz istorije. Pogresan faktor
tiho upise pogresnu zalihu, a to se vidi tek kad proizvodnja ode u minus.
"""

from __future__ import annotations

import re

# Jedinice u kojima ERP vodi tezinu, zapreminu i komad.
GRAM = {"G", "GR"}
LITAR = {"L", "LIT", "LT"}
KOMAD = {"KOM", "KOS", "PAK"}

# Oznake jedinica sa fakture (UBL): KGM = kilogram, LTR = litar, H87 = komad.
_KILOGRAM = {"KGM", "KG"}
_LITAR_FAKTURE = {"LTR", "L"}

_BURE = re.compile(r"(?:KEG|BURE)\s*(\d+)\s*L", re.I)
_MODULAR = re.compile(r"(\d+)\s*L(?:IT)?\b[^/]*MODULAR", re.I)
_KUTIJA = re.compile(r"\b1\s*/\s*(\d+)\b")
_KESICA = re.compile(r"\b(\d+)\s*X\s*\d|\b(\d+)\s*KESICA", re.I)
_GRAMA = re.compile(r"\b(\d+)\s*G\b", re.I)

# Objekti u kojima se pakovanje cita iz naziva - vidi `docs/PUB-071.md`.
MAGACINI_PAKOVANJA = {"071"}


def predlozi_faktor(
    naziv: str,
    jm_fakture: str | None,
    jm_artikla: str | None,
    *,
    magacin: str | None,
) -> tuple[float, str]:
    """Vraca (faktor, objasnjenje). Faktor 1.0 znaci „nema sta da se preracuna".

    `jm_fakture` je `unitCode` sa stavke UBL-a, `jm_artikla` je `ARTIKLI.JEDMERE`.
    `magacin` je objekat kalkulacije: naziv se cita samo u pubu.
    """
    n = (naziv or "").replace(",", ".")
    fakture = (jm_fakture or "").strip().upper()
    artikla = (jm_artikla or "").strip().upper()

    # 1. Kilogram na fakturi, a artikal se vodi u gramima - cista konverzija.
    if fakture in _KILOGRAM and artikla in GRAM:
        return 1000.0, "kilogram sa fakture, artikal se vodi u gramima"
    if fakture in _LITAR_FAKTURE and artikla in LITAR:
        return 1.0, ""

    # 2. Bure: broj litara, ali samo uz rec koja kaze da je bure. Vazi svuda -
    # artikal mora da se vodi u litrima, a burad ima i 021 (Paulaner 30L).
    if artikla in LITAR:
        m = _BURE.search(n) or _MODULAR.search(n)
        if m:
            return float(m.group(1)), f"bure od {m.group(1)} l"

    # Kutija i kesica se citaju iz samog naziva, bez obzira na jedinicu, pa van
    # puba hvataju dimenzije i oznake modela.
    if (magacin or "").strip() in MAGACINI_PAKOVANJA:
        # 3. Kutija zapisana kao „1/40".
        m = _KUTIJA.search(n)
        if m:
            return float(m.group(1)), f"kutija od {m.group(1)}"

        # 4. Kutija kesica: „40X2,5“, „20X1,75“, „40 KESICA“.
        m = _KESICA.search(n)
        if m:
            broj = m.group(1) or m.group(2)
            return float(broj), f"kutija od {broj} kesica"

    # 5. Kesica od N grama, a artikal se vodi u gramima. Vazi svuda - cuva ga
    # jedinica artikla.
    if artikla in GRAM:
        m = _GRAMA.search(n)
        if m:
            return float(m.group(1)), f"{m.group(1)} g po komadu"

    return 1.0, ""
