"""Cekovi gradjana: citanje, provera, sef, predaja u banku, vraceni (Bane, 16.10.)."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace as NS

import pytest

from sefsync.services import cekovi as C

# Sa pravih cekova iz uzorka (data/cekovi, 16.10.).
UNICREDIT = "170000010137000049"
ERSTE_STAMPANO = "340 0032427249 65"
ERSTE = "340000003242724965"
OTP = "325930070767192722"


def _procitan(**kw) -> dict:
    c = {
        "banka": "UniCredit Bank", "serijski_broj": "0000044629437", "racun": UNICREDIT,
        "iznos_ciframa": 5000, "iznos_slovima_tekst": "pet hiljada dinara", "iznos_slovima": 5000,
        "datum": "2026-12-05", "mesto": "Odžaci",
        "sigurno": {"serijski_broj": True, "racun": True, "iznos": True, "datum": True},
    }
    c.update(kw)
    return c


# --------------------------------------------------------------------------- #
# provere bez modela
# --------------------------------------------------------------------------- #


def test_kontrolni_broj_racuna_sa_pravih_cekova():
    assert C.kontrolni_ok(UNICREDIT)
    assert C.kontrolni_ok(OTP)
    assert C.kontrolni_ok(ERSTE)
    assert not C.kontrolni_ok("170000010137000048")   # jedna cifra drugacija
    assert not C.kontrolni_ok("17000001013700004")    # kratak


def test_erste_partija_bez_vodecih_nula_se_dopunjuje():
    assert C.normalizuj_racun(ERSTE_STAMPANO) == ERSTE
    assert C.normalizuj_racun("340-32427249-65") == ERSTE
    assert C.normalizuj_racun(UNICREDIT) == UNICREDIT
    assert C.normalizuj_racun("") is None
    assert C.lep_racun(ERSTE) == "340-0000032427249-65"


def test_ispravan_cek_nema_crvenih_polja():
    p = C.proveri(_procitan(), primljeno=date(2026, 10, 15), oznaka="1-1")
    assert (p.serijski_broj, p.racun, p.iznos, p.datum) == (
        "0000044629437", UNICREDIT, 5000.0, date(2026, 12, 5))
    assert p.sumnjiva == []


def test_pogresna_cifra_racuna_je_crvena_iako_model_kaze_sigurno():
    """Desilo se na probi 16.10.: model je jednu cifru Erste racuna procitao pogresno."""
    p = C.proveri(_procitan(racun="3400032360468806"), primljeno=date(2026, 10, 15), oznaka="1-1")
    assert p.sumnjiva == ["racun"]


def test_model_nesiguran_u_racun_ali_kontrolni_prolazi_nije_crveno():
    p = C.proveri(_procitan(racun="340 003236046806",
                            sigurno={"serijski_broj": True, "racun": False, "iznos": True, "datum": True}),
                  primljeno=date(2026, 10, 15), oznaka="1-1")
    assert p.racun == "340000003236046806"
    assert "racun" not in p.sumnjiva


def test_iznos_ciframa_i_slovima_se_ne_slazu():
    p = C.proveri(_procitan(iznos_ciframa=5000, iznos_slovima=500),
                  primljeno=date(2026, 10, 15), oznaka="1-1")
    assert p.sumnjiva == ["iznos"]
    assert p.iznos == 5000.0


def test_prazan_iznos_je_5000_po_pravilu():
    p = C.proveri(_procitan(iznos_ciframa=None, iznos_slovima=None, iznos_slovima_tekst=None),
                  primljeno=date(2026, 10, 15), oznaka="1-1")
    assert p.iznos == 5000.0 and p.iznos_po_pravilu
    assert p.sumnjiva == []


def test_datum_van_dana_naplate_je_crven():
    """Cekovi idu na 5, 10, 15, 20, 25, 30 - 06.12. je skoro sigurno pogresna cifra."""
    p = C.proveri(_procitan(datum="2026-12-06"), primljeno=date(2026, 10, 15), oznaka="1-1")
    assert p.sumnjiva == ["datum"]
    # februar nema 30. - poslednji dan meseca je u redu
    p = C.proveri(_procitan(datum="2027-02-28"), primljeno=date(2026, 10, 15), oznaka="1-1")
    assert p.sumnjiva == []


def test_datum_daleko_od_prijema_ili_nepročitan_je_crven():
    assert C.proveri(_procitan(datum="2024-12-05"), primljeno=date(2026, 10, 15),
                     oznaka="1").sumnjiva == ["datum"]
    assert C.proveri(_procitan(datum=None), primljeno=date(2026, 10, 15),
                     oznaka="1").sumnjiva == ["datum"]


def test_nepročitan_serijski_dobija_privremenu_oznaku():
    p = C.proveri(_procitan(serijski_broj=None), primljeno=date(2026, 10, 15), oznaka="7-2")
    assert p.serijski_broj == "?7-2" and "serijski_broj" in p.sumnjiva


def test_pravoslavni_uskrs():
    assert [C.pravoslavni_uskrs(g) for g in (2024, 2025, 2026, 2027)] == [
        date(2024, 5, 5), date(2025, 4, 20), date(2026, 4, 12), date(2027, 5, 2)]


@pytest.mark.parametrize("na_ceku, u_banku", [
    (date(2026, 10, 10), date(2026, 10, 12)),   # subota -> ponedeljak
    (date(2026, 10, 15), date(2026, 10, 15)),   # radni dan
    (date(2026, 2, 15), date(2026, 2, 18)),     # Sretenje u nedelju: 16. i 17. se ne radi
    (date(2026, 4, 10), date(2026, 4, 14)),     # Veliki petak, Uskrs, Uskrsnji ponedeljak
    (date(2027, 5, 1), date(2027, 5, 5)),       # 1. maj + Uskrs 2.5.: ne radi se do 4.5.
])
def test_rok_predaje_preskace_vikend_i_praznike(na_ceku, u_banku):
    assert C.rok_predaje(na_ceku) == u_banku


# --------------------------------------------------------------------------- #
# citanje slike
# --------------------------------------------------------------------------- #


class _Lazni:
    def __init__(self, rezultat, stop="end_turn"):
        self.rezultat, self.stop, self.poslato = rezultat, stop, []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.poslato.append(kw)
        return NS(stop_reason=self.stop,
                  content=[NS(type="text", text=json.dumps(self.rezultat))])


@pytest.fixture
def jedinica(db):
    from sefsync.models import BusinessUnit, UnitKind

    with db.session_scope() as session:
        j = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        session.add(j)
        session.flush()
        return j.id


@pytest.fixture
def slika(jedinica, tmp_path, monkeypatch):
    from PIL import Image

    from sefsync.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_dir", tmp_path)
    buf = __import__("io").BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(buf, format="JPEG")
    return C.sacuvaj_sliku(jedinica, buf.getvalue(), mime="image/jpeg", naziv_fajla="c.jpg",
                           korisnik="predrag", primljeno=date(2026, 10, 15))


def test_slika_se_smanjuje_pre_slanja():
    from PIL import Image

    buf = __import__("io").BytesIO()
    Image.new("RGB", (4608, 3456), "white").save(buf, format="JPEG")
    media, podaci = C.slika_za_claude(buf.getvalue())
    import base64
    import io as _io

    malo = Image.open(_io.BytesIO(base64.b64decode(podaci)))
    assert media == "image/jpeg" and max(malo.size) == C.NAJDUZA_STRANA


def test_citanje_upisuje_cekove_i_ne_duplira(db, slika):
    from sefsync.models import Cek, SlikaCekova

    rezultat = {"cekovi": [_procitan(), _procitan(serijski_broj="0000001901886", racun=ERSTE_STAMPANO,
                                                  datum="2027-04-10", banka="Erste")],
                "napomena": None}
    klijent = _Lazni(rezultat)
    brojac = C.procitaj_sliku(slika, klijent=klijent)
    assert brojac == {"cekova": 2, "novih": 2, "vec_uneto": 0, "za_proveru": 0}
    zahtev = klijent.poslato[0]
    assert zahtev["output_config"]["format"]["type"] == "json_schema"
    assert zahtev["messages"][0]["content"][0]["type"] == "image"

    # ista slika poslata ponovo - cekovi se ne dupliraju
    assert C.procitaj_sliku(slika, klijent=_Lazni(rezultat))["vec_uneto"] == 2
    with db.session_scope() as session:
        assert session.query(Cek).count() == 2
        erste = session.query(Cek).filter_by(serijski_broj="0000001901886").one()
        assert erste.racun == ERSTE and erste.primljeno == date(2026, 10, 15)
        assert session.get(SlikaCekova, slika).procitano_cekova == 2


def test_greska_citanja_ostaje_na_slici(db, slika):
    from sefsync.models import SlikaCekova

    C.procitaj_sliku(slika, klijent=_Lazni({}, stop="refusal"))
    with db.session_scope() as session:
        s = session.get(SlikaCekova, slika)
        assert s.procitano_u is not None and "odbio" in s.greska


# --------------------------------------------------------------------------- #
# ispravka, predaja, vraceni
# --------------------------------------------------------------------------- #


def _cek(db, jedinica, **kw):
    from sefsync.models import Cek

    polja = dict(business_unit_id=jedinica, serijski_broj="0000044629437", racun=UNICREDIT,
                 iznos=5000.0, datum=date(2026, 10, 10), uneo="predrag")
    polja.update(kw)
    with db.session_scope() as session:
        c = Cek(**polja)
        session.add(c)
        session.flush()
        return c.id


def test_ispravka_trazi_ispravan_racun_i_potvrdjuje(db, jedinica):
    from sefsync.models import Cek

    cid = _cek(db, jedinica, sumnjiva=["datum"], razlozi=["datum nije jasno čitljiv"])
    with pytest.raises(ValueError, match="kontrolni"):
        C.ispravi(cid, korisnik="predrag", serijski_broj="0000044629437",
                  racun="170-0000101370000-48", iznos="5000", datum="2026-10-10")
    C.ispravi(cid, korisnik="predrag", serijski_broj="0000044629437",
              racun="170-0000101370000-49", iznos="5000.00", datum="2026-10-15")
    with db.session_scope() as session:
        c = session.get(Cek, cid)
        assert c.datum == date(2026, 10, 15) and not c.za_proveru and c.potvrdio == "predrag"


def test_predaja_pravi_presek_i_odbija_crvene(db, jedinica):
    from sefsync.models import Cek, PredajaCekova, StanjeCeka

    a = _cek(db, jedinica)
    b = _cek(db, jedinica, serijski_broj="0000001901886", racun=ERSTE, iznos=4500.0)
    crven = _cek(db, jedinica, serijski_broj="1000065897042", racun=OTP, sumnjiva=["iznos"])
    with db.session_scope() as session:
        za = [c.id for c in C.za_predaju(session, jedinica, dan=date(2026, 10, 12))]
        assert set(za) == {a, b, crven}
        # 10.10. je subota: pre ponedeljka nije za banku
        assert C.za_predaju(session, jedinica, dan=date(2026, 10, 11)) == []

    with pytest.raises(ValueError, match="crvena"):
        C.predaj(jedinica, [a, crven], korisnik="predrag", dan=date(2026, 10, 12))
    pid = C.predaj(jedinica, [a, b], korisnik="predrag", dan=date(2026, 10, 12))
    with db.session_scope() as session:
        p = session.get(PredajaCekova, pid)
        assert (p.komada, p.iznos) == (2, 9500.0)
        assert session.get(Cek, a).stanje is StanjeCeka.PREDAT
    with pytest.raises(ValueError, match="nije u sefu"):
        C.predaj(jedinica, [a], korisnik="predrag")


def test_vracen_cek_pa_resen(db, jedinica):
    from sefsync.models import Cek, StanjeCeka

    cid = _cek(db, jedinica)
    with pytest.raises(ValueError):
        C.vrati(cid, korisnik="predrag")            # nije ni predat
    C.predaj(jedinica, [cid], korisnik="predrag", dan=date(2026, 10, 12))
    C.vrati(cid, korisnik="predrag", napomena="nema pokrića")
    with pytest.raises(ValueError, match="kako je rešeno"):
        C.resi(cid, korisnik="predrag", napomena=" ")
    C.resi(cid, korisnik="predrag", napomena="kupac platio gotovinom")
    with db.session_scope() as session:
        assert session.get(Cek, cid).stanje is StanjeCeka.RESEN


# --------------------------------------------------------------------------- #
# ekran
# --------------------------------------------------------------------------- #


def _klijent_za(user):
    from fastapi.testclient import TestClient

    import sefsync.web.app as app_module

    app_module.app.dependency_overrides[app_module.current_user] = lambda: user
    return TestClient(app_module.app), app_module


def test_ekran_cekova_kod_poslovodje(db, jedinica, monkeypatch):
    from sefsync.web.auth import Role, User

    import sefsync.web.cekovi_rute as rute
    from sefsync.models import BusinessUnit

    _cek(db, jedinica, datum=date(2026, 10, 10))
    _cek(db, jedinica, serijski_broj="1000065897042", racun=OTP, datum=date(2027, 3, 5),
         sumnjiva=["datum"], razlozi=["datum nije jasno čitljiv"])
    monkeypatch.setattr(rute, "_unit_for", lambda s, u: s.get(BusinessUnit, jedinica))
    klijent, app_module = _klijent_za(User("predrag", "Bajic Predrag", Role.POSLOVODJA, "002", "POSLOVODJA"))
    try:
        strana = klijent.get("/cekovi").text
    finally:
        app_module.app.dependency_overrides.clear()
    assert "Uslikaj jučerašnje čekove" in strana
    assert "Proveri crvena polja" in strana and "datum nije jasno čitljiv" in strana
    assert "170-0000101370000-49" in strana


def test_ekran_slanje_slika_pokrece_citanje(db, jedinica, monkeypatch, tmp_path):
    from PIL import Image

    from sefsync.config import get_settings
    from sefsync.models import BusinessUnit, SlikaCekova
    from sefsync.web.auth import Role, User

    import sefsync.web.cekovi_rute as rute

    monkeypatch.setattr(get_settings(), "storage_dir", tmp_path)
    monkeypatch.setattr(rute, "_unit_for", lambda s, u: s.get(BusinessUnit, jedinica))
    procitano = []
    monkeypatch.setattr(C, "procitaj_sliku", lambda i, **k: procitano.append(i))
    buf = __import__("io").BytesIO()
    Image.new("RGB", (100, 80), "white").save(buf, format="JPEG")
    klijent, app_module = _klijent_za(User("predrag", "Bajic Predrag", Role.POSLOVODJA, "002", "POSLOVODJA"))
    try:
        odgovor = klijent.post("/cekovi/slike", data={"primljeno": "2026-10-15"},
                               files=[("slike", ("a.jpg", buf.getvalue(), "image/jpeg")),
                                      ("slike", ("b.jpg", buf.getvalue(), "image/jpeg"))],
                               follow_redirects=False)
    finally:
        app_module.app.dependency_overrides.clear()
    assert odgovor.status_code == 303
    with db.session_scope() as session:
        ids = [s.id for s in session.query(SlikaCekova).all()]
    assert sorted(procitano) == sorted(ids) and len(ids) == 2


def test_presek_i_pregled_za_kancelariju(db, jedinica):
    from sefsync.web.auth import Role, User

    a = _cek(db, jedinica)
    pid = C.predaj(jedinica, [a], korisnik="predrag", dan=date(2026, 10, 12))
    klijent, app_module = _klijent_za(User("bogdan", "Sunajko Bogdan", Role.OPERATER, None, "FINANSIJE"))
    try:
        presek = klijent.get(f"/cekovi/predaja/{pid}").text
        pregled = klijent.get("/cekovi").text
    finally:
        app_module.app.dependency_overrides.clear()
    assert "Predaja čekova u banku" in presek and "5 000,00" in presek
    assert "MP002" in pregled and f"/cekovi/predaja/{pid}" in pregled


# --------------------------------------------------------------------------- #
# blagajna: nalozi i dnevnik (papirne knjige iz objekta, 17.10.)
# --------------------------------------------------------------------------- #


def test_blagajna_ceo_tok_kao_u_knjizi(db, jedinica):
    from sefsync.models import NalogBlagajne, VrstaNaloga
    from sefsync.services import blagajna as B

    # pocetno stanje iz papirne knjige: saldo i sledeci brojevi (097 je poslednji upisan)
    B.postavi_pocetno(jedinica, datum=date(2026, 10, 1), saldo=1_000_000.0,
                      prvi_naplati=98, prvi_isplati=41, korisnik="predrag")
    # stari cek (unazad) je deo pocetnog salda
    _cek(db, jedinica, serijski_broj="9000000000001", racun=OTP, primljeno=None,
         datum=date(2026, 12, 5), iznos=1_000_000.0)

    a = _cek(db, jedinica, primljeno=date(2026, 10, 14), datum=date(2026, 10, 15))
    _cek(db, jedinica, serijski_broj="0000001901886", racun=ERSTE, primljeno=date(2026, 10, 14),
         datum=date(2026, 11, 15), iznos=4500.0)
    crven = _cek(db, jedinica, serijski_broj="1000065897042", racun=OTP,
                 primljeno=date(2026, 10, 15), datum=date(2026, 11, 20), sumnjiva=["datum"])

    with db.session_scope() as session:
        pazari = B.pazari_bez_naloga(session, jedinica)
    assert [(p["dan"], p["komada"], p["iznos"], p["crvenih"]) for p in pazari] == [
        (date(2026, 10, 14), 2, 9500.0, 0), (date(2026, 10, 15), 1, 5000.0, 1)]

    n1 = B.zakljuci_pazar(jedinica, date(2026, 10, 14), korisnik="predrag")
    with pytest.raises(ValueError, match="crvena"):
        B.zakljuci_pazar(jedinica, date(2026, 10, 15), korisnik="predrag")
    with pytest.raises(ValueError, match="nema čekova"):
        B.zakljuci_pazar(jedinica, date(2026, 10, 14), korisnik="predrag")

    pid = C.predaj(jedinica, [a], korisnik="predrag", dan=date(2026, 10, 15))
    with db.session_scope() as session:
        naplati = session.get(NalogBlagajne, n1)
        assert (naplati.oznaka, naplati.iznos, naplati.opis) == ("098/2026", 9500.0, "Čekovi pazar 14.10.")
        isplati = session.query(NalogBlagajne).filter_by(predaja_id=pid).one()
        assert isplati.vrsta is VrstaNaloga.ISPLATI and isplati.oznaka == "041/2026"
        assert isplati.opis == "Čekovi naplata 15.10." and isplati.iznos == 5000.0

        d = B.dnevnik(session, jedinica, date(2026, 10, 1), date(2099, 1, 1))
        assert (d.saldo_od, d.primitak, d.izdatak, d.saldo_do) == (1_000_000.0, 9500.0, 5000.0, 1_004_500.0)
        # u sefu: stari 1.000.000 + Erste 4.500; crveni sa nezatvorenog pazara se ne broji
        assert (d.u_sefu_komada, d.u_sefu_iznos, d.razlika) == (2, 1_004_500.0, 0.0)

        # period posle: saldo od = saldo do prethodnog
        d2 = B.dnevnik(session, jedinica, date(2026, 10, 16), date(2026, 10, 31))
        assert d2.saldo_od == 1_004_500.0 and d2.stavke == []

    # posle prvog naloga pocetno stanje se vise ne menja
    with pytest.raises(ValueError, match="već pravljeni"):
        B.postavi_pocetno(jedinica, datum=date(2026, 10, 1), saldo=1.0, prvi_naplati=1,
                          prvi_isplati=1, korisnik="predrag")


def test_bez_pocetnog_stanja_brojevi_krecu_od_1(db, jedinica):
    from sefsync.models import NalogBlagajne
    from sefsync.services import blagajna as B

    _cek(db, jedinica, primljeno=date(2026, 10, 14))
    n = B.zakljuci_pazar(jedinica, date(2026, 10, 14), korisnik="predrag")
    with db.session_scope() as session:
        assert session.get(NalogBlagajne, n).oznaka == "001/2026"


def test_ekrani_blagajne(db, jedinica, monkeypatch):
    from sefsync.models import BusinessUnit
    from sefsync.services import blagajna as B
    from sefsync.web.auth import Role, User

    import sefsync.web.cekovi_rute as rute

    _cek(db, jedinica, primljeno=date(2026, 10, 14))
    monkeypatch.setattr(rute, "_unit_for", lambda s, u: s.get(BusinessUnit, jedinica))
    klijent, app_module = _klijent_za(User("predrag", "Bajic Predrag", Role.POSLOVODJA, "002", "POSLOVODJA"))
    try:
        assert "Zatvori pazar" in klijent.get("/cekovi").text
        prazan = klijent.get("/cekovi/blagajna").text
        assert "Početno stanje" in prazan
        klijent.post("/cekovi/blagajna/pocetno", data={"datum": "2026-10-01", "saldo": "1833743",
                                                     "prvi_naplati": 98, "prvi_isplati": 41})
        odg = klijent.post("/cekovi/blagajna/zakljuci", data={"dan": "2026-10-14"},
                           follow_redirects=False)
        assert odg.status_code == 303 and odg.headers["location"].startswith("/cekovi/nalog/")
        nalog = klijent.get(odg.headers["location"]).text
        dnev = klijent.get("/cekovi/blagajna?od=2026-10-01&do=2026-10-31").text
    finally:
        app_module.app.dependency_overrides.clear()
    assert "НАЛОГ БЛАГАЈНИ ДА НАПЛАТИ" in nalog and "098/2026" in nalog
    assert "Čekovi pazar 14.10." in dnev and "1 833 743,00" in dnev and "1 838 743,00" in dnev
