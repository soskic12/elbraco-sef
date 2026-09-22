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


def test_poruka_kaze_pod_kojom_prijavom_je_pokusano(monkeypatch):
    """Spisak operatera se pise rukom, pa neslaganje imena mora da se vidi iz poruke.

    17.09. je Dusan ostao bez pristupa jer je u .env upisano "DusanSelak", a
    njegov nalog je "SelakDusan" - poruka to nije pokazivala.
    """
    import sefsync.web.auth as auth

    class LazniRed(dict):
        pass

    class LazniConn:
        def execute(self, *a, **k):
            return self

        def mappings(self):
            return self

        def first(self):
            return {"ime": "Selak Dušan", "funkcija": "PODRSKA", "objekat": "", "aktivan": True}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class LazniEngine:
        def connect(self):
            return LazniConn()

    monkeypatch.setattr(auth, "get_erp_engine", lambda: LazniEngine())
    podesavanja = get_settings()
    monkeypatch.setattr(podesavanja, "web_operators", "NinaR,DusanSelak")

    with pytest.raises(auth.AuthError) as greska:
        auth.load_person("SelakDusan", settings=podesavanja)

    assert "SelakDusan" in str(greska.value)


def test_prijava_bez_objekta_prolazi_ako_je_vezana_za_jedinicu(db, monkeypatch):
    """Ilijana u sifarniku osoblja nema objekat, a treba da radi na magacinu 010.

    `SifraPovezanogObjekta` koristi i projekat LOGISTIKA, pa se u njega ne sme
    upisivati nasa sema - veza covek-jedinica zato stoji u panelu.
    """
    import sefsync.web.auth as auth
    from sefsync.models import BusinessUnit, UnitKind

    with db.session_scope() as session:
        session.add(
            BusinessUnit(code="MAG010", name="Povrat robe", kind=UnitKind.WAREHOUSE,
                         erp_code="010", logins="ilijana", routable=False)
        )

    class Conn:
        def execute(self, *a, **k):
            return self

        def mappings(self):
            return self

        def first(self):
            return {"ime": "Krnjajić Ilijana", "funkcija": "REKLAMACIJE",
                    "objekat": "", "aktivan": True}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(auth, "get_erp_engine", lambda: type("E", (), {"connect": lambda self: Conn()})())
    podesavanja = get_settings()
    monkeypatch.setattr(podesavanja, "web_operators", "NinaR,SelakDusan")

    korisnik = auth.load_person("ilijana", settings=podesavanja)

    assert korisnik.role is Role.POSLOVODJA
    assert korisnik.unit_code == "010"
