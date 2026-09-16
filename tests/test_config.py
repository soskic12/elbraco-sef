"""Gde aplikacija trazi .env i data/ - razlikuje se izvorni folder od instalacije."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

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


# --------------------------------------------------------------------------- #
# pokretanje iz pogresnog foldera
# --------------------------------------------------------------------------- #


def _cist_env(monkeypatch):
    for ime in ("SEFSYNC_HOME", "DB_URL", "SEF_DISABLE_DOTENV"):
        monkeypatch.delenv(ime, raising=False)


def test_folder_bez_instalacije_se_prijavljuje(tmp_path, monkeypatch):
    r"""Komanda pokrenuta iz C:\Users\Administrator mora da stane, ne da uspe.

    Bez ovoga uzme podrazumevano podesavanje, napravi praznu bazu pored i radi
    nad njom - sto je 16.09. na serveru progutalo pravila-uvoz i osvezi-ubl.
    """
    from sefsync import config

    _cist_env(monkeypatch)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

    problem = config.dom_nije_instalacija()

    assert problem is not None
    assert str(tmp_path) in problem


def test_folder_instalacije_prolazi(tmp_path, monkeypatch):
    from sefsync import config

    _cist_env(monkeypatch)
    (tmp_path / ".env").write_text("SEF_ENV=prod", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

    assert config.dom_nije_instalacija() is None


def test_izricito_zadata_baza_ne_trazi_folder(tmp_path, monkeypatch):
    """Testovi i posebna podesavanja zadaju DB_URL - njima folder nije potreban."""
    from sefsync import config

    _cist_env(monkeypatch)
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

    assert config.dom_nije_instalacija() is None


def test_komanda_staje_umesto_da_radi_nad_praznom_bazom(tmp_path, monkeypatch):
    """Provera da je zastita zaista zakacena na pokretanje svake komande."""
    from sefsync import cli, config

    _cist_env(monkeypatch)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

    with pytest.raises(typer.Exit) as izlaz:
        cli._boot()

    assert izlaz.value.exit_code == 2
