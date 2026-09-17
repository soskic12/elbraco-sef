"""Slike koje poslovodja salje uz dokument (ambalaza novog artikla).

Poslovodja slika telefonom, na licu mesta, pa je bitno samo dvoje: da fajl
zavrsi vezan za pravi dokument i da se ne moze podvaliti nesto sto nije slika.

Fajl ide na disk kao i UBL i PDF prilozi; u bazi stoji putanja.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings
from ..db import session_scope
from ..models import AuditLog, Document, Slika, utcnow

log = logging.getLogger(__name__)

# Telefon salje JPEG ili HEIC, poneko i PNG. Sve ostalo se odbija - ovo je ulaz
# koji koristi neko van kancelarije, pa se ne oslanjamo na to "sta je poslato".
DOZVOLJENE = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heic",
    "image/webp": ".webp",
}
NAJVECA = 12 * 1024 * 1024  # 12 MB - telefonska slika je oko 2-5 MB


def _bezbedno_ime(ime: str | None) -> str:
    """Ime fajla bez putanje i bez znakova koji bi izasli iz foldera."""
    osnova = Path(ime or "slika").name
    return re.sub(r"[^A-Za-z0-9._-]", "_", osnova)[:120] or "slika"


def sacuvaj(
    document_id: int,
    sadrzaj: bytes,
    *,
    mime: str | None,
    naziv_fajla: str | None,
    korisnik: str,
    line_no: int | None = None,
    napomena: str | None = None,
) -> Slika:
    """Upisuje sliku uz dokument. Vraca zapis; fajl je vec na disku."""
    if not sadrzaj:
        raise ValueError("Prazan fajl.")
    if len(sadrzaj) > NAJVECA:
        raise ValueError(f"Slika je veća od {NAJVECA // (1024 * 1024)} MB.")
    nastavak = DOZVOLJENE.get((mime or "").lower().split(";")[0].strip())
    if nastavak is None:
        raise ValueError("Dozvoljene su samo slike (JPG, PNG, HEIC, WEBP).")

    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"Dokument #{document_id} ne postoji.")
        if line_no is not None and not any(x.line_no == line_no for x in doc.lines):
            raise ValueError(f"Stavka {line_no} ne postoji na dokumentu.")

        sada = datetime.now(timezone.utc)
        folder = get_settings().storage_dir / "slike" / f"{sada.year:04d}" / f"{sada.month:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        ime = f"{document_id}-{sada.strftime('%H%M%S%f')[:-3]}{nastavak}"
        putanja = folder / ime
        putanja.write_bytes(sadrzaj)

        red = Slika(
            document_id=document_id,
            line_no=line_no,
            putanja=str(putanja),
            naziv_fajla=_bezbedno_ime(naziv_fajla),
            napomena=(napomena or "").strip()[:300] or None,
            poslao=korisnik,
            poslato_u=utcnow(),
        )
        session.add(red)
        session.add(
            AuditLog(
                actor=korisnik,
                action="slika.dodata",
                document_id=document_id,
                detail=f"{red.naziv_fajla} ({len(sadrzaj) // 1024} kB)"
                + (f", stavka {line_no}" if line_no else ""),
            )
        )
        session.flush()
        session.expunge(red)
        return red


def obrisi(slika_id: int, korisnik: str) -> bool:
    """Brise sliku i zapis. Fajl koji vise nije na disku nije greska."""
    with session_scope() as session:
        red = session.get(Slika, slika_id)
        if red is None:
            return False
        try:
            Path(red.putanja).unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 - zakljucan fajl ne sme da obori brisanje
            log.warning("Slika %s nije obrisana sa diska: %s", red.putanja, exc)
        session.add(
            AuditLog(
                actor=korisnik,
                action="slika.obrisana",
                document_id=red.document_id,
                detail=red.naziv_fajla,
            )
        )
        session.delete(red)
        return True
