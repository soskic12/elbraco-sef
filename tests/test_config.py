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


def test_radni_folder_kad_u_njemu_ima_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SEFSYNC_HOME", raising=False)
    (tmp_path / ".env").write_text("DB_URL=sqlite:///x.db", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _project_root() == tmp_path


def test_goli_data_folder_nije_instalacija(tmp_path, monkeypatch):
    r"""`data\` nastane čim komanda jednom promaši — ne sme da važi kao dokaz.

    Zbog toga je zaštita bila slepa: 06.10. je `nacrti --osvezi` „završio za
    0 s“ nad praznom bazom u `C:\Users\Administrator`, jer je tamo od ranije
    stajao prazan `data\` folder.
    """
    _cist_env(monkeypatch, tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)

    assert _project_root() != tmp_path


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
    odrediste.mkdir(parents=True)
    (odrediste / ".env").write_text("DB_URL=sqlite:///x.db", encoding="utf-8")
    monkeypatch.chdir(odrediste)

    assert ".venv" not in str(_project_root())
    assert _project_root() == odrediste


# --------------------------------------------------------------------------- #
# pokretanje iz pogresnog foldera
# --------------------------------------------------------------------------- #


def _cist_env(monkeypatch, tmp_path=None):
    for ime in ("SEFSYNC_HOME", "DB_URL", "SEF_DISABLE_DOTENV"):
        monkeypatch.delenv(ime, raising=False)
    if tmp_path is not None:
        # Instalacija se prepoznaje i po tome gde stoji virtuelno okruzenje.
        # U testu to vodi na sam repozitorijum, pa se mora skloniti da bi se
        # uopste moglo isprobati „pokrenuto van instalacije“.
        import sys

        monkeypatch.setattr(sys, "executable", str(tmp_path / "x" / "y" / "python.exe"))


def test_folder_bez_instalacije_se_prijavljuje(tmp_path, monkeypatch):
    r"""Komanda pokrenuta iz C:\Users\Administrator mora da stane, ne da uspe.

    Bez ovoga uzme podrazumevano podesavanje, napravi praznu bazu pored i radi
    nad njom - sto je 16.09. na serveru progutalo pravila-uvoz i osvezi-ubl.
    """
    from sefsync import config

    _cist_env(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)

    problem = config.dom_nije_instalacija()

    assert problem is not None
    assert str(tmp_path) in problem


def test_folder_instalacije_prolazi(tmp_path, monkeypatch):
    from sefsync import config

    _cist_env(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("SEF_ENV=prod", encoding="utf-8")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)

    assert config.dom_nije_instalacija() is None


def test_izricito_zadata_baza_ne_trazi_folder(tmp_path, monkeypatch):
    """Testovi i posebna podesavanja zadaju DB_URL - njima folder nije potreban."""
    from sefsync import config

    _cist_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)

    assert config.dom_nije_instalacija() is None


def test_komanda_staje_umesto_da_radi_nad_praznom_bazom(tmp_path, monkeypatch):
    """Provera da je zastita zaista zakacena na pokretanje svake komande."""
    from sefsync import cli, config

    _cist_env(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)

    with pytest.raises(typer.Exit) as izlaz:
        cli._boot()

    assert izlaz.value.exit_code == 2


def test_sqlite_u_produkciji_pise_u_log(caplog, monkeypatch, tmp_path):
    """SQLite sa uključenim upisom u produkciju mora da se vidi u logu.

    `.env.server` je 21.09. rekao da je SQLite dovoljan — „jedan operater,
    nekoliko hiljada dokumenata godišnje“. Mereno 08.10.: 8.467 dokumenata u 27
    dana i nekoliko ljudi istovremeno. Odluka je ostala nezapažena mesec dana jer
    nigde nije pisala.
    """
    import logging

    from sefsync import db as db_module

    class Lazna:
        db_url = "sqlite:///C:/efakture/data/sefsync.db"
        db_echo = False
        erp_allow_production_write = True
        storage_dir = tmp_path

    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "get_settings", lambda: Lazna())
    monkeypatch.setattr(db_module, "_prati_veze", lambda eng: None)

    with caplog.at_level(logging.WARNING, logger="sefsync.db"):
        db_module.get_engine()

    assert any("SQLite" in r.message for r in caplog.records)
    monkeypatch.setattr(db_module, "_engine", None)


def test_sqlite_bez_upisa_u_produkciju_ne_dize_uzbunu(caplog, monkeypatch, tmp_path):
    """Razvojna mašina radi na SQLite i to je u redu."""
    import logging

    from sefsync import db as db_module

    class Lazna:
        db_url = "sqlite:///data/sefsync.db"
        db_echo = False
        erp_allow_production_write = False
        storage_dir = tmp_path

    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "get_settings", lambda: Lazna())
    monkeypatch.setattr(db_module, "_prati_veze", lambda eng: None)

    with caplog.at_level(logging.WARNING, logger="sefsync.db"):
        db_module.get_engine()

    assert not [r for r in caplog.records if "SQLite" in r.message]
    monkeypatch.setattr(db_module, "_engine", None)


def test_promena_baze_se_prijavljuje(tmp_path, monkeypatch, caplog):
    """Panel je 07.10. presao na drugu bazu i to niko nije primetio 26 sati.

    Iz `.env`-a je nestao red DB_URL, panel je mirno nastavio nad zaostalim
    SQLite fajlom, a rad ljudi je isao na pogresno mesto.
    """
    import logging

    from sefsync import db as db_module

    class Lazna:
        db_url = "sqlite:///novo.db"
        db_echo = False
        erp_allow_production_write = False
        storage_dir = tmp_path

    monkeypatch.setattr(db_module, "get_settings", lambda: Lazna())
    monkeypatch.setattr(db_module, "_prati_veze", lambda eng: None)
    monkeypatch.setattr(db_module, "_promena_baze", None)

    # Panel je ranije radio nad drugom bazom.
    (tmp_path / db_module.IME_ZAPISA).write_text(
        "mssql+pyodbc://sefsync_app:***@server/SefSync" + chr(10), encoding="utf-8"
    )

    monkeypatch.setattr(db_module, "_engine", None)
    with caplog.at_level(logging.ERROR, logger="sefsync.db"):
        db_module.get_engine()

    promena = db_module.promena_baze()
    assert promena is not None
    assert "SefSync" in promena[0] and "novo.db" in promena[1]
    assert any("PROMENJENA" in r.message for r in caplog.records)

    # Dok se ne potvrdi, prijava stoji; potvrda je gasi.
    assert db_module.potvrdi_bazu().endswith("novo.db")
    assert db_module.promena_baze() is None
    monkeypatch.setattr(db_module, "_engine", None)


def test_ista_baza_ne_dize_uzbunu(tmp_path, monkeypatch):
    from sefsync import db as db_module

    class Lazna:
        db_url = "sqlite:///isto.db"
        db_echo = False
        erp_allow_production_write = False
        storage_dir = tmp_path

    monkeypatch.setattr(db_module, "get_settings", lambda: Lazna())
    monkeypatch.setattr(db_module, "_prati_veze", lambda eng: None)
    monkeypatch.setattr(db_module, "_promena_baze", None)
    (tmp_path / db_module.IME_ZAPISA).write_text("sqlite:///isto.db" + chr(10), encoding="utf-8")

    monkeypatch.setattr(db_module, "_engine", None)
    db_module.get_engine()

    assert db_module.promena_baze() is None
    monkeypatch.setattr(db_module, "_engine", None)
