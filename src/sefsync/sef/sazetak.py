"""Sazeti prikaz fakture - prva strana SEF-ovog prosirenog PDF-a.

SEF za ulazne fakture ima **samo jedan** PDF poziv, `purchase-invoice/pdf`, i on
vraca prosireni prikaz. Ali taj prikaz u sebi vec sadrzi sazetak: prva strana je
obicna faktura (zaglavlje, stavke, iznosi), a od druge pocinje strukturirani
ispis svakog polja po standardu.

Provereno 09.10.2026. na `26-30F-100058`: tekst prve strane prosirenog PDF-a i
tekst sazetka koji portal nudi poklapaju se **100 %**. Dakle sazetak se ne trazi
sa SEF-a nego se dobija odsecanjem - bez ijednog dodatnog poziva.

Odseca se po naslovu kojim strukturirani deo pocinje, ne po broju strana:
faktura sa mnogo stavki ima vise strana pre njega.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# Naslov kojim pocinje strukturirani deo. Tacan tekst sa SEF-ovog PDF-a.
POCETAK_STRUKTURE = "Strukturirani format elektronske fakture"


def samo_sazetak(pdf: bytes) -> bytes:
    """Vraca PDF bez strukturiranog dela. Kad se deo ne nadje, vraca original.

    Nikad ne vraca prazan PDF: ako bi odsecanje pojelo sve strane, bolje je dati
    ceo dokument nego prazan list.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover - pypdf je zavisnost
        return pdf

    try:
        citac = PdfReader(io.BytesIO(pdf))
        granica = None
        for i, strana in enumerate(citac.pages):
            if POCETAK_STRUKTURE in (strana.extract_text() or ""):
                granica = i
                break
        if not granica:  # nema ga, ili je vec na prvoj strani
            return pdf

        pisac = PdfWriter()
        for strana in citac.pages[:granica]:
            pisac.add_page(strana)
        izlaz = io.BytesIO()
        pisac.write(izlaz)
        return izlaz.getvalue()
    except Exception as exc:  # noqa: BLE001 - radije ceo PDF nego nikakav
        log.warning("Sazetak nije napravljen, saljem ceo PDF: %s", exc)
        return pdf
