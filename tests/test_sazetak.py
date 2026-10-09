"""Sažetak fakture — prva strana SEF-ovog proširenog PDF-a.

Provereno 09.10.2026. na `26-30F-100058`: tekst prve strane proširenog PDF-a i
tekst sažetka koji portal nudi poklapaju se 100 %. Zato se sažetak ne traži sa
SEF-a nego se dobija odsecanjem.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from sefsync.sef.sazetak import POCETAK_STRUKTURE, samo_sazetak

PRIMER = next(Path("docs").glob("*extended*"), None)


@pytest.mark.skipif(PRIMER is None, reason="nema primer PDF-a u docs/")
def test_odseca_strukturirani_deo():
    from pypdf import PdfReader

    ceo = PRIMER.read_bytes()
    sazetak = samo_sazetak(ceo)

    strana_celog = PdfReader(io.BytesIO(ceo)).pages
    strana_sazetka = PdfReader(io.BytesIO(sazetak)).pages
    assert len(strana_sazetka) < len(strana_celog)
    assert sazetak[:5] == b"%PDF-"

    tekst = "\n".join((s.extract_text() or "") for s in strana_sazetka)
    assert POCETAK_STRUKTURE not in tekst
    # Ono što čoveku treba mora da ostane.
    assert "Iznos za plaćanje" in tekst


def test_pdf_bez_strukturiranog_dela_ostaje_ceo():
    """Ako markera nema, ne odseca se ništa — bolje ceo nego prazan list."""
    from pypdf import PdfWriter

    pisac = PdfWriter()
    pisac.add_blank_page(width=200, height=200)
    izlaz = io.BytesIO()
    pisac.write(izlaz)
    ceo = izlaz.getvalue()

    assert samo_sazetak(ceo) == ceo


def test_neispravan_pdf_se_vraca_nedirnut():
    assert samo_sazetak(b"ovo nije PDF") == b"ovo nije PDF"
