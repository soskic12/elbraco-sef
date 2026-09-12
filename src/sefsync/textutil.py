"""Normalizacija teksta - cirilica/latinica, dijakritika, razmaci.

Dobavljaci pisu adrese i nazive objekata kako stignu: "Војводе Мишића 12",
"VOJVODE MISICA 12", "Vojvode Mišića br.12". Pravila razvrstavanja moraju
da rade nad normalizovanim oblikom.
"""

from __future__ import annotations

import re
import unicodedata

# Srpska cirilica -> latinica (digrafi prvo)
_CYR_DIGRAPHS = {
    "Љ": "LJ", "љ": "lj",   # Љ љ
    "Њ": "NJ", "њ": "nj",   # Њ њ
    "Џ": "DZ", "џ": "dz",   # Џ џ
}
_CYR_LETTERS = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D",
    "Ђ": "Dj", "Е": "E", "Ж": "Z", "З": "Z", "И": "I",
    "Ј": "J", "К": "K", "Л": "L", "М": "M", "Н": "N",
    "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T",
    "Ћ": "C", "У": "U", "Ф": "F", "Х": "H", "Ц": "C",
    "Ч": "C", "Ш": "S",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "ђ": "dj", "е": "e", "ж": "z", "з": "z", "и": "i",
    "ј": "j", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "ћ": "c", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "c", "ш": "s",
}

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def cyr_to_lat(text: str) -> str:
    for src, dst in _CYR_DIGRAPHS.items():
        text = text.replace(src, dst)
    return "".join(_CYR_LETTERS.get(ch, ch) for ch in text)


def strip_diacritics(text: str) -> str:
    """č/ć -> c, š -> s, ž -> z, đ -> dj."""
    text = text.replace("đ", "dj").replace("Đ", "Dj")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str | None, drop_punct: bool = True) -> str:
    """Kanonski oblik za poredjenje: latinica, bez dijakritike, VELIKA SLOVA."""
    if not text:
        return ""
    out = cyr_to_lat(str(text))
    out = strip_diacritics(out)
    out = out.upper()
    if drop_punct:
        out = _PUNCT_RE.sub(" ", out)
    return _WS_RE.sub(" ", out).strip()


def digits(text: str | None) -> str:
    return "".join(ch for ch in (text or "") if ch.isdigit())


def shorten(text: str | None, limit: int) -> str | None:
    """Skracuje string na duzinu kolone u bazi (bez rusenja upisa)."""
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
