"""Arhiviranje starih UBL fajlova."""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

import pytest

from sefsync.models import (
    Document,
    NacrtKalkulacije,
    SefStatus,
    StanjeNacrta,
    utcnow,
)


def _dokument(db, tmp_path, *, dana_staro: int, sadrzaj=b"<Invoice/>", sef_id=1):
    kad = utcnow() - dt.timedelta(days=dana_staro)
    fajl = tmp_path / f"{sef_id}.xml"
    fajl.write_bytes(sadrzaj)
    with db.session_scope() as session:
        doc = Document(
            sef_invoice_id=sef_id, document_number=f"FA-{sef_id}",
            supplier_name="Dobavljač", sef_status=SefStatus.APPROVED,
            sent_date=kad, ubl_path=str(fajl),
        )
        session.add(doc)
        session.flush()
        return doc.id


def test_svezi_ubl_ostaje_na_disku(db, tmp_path):
    from sefsync.services.arhiva import za_arhivu

    _dokument(db, tmp_path, dana_staro=10, sef_id=1)

    assert za_arhivu(60) == {}


def test_stari_ubl_ide_u_arhivu(db, tmp_path):
    from sefsync.services.arhiva import arhiviraj

    doc_id = _dokument(db, tmp_path, dana_staro=200, sef_id=2)
    fajl = Path(tmp_path / "2.xml")

    ishod = arhiviraj(60, stvarno=True)

    assert ishod["obrisano"] == 1
    assert not fajl.exists(), "fajl je spakovan pa je trebalo da nestane"
    with db.session_scope() as session:
        assert session.get(Document, doc_id).ubl_arhiviran is not None


def test_nezavrsen_dokument_zadrzava_svoj_ubl(db, tmp_path):
    """Fajl koji nam treba sutra ne sme da ode, ma koliko bio star."""
    from sefsync.services.arhiva import za_arhivu

    doc_id = _dokument(db, tmp_path, dana_staro=200, sef_id=3)
    with db.session_scope() as session:
        session.add(NacrtKalkulacije(document_id=doc_id, stanje=StanjeNacrta.CEKA_SIFRU))

    assert za_arhivu(60) == {}


def test_ne_brise_dok_ne_proveri_da_je_sve_u_zipu(db, tmp_path, monkeypatch):
    """Brisanje je nepovratno — ide tek posle provere fajl po fajl."""
    from sefsync.services import arhiva

    _dokument(db, tmp_path, dana_staro=200, sef_id=4, sadrzaj=b"<Invoice>veliki</Invoice>")
    fajl = Path(tmp_path / "4.xml")

    class ZipKojiNistaNeUpise(zipfile.ZipFile):
        def write(self, *a, **k):   # noqa: A003 - namerno ne radi nista
            return None

    monkeypatch.setattr(arhiva.zipfile, "ZipFile", ZipKojiNistaNeUpise)
    ishod = arhiva.arhiviraj(60, stvarno=True)

    assert ishod["obrisano"] == 0
    assert fajl.exists(), "fajl je obrisan iako nije u zipu"
    assert any("nije u zipu" in (m.get("greska") or "") for m in ishod["meseci"].values())


def test_arhiviran_ubl_se_vraca_sa_sef_a(db, tmp_path, monkeypatch):
    """Arhiva nije radni materijal — sadržaj se po potrebi skida ponovo."""
    from sefsync.services import arhiva

    doc_id = _dokument(db, tmp_path, dana_staro=200, sef_id=5, sadrzaj=b"<Invoice>5</Invoice>")
    arhiva.arhiviraj(60, stvarno=True)

    class LazniSef:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def purchase_ubl(self, sef_id):
            return b"<Invoice>sa SEF-a</Invoice>"

    import sefsync.sef.client as klijent_modul

    monkeypatch.setattr(klijent_modul, "SefClient", lambda *a, **k: LazniSef())

    with db.session_scope() as session:
        doc = session.get(Document, doc_id)
        sadrzaj = arhiva.ubl_bajtovi(doc)

    assert sadrzaj == b"<Invoice>sa SEF-a</Invoice>"
    assert Path(tmp_path / "5.xml").exists(), "vraćeni UBL se snima nazad"
