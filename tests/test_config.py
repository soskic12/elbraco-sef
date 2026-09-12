"""Gde aplikacija trazi .env i data/ - razlikuje se izvorni folder od instalacije."""

from __future__ import annotations

from pathlib import Path

from sefsync.config import _project_root


def test_izricito_zadato_ima_prednost(tmp_path, monkeypatch):
    monkeypatch.setenv("SEFSYNC_HOME", str(tmp_path))

    assert _project_root() == tmp_path.resolve()


def test_radni_folder_kad_u_njemu_ima_env(tmp_path, monkeypatch):
    """Servis se pokrece iz C:\efakture, gde stoje .env i data\."""
    monkeypatch.delenv("SEFSYNC_HOME", raising=False)
    (tmp_path / ".env").write_text("SEF_ENV=prod", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _project_root() == tmp_path


def test_radni_folder_kad_u_njemu_ima_data(tmp_path, monkeypatch):
    monkeypatch.delenv("SEFSYNC_HOME", raising=False)
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)

    assert _project_root() == tmp_path


def test_izvorni_folder_projekta(tmp_path, monkeypatch):
    """Pri radu iz repozitorijuma, koren je folder sa pyproject.toml."""
    monkeypatch.delenv("SEFSYNC_HOME", raising=False)
    prazan = tmp_path / "prazan"
    prazan.mkdir()
    monkeypatch.chdir(prazan)

    koren = _project_root()

    assert (koren / "pyproject.toml").exists()


def test_baza_nikad_ne_zavrsi_u_venv(tmp_path, monkeypatch):
    """Instaliran paket ne sme da drzi bazu u sopstvenom okruzenju."""
    monkeypatch.delenv("SEFSYNC_HOME", raising=False)
    odrediste = tmp_path / "efakture"
    (odrediste / "data").mkdir(parents=True)
    monkeypatch.chdir(odrediste)

    assert ".venv" not in str(_project_root())
    assert _project_root() == odrediste
