"""Nacrt kalkulacije i tab na kom se otvaraju sifre artikala."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sefsync.models import (
    BusinessUnit,
    Document,
    DocumentLine,
    ItemMapping,
    NacrtKalkulacije,
    SefStatus,
    StanjeNacrta,
    UnitKind,
)
from sefsync.web.auth import Role, User

OPERATER = User("bogdan", "Sunajko Bogdan", Role.OPERATER, None, "FINANSIJE")


class LazniNacrt:
    """Ono sto generator vrati, bez ERP-a."""

    def __init__(self, problemi=(), nemapirane=()):
        self.magacin = "002"
        self.tabela_zaglavlja = "MKALKUL"
        self.zaglavlje = {"IZNOS_ZA_NAP": 194666.40}
        self.problemi = list(problemi)
        self.nemapirane = list(nemapirane)

    @property
    def spreman(self) -> bool:
        return not self.problemi and not self.nemapirane


STAVKA_BEZ_SIFRE = {
    "line_no": 1,
    "naziv": "Frižider",
    "sifra_dobavljaca": "EW-9911",
    "ean": "8606012345678",
    "kolicina": 2.0,
    "cena": 50000.0,
    "jedinica": "H87",
}


@pytest.fixture
def podaci(db):
    with db.session_scope() as session:
        unit = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        session.add(unit)
        session.flush()
        doc = Document(
            sef_invoice_id=500100, document_number="26-30F-087050",
            supplier_name="EWE COMP DOO", supplier_vat="100042618",
            amount=194666.40, sef_status=SefStatus.SEEN, business_unit_id=unit.id,
        )
        doc.lines.append(
            DocumentLine(line_no=1, name="Frižider", sellers_item_id="EW-9911",
                         standard_item_id="8606012345678", quantity=2.0, price=50000.0)
        )
        session.add(doc)
        session.flush()
        return doc.id


@pytest.fixture
def client(db) -> TestClient:
    import sefsync.web.app as app_module

    app_module.app.dependency_overrides[app_module.current_user] = lambda: OPERATER
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def test_nacrt_koji_ceka_sifru_se_pamti(db, podaci, monkeypatch):
    from sefsync.services import nacrti

    monkeypatch.setattr(
        nacrti, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )

    red = nacrti.osvezi(podaci)

    assert red.stanje is StanjeNacrta.CEKA_SIFRU
    assert red.nemapirane[0]["ean"] == "8606012345678"
    with db.session_scope() as session:
        assert session.get(NacrtKalkulacije, podaci).stanje is StanjeNacrta.CEKA_SIFRU


def test_upisan_nacrt_se_ne_vraca_unazad(db, podaci, monkeypatch):
    """Kad je kalkulacija vec u ERP-u, ponovno racunanje ne sme da je proglasi nespremnom."""
    from sefsync.services import nacrti

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    nacrti.osvezi(podaci)
    with db.session_scope() as session:
        session.get(NacrtKalkulacije, podaci).stanje = StanjeNacrta.UPISAN

    monkeypatch.setattr(
        nacrti, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )
    nacrti.osvezi(podaci)

    with db.session_scope() as session:
        assert session.get(NacrtKalkulacije, podaci).stanje is StanjeNacrta.UPISAN


def test_povezana_sifra_vazi_i_za_buduce_fakture(db, podaci, monkeypatch):
    """Unos nije ispravka jednog dokumenta nego pravilo za tog dobavljaca."""
    from sefsync.services import nacrti

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())

    nacrti.povezi_sifru(podaci, 1, " 12345 ", korisnik="dusan")

    with db.session_scope() as session:
        veza = session.scalar(select(ItemMapping))
        assert (veza.supplier_vat, veza.supplier_item_id) == ("100042618", "EW-9911")
        assert veza.erp_item_code == "12345"
        assert veza.confirmed_by == "dusan"
        linija = session.scalar(select(DocumentLine))
        assert linija.erp_item_code == "12345"
        assert session.get(NacrtKalkulacije, podaci).stanje is StanjeNacrta.SPREMAN


def test_tab_prikazuje_sta_fali(client, db, podaci, monkeypatch):
    from sefsync.services import nacrti

    monkeypatch.setattr(
        nacrti, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )
    nacrti.osvezi(podaci)

    odgovor = client.get("/sifre")

    assert odgovor.status_code == 200
    assert "8606012345678" in odgovor.text      # EAN, da Dule ima po cemu da trazi
    assert "EW-9911" in odgovor.text            # sifra dobavljaca
    assert "26-30F-087050" in odgovor.text      # broj fakture


def test_prazan_unos_sifre_ne_prolazi(db, podaci, monkeypatch):
    from sefsync.services import nacrti

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())

    with pytest.raises(ValueError):
        nacrti.povezi_sifru(podaci, 1, "   ", korisnik="dusan")


# --------------------------------------------------------------------------- #
# predlog nove sifre artikla
# --------------------------------------------------------------------------- #


class LazniErp:
    """ERP koji vrati spisak magacina i prodajnih mesta, bez prave baze."""

    def __init__(self, barkod_postoji: str | None = None):
        self.barkod_postoji = barkod_postoji

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, upit, params=None):
        tekst = str(upit)
        if "BARCODE" in tekst:
            return _Rezultat(
                [_Red(a="55555", n="Isti artikal")] if self.barkod_postoji else []
            )
        if "ARTPROD" in tekst:
            return [_Red(m="001"), _Red(m="002")]
        return [_Red(m="001"), _Red(m="099")]


class _Red:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Rezultat:
    def __init__(self, redovi):
        self._redovi = redovi

    def first(self):
        return self._redovi[0] if self._redovi else None


def test_predlog_uzima_sve_sto_faktura_zna(db, podaci):
    from sefsync.erp.artikal import predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp())

    assert predlog.naziv == "Frižider"
    assert predlog.barkod == "8606012345678"
    assert predlog.jedmere == "kom"          # H87 sa fakture
    assert predlog.tarifa == "002"           # PDV 20% -> tarifa 002
    assert predlog.magacini == ["001", "099"]
    assert predlog.broj_redova == 2 + 2 + 1  # ARTIKLI + ARTPROD + DARTIKLI


def test_poznat_barkod_ne_otvara_novu_sifru(db, podaci):
    """Ako barkod vec ima sifru, greska bi bila otvoriti drugu za isti artikal."""
    from sefsync.erp.artikal import predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp(barkod_postoji="55555"))

    assert predlog.sifra == "55555"
    assert any("već ima šifru" in n for n in predlog.napomene)


def test_bez_sifre_nema_sql(db, podaci):
    """App ne dodeljuje sifru sama - dok je nema, nema sta da se upise."""
    import pytest as _pytest

    from sefsync.erp.artikal import kao_sql, predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp())
    with _pytest.raises(ValueError):
        kao_sql(predlog)


def test_sql_nosi_izmerene_stalne_vrednosti(db, podaci):
    from sefsync.erp.artikal import kao_sql, predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp())
    predlog.sifra = "86100"
    sql = kao_sql(predlog)

    assert sql.count("INSERT INTO dbo.ARTIKLI") == 2
    assert sql.count("INSERT INTO dbo.ARTPROD") == 2
    assert sql.count("INSERT INTO dbo.DARTIKLI") == 1
    assert "'86100'" in sql
    assert "DECKOLIC" in sql and "PROIZVODJAC" in sql
