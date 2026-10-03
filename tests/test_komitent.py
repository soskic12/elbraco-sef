"""Otvaranje novog komitenta u ERP-ovom šifarniku."""

from __future__ import annotations

import pytest

from sefsync.erp.komitent import PredlogKomitenta, skrati_naziv, slicni_komitenti, upisi


class _Rez:
    def __init__(self, redovi):
        self._r = redovi

    def first(self):
        return self._r[0] if self._r else None

    def __iter__(self):
        return iter(self._r)


def _red(**kw):
    return type("R", (), kw)()


# --------------------------------------------------------------------------- #
# naziv
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pun, mesto, ocekivano", [
    ("PREDUZEĆE ZA SERVIS I TRGOVINU VITEL DOO NIŠ", "Niš", "Vitel doo"),
    ("EKSER PROIZVODNJA EXPORT-IMPORT I TRGOVINA DOO SOMBOR", "Sombor", "Ekser doo"),
    ("SINAGOGA DRUŠTVO SA OGRANIČENOM ODGOVORNOŠĆU SOMBOR", "Sombor", "Sinagoga doo"),
    ("ЈАВНО КОМУНАЛНО ПРЕДУЗЕЋЕ ВОДОКАНАЛ СОМБОР", "Sombor", "Vodokanal JKP"),
    ("BCE GROUP DOO ČAČAK", "Čačak", "Bce group doo"),
])
def test_skracivanje_naziva_po_pravilu_iz_sifarnika(pun, mesto, ocekivano):
    """Brend prvo, pa skraćeni pravni oblik — bez opisnog dela i bez mesta.

    Pravilo je pročitano iz šifarnika na 195 parova (pun naziv sa SEF-a ↔
    postojeći NAZIV1), nije izmišljeno.
    """
    assert skrati_naziv(pun, mesto) == ocekivano


def test_naziv_nema_tacke_u_pravnom_obliku():
    """„d.o.o.“ je istorijska greška operatera — ne ponavlja se (Bane, 03.10.)."""
    assert skrati_naziv("RAIFFEISEN BANKA A.D.", "Beograd") == "Raiffeisen banka ad"
    assert "." not in skrati_naziv("Comtrade Distribution d.o.o. Beograd", "Beograd")


def test_naziv_staje_u_trideset_znakova():
    """NAZIV1 je varchar(30) — duži naziv bi pukao pri upisu."""
    dug = "PRIVREDNO DRUŠTVO ZA PROIZVODNJU I PROMET ELEKTRONIKE MEGATREND DOO"
    assert len(skrati_naziv(dug, "")) <= 30


# --------------------------------------------------------------------------- #
# provera da vec ne postoji
# --------------------------------------------------------------------------- #


class _ConnSifarnik:
    def __init__(self, po_broju=(), po_imenu=()):
        self.po_broju, self.po_imenu = list(po_broju), list(po_imenu)

    def execute(self, upit, parametri=None, *a, **k):
        tekst = str(upit)
        if "MATBROJ,''))=" in tekst or "ZIRORACUN,''))=" in tekst:
            return _Rez(self.po_broju)
        if "NAZIV1 LIKE" in tekst:
            return _Rez(self.po_imenu)
        return _Rez([])


def test_isti_maticni_broj_je_dokaz_a_ne_slicnost():
    nadjeni = slicni_komitenti(
        _ConnSifarnik(po_broju=[_red(a="00709", nz="Komunalac JKP", se="Kula",
                                     zr="100662387", mb="08004790")]),
        naziv="JAVNO KOMUNALNO PREDUZEĆE KOMUNALAC, KULA", mesto="Kula",
        pib="100662387", maticni="08004790",
    )

    assert nadjeni[0]["analitika"] == "00709"
    assert nadjeni[0]["po_broju"] is True


def test_jmbg_fizickog_lica_se_ne_poredi_sa_pibom():
    """ZIRORACUN kod fizičkih lica drži JMBG od 13 cifara, ne PIB od devet."""
    conn = _ConnSifarnik(po_broju=[_red(a="99998", nz="Banjac Vjera", se="Kula",
                                        zr="0108953815031", mb="")])

    nadjeni = slicni_komitenti(conn, naziv="Nešto drugo doo", mesto="Kula",
                               pib="0108953815031", maticni="")

    assert nadjeni == [], "trinaestocifren broj nije PIB i ne sme da uparuje"


def test_slicno_ime_upozorava_ali_nije_dokaz():
    nadjeni = slicni_komitenti(
        _ConnSifarnik(po_imenu=[_red(a="00142", nz="Eurotehna 021 doo", se="Novi Sad",
                                     zr="101634740", mb="")]),
        naziv="EUROTEHNA 021 DOO NOVI SAD", mesto="Novi Sad",
    )

    assert nadjeni[0]["po_broju"] is False
    assert nadjeni[0]["kako"] == "slično ime"


# --------------------------------------------------------------------------- #
# upis
# --------------------------------------------------------------------------- #


class _ErpZaUpis:
    def __init__(self, zauzeta=None):
        self.zauzeta, self.upisano = zauzeta, []

    def begin(self):
        return self

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, upit, parametri=None, *a, **k):
        tekst = str(upit)
        if tekst.strip().upper().startswith("INSERT"):
            self.upisano.append(parametri)
            return _Rez([])
        if "DB_NAME" in tekst:
            return _Rez([("ELBSX_2026",)])
        return _Rez([_red(n=self.zauzeta)] if self.zauzeta else [])

    def scalar(self):
        return "ELBSX_2026"


def test_upis_odbija_sifru_koju_je_neko_u_medjuvremenu_uzeo(monkeypatch):
    """Između pravljenja predloga i klika može proći pola sata."""
    from sefsync.erp import komitent as k

    erp = _ErpZaUpis(zauzeta="Neko drugi doo")
    monkeypatch.setattr(k, "erp_engine_za_upis", lambda produkcija=False: (erp, "ELBSX_2026"))

    with pytest.raises(ValueError, match="u međuvremenu zauzeta"):
        upisi(PredlogKomitenta(document_id=1, analitika="00042", naziv="Bce group doo"))


def test_upis_puni_polja_koja_sifarnik_ocekuje(monkeypatch):
    from sefsync.erp import komitent as k

    erp = _ErpZaUpis()
    monkeypatch.setattr(k, "erp_engine_za_upis", lambda produkcija=False: (erp, "ELBSX_2026"))

    upisi(PredlogKomitenta(
        document_id=1, analitika="00042", naziv="Bce group doo",
        pun_naziv="BCE GROUP DOO ČAČAK", mesto="Čačak", pib="111222333",
        maticni="21587516",
    ))

    red = erp.upisano[0]
    assert red["ANALITIKA"] == "00042"
    assert red["NAZIV1"] == "Bce group doo"
    assert red["APRNAZIV"] == "BCE GROUP DOO ČAČAK"   # pun naziv ostaje zapisan
    assert red["ZIRORACUN"] == "111222333"            # PIB, uprkos imenu kolone
    assert red["SH_KOM"] == "."                       # popuna, kao i svuda
