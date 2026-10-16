"""Specifikacija artikla (DEKA) pri otvaranju sifre - Bane, 15.10."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from sefsync.services import specifikacija as S

# Pravi zapis iz ELBS_2026 (93827 Gorenje WPNEI 94A1SWIFI), skracen.
ERP_RTF = (
    "{\\rtf1\\ansi\\ansicpg1250\\deff0\\deflang9242{\\fonttbl{\\f0\\fnil\\fcharset238 Arial Narrow;}}\r\n"
    "{\\colortbl ;\\red8\\green0\\blue0;}\r\n"
    "{\\*\\generator Msftedit 5.41.21.2510;}\\viewkind4\\uc1\\pard\\cf1\\highlight0\\f0\\fs17 "
    "Energetski razred: A\\par\r\n"
    "Motor: Inverter PowerDrive motor bez \\'e8etkica\\par\r\n"
    "Dimenzije (\\'8axVxD): 600 x 850 x 545 mm\\par\r\n"
    "}\r\n"
)
# Drugi editor (Riched20) mesa fontove usred reci.
ERP_RTF_RICHED = (
    "{\\rtf1\\ansi\\ansicpg1252\\deff0\\nouicompat\\deflang1033{\\fonttbl{\\f0\\fnil\\fcharset0 Arial Narrow;}"
    "{\\f1\\fnil\\fcharset238 Arial Narrow;}}\r\n{\\colortbl ;\\red8\\green0\\blue0;}\r\n"
    "{\\*\\generator Riched20 10.0.19041}\\viewkind4\\uc1 \r\n"
    "\\pard\\cf1\\highlight0\\f0\\fs17 Na\\f1\\'e8\\f0 in punjenja: Frontalno\\par\r\nWi-Fi: Da\\tab\\par\r\n}\r\n"
)


def test_rtf_iz_erpa_postaje_citljiv_tekst():
    assert S.rtf_u_tekst(ERP_RTF) == (
        "Energetski razred: A\n"
        "Motor: Inverter PowerDrive motor bez četkica\n"
        "Dimenzije (ŠxVxD): 600 x 850 x 545 mm"
    )
    assert S.rtf_u_tekst(ERP_RTF_RICHED) == "Način punjenja: Frontalno\nWi-Fi: Da"
    assert S.rtf_u_tekst("") == ""
    assert S.rtf_u_tekst(None) == ""


def test_nas_rtf_je_u_obliku_erpa_i_vraca_isti_tekst():
    """Provereno i na svih 6.929 zapisa u DEKA (15.10.): povratak bez razlike."""
    tekst = "Snaga: 2000 W\nBoja: Crna/Siva\nŠirina: 60 cm\nĐubrovnik {test} \\ kraj\nНапон: 230 V"
    rtf = S.tekst_u_rtf(tekst)
    assert rtf.startswith("{\\rtf1\\ansi\\ansicpg1250")
    assert "Arial Narrow" in rtf and "\\fs17" in rtf
    assert "\\'8airina" in rtf          # Š kao u ERP-u, cp1250
    assert "\\u1053?" in rtf             # cirilica van cp1250 - RTF unicode
    assert S.rtf_u_tekst(rtf) == tekst


def test_prazni_redovi_i_razmaci_se_ciste():
    assert S.rtf_u_tekst(S.tekst_u_rtf("  Snaga: 1 W \n\n\nBoja: bela  ")) == "Snaga: 1 W\nBoja: bela"
    assert S.tekst_u_rtf("   \n  ") == ""


def test_predugacka_specifikacija_se_odbija():
    S.proveri_duzinu("Snaga: 1 W")
    with pytest.raises(ValueError, match="predugačka"):
        S.proveri_duzinu("Č" * 1200)    # svako Č je 4 znaka u RTF-u


def _predlog(**kw):
    from sefsync.erp.artikal import PredlogArtikla

    p = PredlogArtikla(document_id=1, line_no=1, predlozena_sifra="14460", naziv="Gorenje X",
                       barkod="3838782000001", cena_mp=49990.0, magacini=["099", "001"],
                       prodajna_mesta=["001"])
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_specifikacija_ide_u_deka_u_istom_upisu():
    from sefsync.erp.artikal import kao_sql, redovi

    tabele = redovi(_predlog(specifikacija="Snaga: 2000 W\nBoja: bela"))
    (red,) = tabele["DEKA"]
    assert red["ARTIKAL"] == "14460"
    assert red["SELEKTOVAN"] == 0
    assert S.rtf_u_tekst(red["TEKSTDEK"]) == "Snaga: 2000 W\nBoja: bela"
    assert "INSERT INTO dbo.DEKA" in kao_sql(_predlog(specifikacija="Snaga: 2000 W"))


def test_bez_specifikacije_nema_reda_u_deka():
    from sefsync.erp.artikal import redovi

    assert "DEKA" not in redovi(_predlog(specifikacija="  \n "))
    assert "DEKA" not in redovi(_predlog())


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #


class _Lazni:
    """Lazni klijent: vraca unapred zadate odgovore i pamti sta je poslato."""

    def __init__(self, *odgovori):
        self.odgovori = list(odgovori)
        self.poslato: list[dict] = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.poslato.append(kw)
        return self.odgovori.pop(0)


def _odg(stop="end_turn", *blokovi):
    return NS(stop_reason=stop, content=list(blokovi))


def _tekst(t):
    return NS(type="text", text=t)


def _pretraga(*urlovi):
    return NS(type="web_search_tool_result",
              content=[NS(type="web_search_result", url=u, title=f"naslov {i}")
                       for i, u in enumerate(urlovi)])


def test_claude_predlog_vadi_specifikaciju_i_izvore():
    klijent = _Lazni(_odg(
        "end_turn",
        NS(type="server_tool_use", name="web_search"),
        _pretraga("https://gorenje.rs/x", "https://gorenje.rs/x", "https://shop.rs/y"),
        _tekst("Našao sam model.\n<specifikacija>\n- Snaga: 2000 W\nBoja: bela\n</specifikacija>"),
    ))
    p = S.predlozi_claude("Gorenje X", ean="3838", kategorija="Bela tehnika / Šporeti",
                          primeri=["Snaga: 1 W"], klijent=klijent)
    assert p.tekst == "Snaga: 2000 W\nBoja: bela"
    assert [i["url"] for i in p.izvori] == ["https://gorenje.rs/x", "https://shop.rs/y"]

    zahtev = klijent.poslato[0]
    assert zahtev["model"] == "claude-opus-5-5"
    assert zahtev["tools"][0]["type"] == "web_search_20260209"
    assert zahtev["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in zahtev["betas"]
    upit = zahtev["messages"][0]["content"]
    assert "3838" in upit and "Šporeti" in upit and "<primer1>" in upit


def test_claude_nastavlja_posle_pauze_pretrage():
    klijent = _Lazni(
        _odg("pause_turn", NS(type="server_tool_use", name="web_search")),
        _odg("end_turn", _tekst("<specifikacija>Snaga: 5 W</specifikacija>")),
    )
    p = S.predlozi_claude("X", klijent=klijent)
    assert p.tekst == "Snaga: 5 W"
    drugi = klijent.poslato[1]["messages"]
    assert [m["role"] for m in drugi] == ["user", "assistant"]   # bez dodatnog „nastavi"


def test_claude_ne_nadje_model_pa_vrati_razlog_umesto_teksta():
    klijent = _Lazni(_odg("end_turn", _tekst(
        "<specifikacija></specifikacija>\nNa webu nema modela sa tim EAN-om.")))
    p = S.predlozi_claude("Nepoznato 123", klijent=klijent)
    assert p.tekst == ""
    assert "nema modela" in p.poruka


def test_claude_odbijanje_ne_daje_tekst():
    p = S.predlozi_claude("X", klijent=_Lazni(_odg("refusal", _tekst("ne"))))
    assert p.tekst == "" and "odbio" in p.poruka


def test_bez_kljuca_nema_poziva(monkeypatch):
    from sefsync.config import get_settings

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        S.predlozi_claude("X")


# --------------------------------------------------------------------------- #
# panel
# --------------------------------------------------------------------------- #


def test_otvaranje_sifre_prenosi_specifikaciju(db, monkeypatch):
    from fastapi.testclient import TestClient

    import sefsync.web.app as app_module
    from sefsync.services import nacrti
    from sefsync.web.auth import Role, User

    app_module.app.dependency_overrides[app_module.current_user] = lambda: User(
        "dule", "Selak Dusan", Role.OPERATER, None, "FINANSIJE")
    primljeno = {}
    monkeypatch.setattr(nacrti, "otvori_sifru",
                        lambda *a, **k: primljeno.update(k) or "Otvorena šifra 14460.")
    try:
        odgovor = TestClient(app_module.app).post("/sifre/otvori", data={
            "document_id": 1, "line_no": 1, "naziv": "Gorenje X", "cena_mp": 100,
            "garancija": 24, "produzena_garancija": 0,
            "specifikacija": "Snaga: 2000 W\nBoja: bela",
        }, follow_redirects=False)
    finally:
        app_module.app.dependency_overrides.clear()
    assert odgovor.status_code == 303
    assert primljeno["specifikacija"] == "Snaga: 2000 W\nBoja: bela"


def test_dugme_predlaze_specifikaciju_kao_json(db, monkeypatch):
    from fastapi.testclient import TestClient

    import sefsync.web.app as app_module
    from sefsync.models import AuditLog, Document, DocumentLine
    from sefsync.web.auth import Role, User

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=880001, document_number="R-1", supplier_name="EWE")
        doc.lines.append(DocumentLine(line_no=1, name="Gorenje WPNEI 94A1SWIFI masina za ves",
                                      standard_item_id="3838782123456", quantity=1))
        session.add(doc)
        session.flush()
        doc_id = doc.id

    pozvano = {}

    def lazni(naziv, **kw):
        pozvano.update(kw, naziv=naziv)
        return S.PredlogSpecifikacije(tekst="Kapacitet pranja: 9 kg",
                                      izvori=[{"naslov": "g", "url": "https://g.rs"}])

    monkeypatch.setattr(S, "predlozi_claude", lazni)
    monkeypatch.setattr("sefsync.erp.connection.get_erp_engine",
                        lambda: (_ for _ in ()).throw(RuntimeError("nema ERP-a")))
    app_module.app.dependency_overrides[app_module.current_user] = lambda: User(
        "dule", "Selak Dusan", Role.OPERATER, None, "FINANSIJE")
    try:
        odgovor = TestClient(app_module.app).post("/sifre/specifikacija", data={
            "document_id": doc_id, "line_no": 1, "naziv": "Gorenje WPNEI 94A1", "shkat": "511"})
    finally:
        app_module.app.dependency_overrides.clear()

    assert odgovor.json() == {"ok": True, "tekst": "Kapacitet pranja: 9 kg",
                              "izvori": [{"naslov": "g", "url": "https://g.rs"}], "poruka": ""}
    # pun naziv sa fakture je bolji za pretragu od skracenog na 30 znakova
    assert pozvano["naziv"].startswith("Gorenje WPNEI 94A1SWIFI masina")
    assert pozvano["ean"] == "3838782123456"
    with db.session_scope() as session:
        assert session.query(AuditLog).filter_by(action="artikal.specifikacija.predlog").count() == 1


def test_slicni_artikli_ne_salju_drugi_upit_dok_prvi_cita():
    """Bez MARS-a SQL Server odbija drugi upit dok prvi vraca redove.

    Strana „Nova šifra“ je zato pucala sa „Connection is busy“ kad god je
    postojao sličan artikal (server 05.10.). Lažna veza ovde puca isto.
    """
    from sefsync.erp.artikal import slicni_artikli

    class Rezultat:
        def __init__(self, veza, redovi):
            self.veza, self.redovi = veza, redovi

        def all(self):
            self.veza.zauzeta = False
            return list(self.redovi)

        def first(self):
            self.veza.zauzeta = False
            return self.redovi[0] if self.redovi else None

        def __iter__(self):
            for r in self.redovi:
                yield r
            self.veza.zauzeta = False

    class Veza:
        zauzeta = False

        def execute(self, upit, parametri=None):
            if self.zauzeta:
                raise RuntimeError("Connection is busy with results for another command")
            self.zauzeta = True
            if "FROM dbo.ARTIKLI" in str(upit):
                return Rezultat(self, [NS(a="14455", n="Realme C100 5G 128GB BlPurple", b="",
                                          k="862", p="", g=24, pg=0)])
            return Rezultat(self, [NS(mp=19990.0, nab=14000.0)])

    (s,) = slicni_artikli(Veza(), "Realme C100 5G 128GB Black")
    assert s["artikal"] == "14455"
    assert s["cena_mp"] == 19990.0


def test_kljuc_bez_radnog_prostora_salje_id_radnog_prostora(monkeypatch):
    """Ključ sa servera (16.10.) API prima samo uz anthropic-workspace-id."""
    from sefsync.config import get_settings

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-ant-usr-x")
    monkeypatch.setattr(get_settings(), "anthropic_workspace_id", " wrkspc_123 ")
    assert S._klijent().default_headers["anthropic-workspace-id"] == "wrkspc_123"
    monkeypatch.setattr(get_settings(), "anthropic_workspace_id", "")
    assert "anthropic-workspace-id" not in S._klijent().default_headers
