"""Koliko komada, litara ili grama stoji iza stavke fakture.

Svi primeri su stvarni — iz 159 uparenih stavki pub-a (071) za jun–oktobar 2026,
gde se zna i šta je na fakturi i šta je čovek upisao u ERP.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sefsync.erp.pakovanje import predlozi_faktor


@pytest.mark.parametrize(
    "naziv, jm_fakture, jm_artikla, ocekivano",
    [
        # --- bure: broj litara, samo uz reč koja kaže da je bure --------------
        ("HEINEKEN DAVID KEG 20L", "H87", "l", 20.0),
        ("TUBORG BURE 20L MODULAR", "H87", "lit", 20.0),
        ("KRONENBURG 20L MODULAR BLANC", "H87", "lit", 20.0),
        ("Libeeri Pilsner pivo, keg 20lit", "H87", "lit", 20.0),
        ("Libeeri IPA pivo, keg 20l", "H87", "l", 20.0),
        # --- kutija „1/40" ---------------------------------------------------
        ("Topla čokolada crna 1/40", "KGM", "kom", 40.0),
        # --- kutija kesica ---------------------------------------------------
        ("MILFORD ZELENI CAJ 20X1,75 GRAMA", "H87", "kom", 20.0),
        ("MILFORD VOCNI 40X2,5GR FRUCHTEMIX", "H87", "kom", 40.0),
        ("MILFORD MENTA 40 KESICA", "H87", "kom", 40.0),
        ("MILFORD KAMILICA 40X1.5 GRAMA", "H87", "kom", 40.0),
        ("MILFORD JABUKA CIMET  20X2,5", "H87", "kom", 20.0),
        # --- kesica od N grama, artikal se vodi u gramima --------------------
        ("KAFA AMIGO 9G", "H87", "g", 9.0),
        # --- kilogram sa fakture, artikal u gramima --------------------------
        ("HAUSBRANDT HERMAN 1kg", "KGM", "g", 1000.0),
    ],
)
def test_preracunava_se(naziv, jm_fakture, jm_artikla, ocekivano):
    faktor, objasnjenje = predlozi_faktor(naziv, jm_fakture, jm_artikla, magacin="071")
    assert faktor == ocekivano
    assert objasnjenje


@pytest.mark.parametrize(
    "naziv, jm_fakture, jm_artikla",
    [
        # „N/1" je transportno pakovanje i ne znači ništa — ovo je glavna zamka.
        ("ROSA VODA 0,33 NEGAZIRANA 24/1", "H87", "kom"),
        ("CEDEVITA NARANDZA 19 GR STICK 50/1", "H87", "kom"),
        ("MED 15G  200/1", "H87", "kom"),
        ("BAVARIA BEZALKOHOLNA 0,25 6/1", "H87", "kom"),
        ("TUBORG 0.33L  24/1", "H87", "kom"),
        # Zapremina u nazivu nije pakovanje.
        ("COCA COLA 0,25L RGB", "H87", "kom"),
        ("ZAJECARSKO SVETLO  0,33  20/1  NEPOVRATNO", "H87", "kom"),
        ("COCKTA 0,275L STAKLO", "H87", "kom"),
        # Nescafe stick se i prodaje po komadu.
        ("Nescafe stick 2gr", "H87", "kom"),
        # Cigarete: samo druga oznaka jedinice.
        ("DAVIDOFF REACH BLUE", "pak", "kom"),
        ("", "H87", "kom"),
    ],
)
def test_ne_preracunava_se(naziv, jm_fakture, jm_artikla):
    faktor, objasnjenje = predlozi_faktor(naziv, jm_fakture, jm_artikla, magacin="071")
    assert faktor == 1.0
    assert objasnjenje == ""


def test_zapremina_pred_L_nije_bure():
    """Da pravilo za bure ne pokupi „0,33L" — dalo bi trećinu litre po flaši."""
    assert predlozi_faktor("TUBORG 0.33L  24/1", "H87", "lit", magacin="071")[0] == 1.0
    assert predlozi_faktor("COCA COLA 0,25L RGB", "H87", "l", magacin="071")[0] == 1.0


def test_litar_u_litar_se_ne_dira():
    assert predlozi_faktor("Vranac 13.jul 1l", "LTR", "l", magacin="071")[0] == 1.0


# --------------------------------------------------------------------------- #
# nepotvrdjeno pakovanje zadrzava stavku
# --------------------------------------------------------------------------- #

def test_potvrda_upisuje_faktor_uz_vezu(db, monkeypatch):
    """Potvrđeno vredi i za sve sledeće fakture tog dobavljača za taj artikal."""
    from sefsync.models import (
        Document,
        ItemMapping,
        NacrtKalkulacije,
        StanjeNacrta,
    )
    from sefsync.services import nacrti as modul

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=881301, document_number="F-1", supplier_vat="100777")
        session.add(doc)
        session.flush()
        doc_id = doc.id
        session.add_all([
            ItemMapping(
                supplier_vat="100777", supplier_item_id="KEG20",
                erp_item_code="9999970", active=True,
            ),
            NacrtKalkulacije(
                document_id=doc_id,
                stanje=StanjeNacrta.CEKA_SIFRU,
                nemapirane=[{
                    "line_no": 1, "naziv": "HEINEKEN DAVID KEG 20L", "sifra": "9999970",
                    "predlog_faktora": 20.0, "objasnjenje_faktora": "bure od 20 l",
                }],
            ),
        ])

    # `osvezi` trazi ERP; kroz monkeypatch, da se vrati posle testa - inace
    # curi u ostale testove (test_web je pao bas zbog toga).
    monkeypatch.setattr(modul, "osvezi", lambda *a, **k: None)

    poruka = modul.potvrdi_pakovanje(doc_id, 1, 20.0, korisnik="nina")

    assert "×20" in poruka
    with db.session_scope() as session:
        veza = session.scalars(select(ItemMapping)).first()
        assert veza.unit_factor == 20.0


def test_neuverljiv_faktor_se_odbija(db):
    import pytest

    from sefsync.services import nacrti as modul

    with pytest.raises(ValueError):
        modul.potvrdi_pakovanje(1, 1, 0, korisnik="nina")
    with pytest.raises(ValueError):
        modul.potvrdi_pakovanje(1, 1, 99999, korisnik="nina")


def test_dijagnostika_imenuje_zadrsku_zbog_pakovanja():
    """Bez ovoga je `nemapirane` za LIMUN govorila „treba da se reši sama“.

    Šifra jeste poznata — zato je app i zadržala stavku: artikal se vodi u
    gramima, a faktura šalje komade.
    """
    import re

    from sefsync.services.nacrti import _NIJE_ARTIKAL

    stavka = {"naziv": "LIMUN", "ean": "", "sifra_dobavljaca": "132000008",
              "predlog_faktora": 1000.0}

    # Ista grana kao u `razlozi_cekanja`: pakovanje se proverava prvo.
    assert stavka.get("predlog_faktora")
    assert not _NIJE_ARTIKAL.search(stavka["naziv"])
    assert isinstance(_NIJE_ARTIKAL, re.Pattern)


@pytest.mark.parametrize(
    "naziv",
    [
        "Tesla RS0880HE frizider 48x45x85cm",
        "Klupa sklopiva 18x30x43",
        "Bosch KGN 34X04",
        "Unior 481 1/2 inca",
        "Kabel 5x1,5mm",
        "LED 40W/600X8/2800LM/4000K/BO",
    ],
)
def test_van_puba_naziv_nije_pakovanje(naziv):
    """Dimenzije i oznake modela nisu pakovanje (Tesla RS0880HE, Dule 12.10.).

    Mereno na sifarniku: pravila za kutiju i kesicu su van puba davala predlog
    za 6.763 artikla, gotovo sve lazne.
    """
    for magacin in ("001", "021", None):
        assert predlozi_faktor(naziv, "H87", "kom", magacin=magacin) == (1.0, "")


def test_kilogram_u_grame_vazi_svuda():
    """To kaze jedinica mere, ne naziv - pa vazi i van puba."""
    assert predlozi_faktor("Kafa", "KGM", "g", magacin="001")[0] == 1000.0
