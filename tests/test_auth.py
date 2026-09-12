"""Ko sme u panel: uloga se izvodi iz sifarnika osoblja i imenovanog spiska."""

from __future__ import annotations

import pytest

from sefsync.config import get_settings
from sefsync.web.auth import AuthError, Role, _connection_for, load_person

OSOBA = {"ime": "Nina Nikolić", "funkcija": "FAKTURISANJE", "objekat": "", "aktivan": 1}
POSLOVODJA = {"ime": "Bajic Predrag", "funkcija": "POSLOVODJA", "objekat": "002", "aktivan": 1}


def _staff(monkeypatch, row):
    """Zamenjuje upit ka ERP-u jednim redom iz sifarnika."""
    class LazniRezultat:
        def mappings(self):
            return self

        def first(self):
            return row

    class LaznaVeza:
        def execute(self, *args, **kwargs):
            return LazniRezultat()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class LazniEngine:
        def connect(self):
            return LaznaVeza()

        def dispose(self):
            pass

    monkeypatch.setattr("sefsync.web.auth.get_erp_engine", lambda: LazniEngine())


def test_imenovani_operater(monkeypatch):
    _staff(monkeypatch, OSOBA)
    s = get_settings()
    monkeypatch.setattr(s, "web_operators", "nina, selak")

    user = load_person("Nina", s)

    assert user.role is Role.OPERATER
    assert user.name == "Nina Nikolić"


def test_neimenovani_nalog_nema_pristup(monkeypatch):
    """Marketing i magacin imaju naloge, ali u ovom panelu nemaju šta da rade."""
    _staff(monkeypatch, {**OSOBA, "ime": "Krtinić Sonja", "funkcija": "MARKETING"})
    s = get_settings()
    monkeypatch.setattr(s, "web_operators", "nina,selak")

    with pytest.raises(AuthError, match="nema pristup"):
        load_person("sonja", s)


def test_poslovodja_dobija_svoj_objekat(monkeypatch):
    _staff(monkeypatch, POSLOVODJA)
    s = get_settings()
    monkeypatch.setattr(s, "web_operators", "nina,selak")

    user = load_person("predrag", s)

    assert (user.role, user.unit_code) == (Role.POSLOVODJA, "002")


def test_operater_zadrzava_puna_prava_i_kad_ima_objekat(monkeypatch):
    """Zamena može biti vezana za objekat, a i dalje radi kao operater."""
    _staff(monkeypatch, {**POSLOVODJA, "ime": "Selak Dušan", "objekat": "004"})
    s = get_settings()
    monkeypatch.setattr(s, "web_operators", "nina,selak")

    user = load_person("selak", s)

    assert user.role is Role.OPERATER


def test_ugasen_nalog(monkeypatch):
    _staff(monkeypatch, {**OSOBA, "aktivan": 0})
    with pytest.raises(AuthError, match="nije aktivan"):
        load_person("nina", get_settings())


def test_nalog_van_sifarnika(monkeypatch):
    _staff(monkeypatch, None)
    with pytest.raises(AuthError, match="nije upisan u šifarnik"):
        load_person("neko", get_settings())


def test_lozinka_ne_ostaje_u_osnovnoj_vezi():
    veza = _connection_for("nina", "taj@na", "mssql+pyodbc://sluzbeni:stara@srv:1433/ELBS_2026?x=1")

    assert veza.startswith("mssql+pyodbc://nina:taj%40na@srv:1433/ELBS_2026")
    assert "sluzbeni" not in veza and "stara" not in veza
