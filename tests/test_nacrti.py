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

    def __init__(self, problemi=(), nemapirane=(), napomene=()):
        self.magacin = "002"
        self.tabela_zaglavlja = "MKALKUL"
        self.zaglavlje = {"IZNOS_ZA_NAP": 194666.40}
        self.problemi = list(problemi)
        self.nemapirane = list(nemapirane)
        self.napomene = list(napomene)

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
    """Mali sifarnik umesto prave ERP baze.

    `zauzete` su brojcane sifre koje vec postoje - iz njih se racunaju rupe, pa
    se vidi i da li predlog ume da nadje prvu slobodnu.
    """

    def __init__(self, zauzete=(1, 2, 3, 500), barkod_postoji: str | None = None):
        self.zauzete = {str(x) for x in zauzete}
        self.barkod_postoji = barkod_postoji

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, upit, params=None):
        tekst = str(upit)
        if "TOP 1 1" in tekst:                      # provera zauzetosti jedne sifre
            sifra = (params or {}).get("s", "")
            return _Rezultat([_Red(x=1)] if sifra in self.zauzete else [])
        if "DISTINCT TRY_CAST" in tekst:            # spisak zauzetih
            return [_Red(v=int(x)) for x in self.zauzete]
        if "BARCODE" in tekst:
            return _Rezultat(
                [_Red(a="55555", n="Isti artikal")] if self.barkod_postoji else []
            )
        if "PRODAV" in tekst:                       # prodajna mesta bez usluznih
            return [_Red(s="001"), _Red(s="002")]
        if "NAZIVI" in tekst:                       # analitika dobavljaca
            return _Rezultat([])
        if "MPRULK" in tekst or "ARTPROD" in tekst:  # istorija marze / cene
            return _Rezultat([])
        return _Rezultat([_Red(m="001"), _Red(m="099")])


class _Red:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Rezultat:
    def __init__(self, redovi):
        self._redovi = redovi

    def first(self):
        return self._redovi[0] if self._redovi else None

    def __iter__(self):
        return iter(self._redovi)


def test_predlog_uzima_sve_sto_faktura_zna(db, podaci):
    from sefsync.erp.artikal import predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp())

    assert predlog.naziv == "Frižider"
    assert predlog.barkod == "8606012345678"
    assert predlog.jedmere == "kom"          # H87 sa fakture
    assert predlog.tarifa == "002"           # PDV 20% -> tarifa 002
    # magacini su imenovani spisak, prodajna mesta se citaju iz ERP-a
    from sefsync.erp.artikal import MAGACINI

    assert predlog.magacini == list(MAGACINI)
    assert predlog.prodajna_mesta == ["001", "002"]
    assert predlog.broj_redova == len(MAGACINI) + 2


def test_poznat_barkod_ne_otvara_novu_sifru(db, podaci):
    """Ako barkod vec ima sifru, greska bi bila otvoriti drugu za isti artikal."""
    from sefsync.erp.artikal import predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp(barkod_postoji="55555"))

    assert predlog.sifra == "55555"
    assert any("već ima šifru" in n for n in predlog.napomene)


def test_predlaze_prvu_slobodnu_iz_dovoljno_velike_rupe(db, podaci):
    """Prag je 500 mesta: rupa 4-999 je dovoljna, pa predlog krece od 4."""
    from sefsync.erp.artikal import predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp(zauzete=(1, 2, 3, 1000)))

    assert predlog.predlozena_sifra == "4"
    assert predlog.sifra is None


def test_mala_rupa_se_preskace(db, podaci):
    """Rupa ispod praga se ne dira - nove sifre idu u blok, ne medju stare."""
    from sefsync.erp.artikal import predlozi

    # 2 je slobodno (rupa od 1), 4-9 takodje (rupa od 6) - obe su ispod praga od
    # 500; prvi dovoljno veliki blok pocinje na 11
    predlog = predlozi(podaci, 1, engine=LazniErp(zauzete=(1, 3, 10, 5000)))

    assert predlog.predlozena_sifra == "11"


def test_zauzeta_sifra_se_ne_predlaze(db, podaci):
    """Spisak je snimljen ranije, pa se pred dodelu pita jos jednom."""
    from sefsync.erp.artikal import slobodna_sifra

    erp = LazniErp(zauzete=(1, 3, 10, 5000))
    erp.zauzete.add("11")          # neko ju je otvorio u medjuvremenu

    with erp.connect() as conn:
        assert slobodna_sifra(conn) == "12"


def test_sql_nosi_izmerene_stalne_vrednosti(db, podaci):
    from sefsync.erp.artikal import kao_sql, predlozi

    predlog = predlozi(podaci, 1, engine=LazniErp())
    predlog.predlozena_sifra = "86100"
    predlog.cena_mp = 59_900.0          # cena je uslov, ne ukras
    sql = kao_sql(predlog)

    from sefsync.erp.artikal import MAGACINI

    assert sql.count("INSERT INTO dbo.ARTIKLI") == len(MAGACINI)
    assert sql.count("INSERT INTO dbo.ARTPROD") == 2
    # DARTIKLI puni nocni posao na serveru - mi je ne diramo (03.10.)
    assert "DARTIKLI" not in sql
    assert "'86100'" in sql
    assert "DECKOLIC" in sql and "PROIZVODJAC" in sql


def test_zaustavljeni_nacrti_se_vide_na_istom_tabu(client, db, podaci, monkeypatch):
    """Nacrt zaustavljen necim drugim (dobavljac van sifarnika) ne sme da nestane."""
    from sefsync.services import nacrti

    monkeypatch.setattr(
        nacrti,
        "pripremi",
        lambda *a, **k: LazniNacrt(problemi=["Dobavljač EWE COMP (MB 123) ne postoji u NAZIVI."]),
    )
    nacrti.osvezi(podaci)

    odgovor = client.get("/sifre")

    assert "ne postoji u NAZIVI" in odgovor.text


def test_nacrt_nastaje_cim_operater_razvrsta_dokument(db, podaci, monkeypatch):
    """Faktura koja stigne posle poslednjeg racunanja mora sama da se pojavi.

    17.09. je faktura FA-2647-001/26 stigla 47 minuta posle `nacrti --osvezi`, sa
    pet stavki bez nase sifre - i nije se videla nigde, jer nacrte je do tada
    racunala samo ta komanda.
    """
    from sefsync.models import BusinessUnit, NacrtKalkulacije, StanjeNacrta, UnitKind
    from sefsync.services import nacrti as nacrti_modul
    from sefsync.services.workflow import Workflow

    monkeypatch.setattr(
        nacrti_modul, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )
    with db.session_scope() as session:
        druga = BusinessUnit(code="MP005", name="SOMBOR", kind=UnitKind.RETAIL, erp_code="005")
        session.add(druga)
        session.flush()
        doc = session.get(Document, podaci)
        doc.business_unit_id = None
        nova_pj = druga.id

    Workflow(notifier=None).assign_unit(podaci, nova_pj, actor="nina", remember=False)

    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci)
        assert red is not None, "razvrstavanje mora da napravi nacrt"
        assert red.stanje is StanjeNacrta.CEKA_SIFRU


# --------------------------------------------------------------------------- #
# isti artikal na vise objekata
# --------------------------------------------------------------------------- #


def test_otvorena_sifra_resava_i_fakture_drugih_objekata(db, podaci, monkeypatch):
    """Dusan kaze sifru jednom, a ceka je vise objekata sa istim artiklom.

    Bez ovoga bi poslovodja u drugom objektu i dalje gledao "ceka sifru" za nesto
    sto je vec reseno, dok neko rucno ne preracuna nacrte.
    """
    from sefsync.models import Document, DocumentLine, NacrtKalkulacije, SefStatus, StanjeNacrta
    from sefsync.services import nacrti

    with db.session_scope() as session:
        prvi = session.get(Document, podaci)
        drugi = Document(
            sef_invoice_id=500999, document_number="FA-2650/26",
            supplier_name="EWE COMP DOO", supplier_vat="100042618",
            amount=50000.0, sef_status=SefStatus.SEEN,
            business_unit_id=prvi.business_unit_id,
        )
        drugi.lines.append(
            DocumentLine(line_no=1, name="Frižider", sellers_item_id="EW-9911",
                         standard_item_id="8606012345678", quantity=1.0, price=50000.0)
        )
        session.add(drugi)
        session.flush()
        drugi_id = drugi.id

    monkeypatch.setattr(
        nacrti, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )
    nacrti.osvezi(podaci)
    nacrti.osvezi(drugi_id)

    # od trenutka kad sifra postoji, generator je nalazi za obe fakture
    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    nacrti.povezi_sifru(podaci, 1, "12345", korisnik="dusan")

    with db.session_scope() as session:
        assert session.get(NacrtKalkulacije, drugi_id).stanje is StanjeNacrta.SPREMAN


def test_slika_jednog_objekta_vidi_se_na_fakturi_drugog(db, podaci):
    """Slika je o artiklu, ne o fakturi - drugi poslovodja ne treba da slika ponovo."""
    from sefsync.services.slike import sacuvaj, za_artikal

    MALI_PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000100ffff03000006"
        "000557bfabd40000000049454e44ae426082"
    )
    sacuvaj(podaci, MALI_PNG, mime="image/png", naziv_fajla="a.png",
            korisnik="predrag", line_no=1)

    with db.session_scope() as session:
        nadjene = za_artikal(
            session, ean="8606012345678", sifra_dobavljaca=None, supplier_vat=None
        )
    assert len(nadjene) == 1
    assert nadjene[0].poslao == "predrag"
    assert nadjene[0].naziv_artikla == "Frižider"


def test_stavka_koja_nije_artikal_izlazi_sa_spiska(db, podaci, monkeypatch):
    """Eurotehna na odobrenju pise "Odobrenje po Saglasnosti broj ..." - to nije artikal.

    Bez ovoga takva stavka zauvek stoji na Dusanovom spisku i zatrpava ga.
    """
    from sefsync.models import DocumentLine, NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti

    monkeypatch.setattr(
        nacrti, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )
    nacrti.osvezi(podaci)

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    nacrti.oznaci_nije_artikal(podaci, 1, korisnik="dusan")

    with db.session_scope() as session:
        linija = session.scalar(select(DocumentLine))
        assert linija.nije_artikal is True
        assert linija.nije_artikal_by == "dusan"
        assert session.get(NacrtKalkulacije, podaci).stanje is StanjeNacrta.SPREMAN


def test_oznaka_se_moze_vratiti(db, podaci, monkeypatch):
    from sefsync.models import DocumentLine
    from sefsync.services import nacrti

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    nacrti.oznaci_nije_artikal(podaci, 1, korisnik="dusan")
    nacrti.oznaci_nije_artikal(podaci, 1, korisnik="dusan", vrati=True)

    with db.session_scope() as session:
        linija = session.scalar(select(DocumentLine))
        assert linija.nije_artikal is False
        assert linija.nije_artikal_by is None


def test_dusan_vidi_sliku_sa_tudje_fakture(client, db, podaci, monkeypatch):
    """Slikao poslovodja u jednom objektu, a Dusan gleda fakturu drugog objekta.

    Dok su se slike trazile po dokumentu, kod njega nije pisalo nista - pa bi
    trazio sliku koja vec postoji.
    """
    from sefsync.models import Document, DocumentLine, SefStatus
    from sefsync.services import nacrti
    from sefsync.services.slike import sacuvaj

    MALI_PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000100ffff03000006"
        "000557bfabd40000000049454e44ae426082"
    )
    # slika stize sa PRVE fakture
    sacuvaj(podaci, MALI_PNG, mime="image/png", naziv_fajla="a.png",
            korisnik="so1", line_no=1)

    # a Dusan gleda DRUGU fakturu sa istim artiklom (isti EAN)
    with db.session_scope() as session:
        prvi = session.get(Document, podaci)
        drugi = Document(
            sef_invoice_id=500555, document_number="FA-9/26",
            supplier_name="EWE COMP DOO", supplier_vat="100042618",
            amount=1.0, sef_status=SefStatus.SEEN, business_unit_id=prvi.business_unit_id,
        )
        drugi.lines.append(
            DocumentLine(line_no=1, name="Frižider", sellers_item_id="EW-9911",
                         standard_item_id="8606012345678", quantity=1.0, price=1.0)
        )
        session.add(drugi)
        session.flush()
        drugi_id = drugi.id

    monkeypatch.setattr(
        nacrti, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )
    nacrti.osvezi(drugi_id)

    odgovor = client.get("/sifre")

    assert "/slika/" in odgovor.text        # slika sa tudje fakture je tu
    assert "so1" in odgovor.text            # i ko ju je poslao


# --------------------------------------------------------------------------- #
# upis kalkulacije u ERP na potvrdu prijema
# --------------------------------------------------------------------------- #


class SpremanNacrt(LazniNacrt):
    def __init__(self):
        super().__init__()


def test_potvrda_prijema_upisuje_kalkulaciju(db, podaci, monkeypatch):
    """Upis ide tek kad poslovodja kaze da je roba stigla - ranije bi ga pretekao."""
    from sefsync.models import NacrtKalkulacije, StanjeNacrta, utcnow
    from sefsync.services import nacrti
    from sefsync.services.workflow import Workflow

    upisano: list = []

    def lazni_upis(nacrt, produkcija=False):
        # pravi `upisi` dodeli broj i upise ga u nacrt - test to mora da oponasa
        nacrt.zaglavlje["BROJ"] = "00450"
        upisano.append(produkcija)
        return "ELBSX_2026: MKALKUL 002/U/00450, stavki 3"

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    monkeypatch.setattr(nacrti, "upisi", lazni_upis)
    with db.session_scope() as session:
        doc = session.get(Document, podaci)
        doc.forwarded_at = utcnow()
        doc.forwarded_by = "nina"
    nacrti.osvezi(podaci)

    radnik = Workflow(notifier=type("N", (), {"notify_receipt": lambda *a, **k: True})())
    monkeypatch.setattr(radnik.settings, "erp_upis_kalkulacije", True)
    ishod = radnik.confirm_receipt(podaci, actor="predrag")

    assert ishod.ok
    assert "upisana u ERP" in ishod.message
    assert upisano == [False], "podrazumevano ide u test bazu"
    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci)
        assert red.stanje is StanjeNacrta.UPISAN
        assert red.upisao == "predrag"
        assert red.erp_broj == "002/00450"


def test_kalkulacija_koja_vec_postoji_se_ne_pravi_drugi_put(db, podaci, monkeypatch):
    """Poslovodje i dalje rade rucno - dve kalkulacije za istu fakturu su gore od nijedne."""
    from sefsync.erp.kalkulacija import VecPostoji
    from sefsync.models import NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti

    def pukni(nacrt, produkcija=False):
        raise VecPostoji("Kalkulacija za taj račun već postoji u ERP-u: MKALKUL 002/00449")

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    monkeypatch.setattr(nacrti, "upisi", pukni)

    upisana, poruka = nacrti.upisi_u_erp(podaci, korisnik="predrag")

    assert upisana is False
    assert "već postoji" in poruka
    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci)
        assert red.stanje is StanjeNacrta.UPISAN
        assert red.upisao == "ručno u ERP-u"


def test_nespremna_kalkulacija_se_ne_upisuje(db, podaci, monkeypatch):
    from sefsync.services import nacrti

    monkeypatch.setattr(
        nacrti, "pripremi", lambda *a, **k: LazniNacrt(nemapirane=[STAVKA_BEZ_SIFRE])
    )
    monkeypatch.setattr(
        nacrti, "upisi",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ne sme da se upise")),
    )

    upisana, poruka = nacrti.upisi_u_erp(podaci, korisnik="predrag")

    assert upisana is False
    assert "nije spremna" in poruka


def test_upis_se_ne_ponavlja(db, podaci, monkeypatch):
    from sefsync.services import nacrti

    def lazni_upis(nacrt, produkcija=False):
        nacrt.zaglavlje["BROJ"] = "00450"
        return "ELBSX_2026: MKALKUL 002/U/00450"

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    monkeypatch.setattr(nacrti, "upisi", lazni_upis)
    nacrti.upisi_u_erp(podaci, korisnik="predrag")

    monkeypatch.setattr(
        nacrti, "upisi",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("drugi upis nije smeo")),
    )
    upisana, poruka = nacrti.upisi_u_erp(podaci, korisnik="predrag")

    assert upisana is False
    assert "već upisana" in poruka


def test_operater_moze_da_upise_kalkulaciju_bez_prijema(client, db, podaci, monkeypatch):
    """Kod usluga (088) nema prijema robe - niko ne "primi" struju ni zakup.

    OFFICE nema erp_code, pa se nijedna osoba iz sifarnika ne moze povezati s tom
    jedinicom; bez ovog dugmeta takav dokument ne bi imao ko da pusti.
    """
    from sefsync.models import NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti
    import sefsync.web.app as app_module

    def lazni_upis(nacrt, produkcija=False):
        nacrt.zaglavlje["BROJ"] = "03017"
        return "ELBSX_2026: ULKALKUL 088/U/03017"

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    monkeypatch.setattr(nacrti, "upisi", lazni_upis)
    monkeypatch.setattr(app_module.get_settings(), "erp_upis_kalkulacije", True)
    nacrti.osvezi(podaci)

    odgovor = client.post(
        f"/dokument/{podaci}/upisi-kalkulaciju", follow_redirects=False
    )

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci)
        assert red.stanje is StanjeNacrta.UPISAN
        assert red.upisao == "bogdan"


def test_napomena_preziva_uspesan_upis(client, db, podaci, monkeypatch):
    """Zuta napomena treba da se vidi BAS posle upisa - knjizi se posle njega.

    Nacrt se posle uspesnog upisa vise ne preracunava, pa bi se napomena
    izgubila da se ne sacuva u tom trenutku.
    """
    from sefsync.models import NacrtKalkulacije
    from sefsync.services import nacrti
    import sefsync.web.app as app_module

    napomena = "kod ovog dobavljača je ranije 58 puta biran 21, a 40 puta 1"

    def lazni_upis(nacrt, produkcija=False):
        nacrt.zaglavlje["BROJ"] = "03018"
        return "ELBSX_2026: ULKALKUL 088/U/03018"

    monkeypatch.setattr(nacrti, "pripremi",
                        lambda *a, **k: LazniNacrt(napomene=[napomena]))
    monkeypatch.setattr(nacrti, "upisi", lazni_upis)
    monkeypatch.setattr(app_module.get_settings(), "erp_upis_kalkulacije", True)

    odgovor = client.post(
        f"/dokument/{podaci}/upisi-kalkulaciju", follow_redirects=False
    )

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci)
        assert red.napomene == [napomena]
        assert red.upozorenje is None, "napomena nije greska pri upisu"


def test_bez_prekidaca_operater_ne_moze_da_upise(client, db, podaci, monkeypatch):
    from sefsync.services import nacrti
    import sefsync.web.app as app_module

    monkeypatch.setattr(app_module.get_settings(), "erp_upis_kalkulacije", False)
    monkeypatch.setattr(
        nacrti, "upisi",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ne sme da se upise")),
    )

    odgovor = client.post(
        f"/dokument/{podaci}/upisi-kalkulaciju", follow_redirects=False
    )

    assert "isklju" in odgovor.headers["location"]


def test_neuspeo_upis_ostaje_zapisan_na_dokumentu(db, monkeypatch):
    """Poruka o neuspehu pojavi se jednom na ekranu pa nestane — trag mora da ostane."""
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti as N

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=4242, document_number="26-15240-238911",
                       supplier_name="MISON DOO")
        session.add(doc)
        session.flush()
        session.add(NacrtKalkulacije(
            document_id=doc.id, stanje=StanjeNacrta.SPREMAN, magacin="004",
        ))
        doc_id = doc.id

    def puca(*a, **k):
        raise RuntimeError("ERP ne odgovara")

    monkeypatch.setattr(N, "pripremi", puca)
    upisano, poruka = N.upisi_u_erp(doc_id, korisnik="MikicNemanja")

    assert not upisano
    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, doc_id)
        assert red.upozorenje
        assert "ERP ne odgovara" in red.upozorenje


def test_osvezi_zapele_dira_samo_ono_sto_erp_moze_da_odmrzne(db, monkeypatch):
    """Šifra se otvara u ERP-u, pa panel mora sam da primeti da je stavka odmrznuta."""
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti as N

    with db.session_scope() as session:
        def dodaj(broj, stanje, arhiviran=False):
            doc = Document(sef_invoice_id=abs(hash(broj)) % 10**6, document_number=broj,
                           archived=arhiviran)
            session.add(doc)
            session.flush()
            session.add(NacrtKalkulacije(document_id=doc.id, stanje=stanje))
            return doc.id

        ceka = dodaj("CEKA", StanjeNacrta.CEKA_SIFRU)
        problem = dodaj("PROBLEM", StanjeNacrta.PROBLEM)
        dodaj("SPREMAN", StanjeNacrta.SPREMAN)
        dodaj("UPISAN", StanjeNacrta.UPISAN)
        dodaj("ARHIVIRAN", StanjeNacrta.CEKA_SIFRU, arhiviran=True)

    dirani = []

    def lazni_osvezi(doc_id, engine=None):
        dirani.append(doc_id)
        return None

    monkeypatch.setattr(N, "osvezi", lazni_osvezi)
    ishod = N.osvezi_zapele()

    assert sorted(dirani) == sorted([ceka, problem])
    assert ishod["pregledano"] == 2


def test_povezivanje_bez_sifre_i_barkoda_ne_gasi_prethodnu_vezu(db, monkeypatch):
    """Dule je povezao sve LEXA zvučnike i sve su mu ostale na spisku.

    Ključ veze bio je (dobavljač, prazno, prazno) — isti za svaki takav artikal
    tog dobavljača — pa je svako novo povezivanje gasilo prethodno kao
    „kolebljivo".
    """
    from sqlalchemy import select

    from sefsync.models import Document, DocumentLine, ItemMapping
    from sefsync.services import nacrti as N
    from sefsync.services.nacrti import povezi_sifru

    # osvežavanje nacrta traži ERP; ovde se meri knjiženje veze
    monkeypatch.setattr(N, "osvezi", lambda *a, **k: None)
    monkeypatch.setattr(N, "_cekaju_isti_artikal", lambda *a, **k: [])

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=8800, document_number="R-1384/26VP",
                       supplier_name="VITEL DOO", supplier_vat="100123456")
        session.add(doc)
        session.flush()
        for i, ime in enumerate(
            ("LEXA BT ZVUCNIK LX-201 SPARK", "LEXA BT ZVUCNIK LX-302 NITRO"), start=1
        ):
            session.add(DocumentLine(document_id=doc.id, line_no=i, name=ime,
                                     quantity=1, line_amount=6900.0))
        doc_id = doc.id

    povezi_sifru(doc_id, 1, "98101", korisnik="dusan")
    povezi_sifru(doc_id, 2, "98102", korisnik="dusan")

    with db.session_scope() as session:
        veze = list(session.scalars(
            select(ItemMapping).where(ItemMapping.active == True)  # noqa: E712
        ))
        sifre = sorted(v.erp_item_code for v in veze)

    assert sifre == ["98101", "98102"], "dva različita artikla — dve žive veze"


def test_isti_naziv_sa_drugom_sifrom_i_dalje_gasi_vezu(db, monkeypatch):
    """Kad isti naziv jednom znači jedno a drugi put drugo, veza se gasi."""
    from sqlalchemy import select

    from sefsync.models import Document, DocumentLine, ItemMapping
    from sefsync.services import nacrti as N
    from sefsync.services.nacrti import povezi_sifru

    monkeypatch.setattr(N, "osvezi", lambda *a, **k: None)
    monkeypatch.setattr(N, "_cekaju_isti_artikal", lambda *a, **k: [])

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=8801, document_number="R-2/26",
                       supplier_name="CSM", supplier_vat="107351003")
        session.add(doc)
        session.flush()
        for i in (1, 2):
            session.add(DocumentLine(document_id=doc.id, line_no=i,
                                     name="MASINA ZA PRANJE VESA", quantity=1))
        doc_id = doc.id

    povezi_sifru(doc_id, 1, "15064", korisnik="dusan")
    povezi_sifru(doc_id, 2, "84154", korisnik="dusan")

    with db.session_scope() as session:
        zive = list(session.scalars(
            select(ItemMapping).where(ItemMapping.active == True)  # noqa: E712
        ))
    assert zive == [], "dvosmislen naziv ne sme da ostane kao veza"


def test_ponovno_povezivanje_iste_sifre_ozivljava_vezu(db, monkeypatch):
    """Bez ovoga je čovek mogao da klikće koliko hoće — veza ostaje mrtva."""
    from sqlalchemy import select

    from sefsync.models import Document, DocumentLine, ItemMapping
    from sefsync.services import nacrti as N
    from sefsync.services.nacrti import povezi_sifru

    monkeypatch.setattr(N, "osvezi", lambda *a, **k: None)
    monkeypatch.setattr(N, "_cekaju_isti_artikal", lambda *a, **k: [])

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=8802, document_number="R-3/26",
                       supplier_name="VITEL", supplier_vat="100123456")
        session.add(doc)
        session.flush()
        session.add(DocumentLine(document_id=doc.id, line_no=1,
                                 name="LEXA BT ZVUCNIK LX-201 SPARK", quantity=1))
        session.add(ItemMapping(
            supplier_vat="100123456", supplier_item_name="LEXA BT ZVUCNIK LX-201 SPARK",
            erp_item_code="87422", source="kolebljivo", active=False,
        ))
        doc_id = doc.id

    povezi_sifru(doc_id, 1, "87422", korisnik="dusan")

    with db.session_scope() as session:
        veza = session.scalar(select(ItemMapping))
        assert veza.active is True
        assert veza.source == "rucno"


def test_prepoznaje_finansijske_stavke_po_nazivu():
    """`razlozi_cekanja` razdvaja „čovek mora“ od „ovo nikad neće biti artikal“.

    Dva zatečena odobrenja na 010 (01.10.) padala su pod „čovek“, a nijedno
    nikad neće imati našu šifru.
    """
    from sefsync.services.nacrti import _NIJE_ARTIKAL as uzorak

    for naziv in (
        "KNJIŽNO ODOBRENJE ZA POVRAT AVANSA",
        "Odobravamo otpis neispravne robe CJ301BX",
        "Odobrenje po Saglasnosti broj 277102/2026",
        "Troškovi transporta",
    ):
        assert uzorak.search(naziv), naziv

    for naziv in ("VIVAX IMAGO LED TV-55S60WO", "LEXA BT ZVUCNIK LX-302 NITRO",
                  "MASINA ZA PRANJE VESA"):
        assert not uzorak.search(naziv), naziv


def test_za_dovrsavanje_hvata_tihu_fakturu(db, podaci, monkeypatch):
    """Roba primljena, nacrt spreman, u ERP-u ništa — to niko ne pokreće ponovo."""
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti
    from sefsync.models import utcnow

    with db.session_scope() as session:
        doc = session.get(Document, podaci)
        doc.received_at = utcnow()
        doc.received_by = "bojans"
        session.add(NacrtKalkulacije(document_id=podaci, stanje=StanjeNacrta.SPREMAN,
                                     magacin="003"))

    assert [x[0] for x in nacrti.za_dovrsavanje()] == [podaci]


def test_za_dovrsavanje_preskace_otvoren_manjak(db, podaci):
    """Dok se manjak ne reši, kalkulacija ne ide — možda ostatak nikad ne dođe."""
    from sefsync.models import Document, NacrtKalkulacije, PrijavaManjka, StanjeNacrta
    from sefsync.services import nacrti
    from sefsync.models import utcnow

    with db.session_scope() as session:
        doc = session.get(Document, podaci)
        doc.received_at = utcnow()
        session.add(NacrtKalkulacije(document_id=podaci, stanje=StanjeNacrta.SPREMAN,
                                     magacin="003"))
        from sefsync.models import OdlukaManjka

        session.add(PrijavaManjka(
            document_id=podaci, prijavio="bojans", odluka=OdlukaManjka.KASNIJE,
            stavke=[{"line_no": 1, "naziv": "Frižider", "fakturisano": 2, "stiglo": 1}]))

    assert nacrti.za_dovrsavanje() == []


def test_dovrsi_upise_ne_pada_na_jednom_dokumentu(db, podaci, monkeypatch):
    """Ciklus ne sme da stane zato što je jedan upis pukao."""
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti
    from sefsync.models import utcnow

    with db.session_scope() as session:
        doc = session.get(Document, podaci)
        doc.received_at = utcnow()
        session.add(NacrtKalkulacije(document_id=podaci, stanje=StanjeNacrta.SPREMAN,
                                     magacin="003"))

    def puca(*a, **k):
        raise RuntimeError("ERP nedostupan")

    monkeypatch.setattr(nacrti, "upisi_u_erp", puca)
    assert nacrti.dovrsi_upise() == {"nasao": 1, "upisao": 0, "nije": 1}


class _ErpSaKalkulacijom:
    """ERP u kom kalkulacija 002/00516 stoji i nije proknjižena."""

    def __init__(self, proknjizeno=0):
        self.proknjizeno = proknjizeno

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, upit, *a, **k):
        self.upit = str(upit)
        return self

    def first(self):
        if "PROKNJIZENO" in self.upit:
            return type("R", (), {"p": self.proknjizeno})()
        if "ARTIKLI" in self.upit:
            return (1,)          # šifra postoji u šifarniku
        return None

    def scalar(self):
        return None

    def all(self):
        return []

    def __iter__(self):
        if "ARTIKLI" in getattr(self, "upit", ""):
            return iter([type("R", (), {"a": "87422", "n": "Lexa LX 201 Spark"})(),
                         type("R", (), {"a": "87423", "n": "Lexa LX 302 Nitro"})()])
        return iter(())


def _upisan_nacrt(db, doc_id, sifra="87422"):
    from sefsync.models import utcnow

    with db.session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(DocumentLine.document_id == doc_id)
        )
        linija.erp_item_code = sifra
        linija.erp_mapping_source = "rucno"
        session.add(NacrtKalkulacije(
            document_id=doc_id, stanje=StanjeNacrta.UPISAN, magacin="002",
            tabela="MKALKUL", erp_broj="00516", upisao="dusan", upisano_u=utcnow(),
        ))


def test_ispravka_sifre_posle_upisa(client, db, podaci, monkeypatch):
    """Dule je upisao pogrešnu šifru, kalkulacija je već otišla u ERP.

    App u ERP-u ne sme ništa da menja (ima samo INSERT), pa popravlja vezu i
    zabeleži šta tačno čoveka čeka.
    """
    from sefsync.erp import connection as veza
    from sefsync.notify import dispatcher as disp
    from sefsync.services import nacrti

    _upisan_nacrt(db, podaci)
    erp = _ErpSaKalkulacijom(proknjizeno=0)
    monkeypatch.setattr(veza, "get_erp_engine", lambda *a, **k: erp)
    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    poslato = []
    monkeypatch.setattr(disp.Dispatcher, "notify_ispravka",
                        lambda self, doc_id: poslato.append(doc_id) or True)

    odgovor = client.post(f"/dokument/{podaci}/ispravi-sifru",
                          data={"line_no": 1, "erp_item_code": "87423"},
                          follow_redirects=False)

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci)
        assert red.stanje is StanjeNacrta.UPISAN, "nacrt se ne vraća unazad"
        i = red.ispravka
        assert i["stara"] == "87422" and i["nova"] == "87423"
        assert i["kalkulacija"] == "002/00516" and i["stavka"] == 1
        assert i["proknjizeno"] == 0 and "nije proknjižena" in i["uputstvo"]
        linija = session.scalar(
            select(DocumentLine).where(DocumentLine.document_id == podaci)
        )
        assert linija.erp_item_code == "87423", "veza mora da važi za ubuduće"
    assert poslato == [podaci], "jedinica nije obaveštena"


def test_ispravka_kaze_da_je_kalkulacija_zakljucana(client, db, podaci, monkeypatch):
    """Kod PROKNJIZENO 2 nije dovoljno promeniti polje — poruka to mora da kaže."""
    from sefsync.erp import connection as veza
    from sefsync.notify import dispatcher as disp
    from sefsync.services import nacrti

    _upisan_nacrt(db, podaci)
    monkeypatch.setattr(veza, "get_erp_engine",
                        lambda *a, **k: _ErpSaKalkulacijom(proknjizeno=2))
    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    monkeypatch.setattr(disp.Dispatcher, "notify_ispravka", lambda self, doc_id: True)

    client.post(f"/dokument/{podaci}/ispravi-sifru",
                data={"line_no": 1, "erp_item_code": "87423"}, follow_redirects=False)

    with db.session_scope() as session:
        i = session.get(NacrtKalkulacije, podaci).ispravka
        assert i["proknjizeno"] == 2 and "zaključana" in i["uputstvo"]


def test_ispravka_se_zatvara_kad_je_uradjena(client, db, podaci, monkeypatch):
    from sefsync.erp import connection as veza
    from sefsync.notify import dispatcher as disp
    from sefsync.services import nacrti

    _upisan_nacrt(db, podaci)
    monkeypatch.setattr(veza, "get_erp_engine", lambda *a, **k: _ErpSaKalkulacijom())
    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    monkeypatch.setattr(disp.Dispatcher, "notify_ispravka", lambda self, doc_id: True)
    client.post(f"/dokument/{podaci}/ispravi-sifru",
                data={"line_no": 1, "erp_item_code": "87423"}, follow_redirects=False)

    odgovor = client.post(f"/dokument/{podaci}/ispravka-zatvori", follow_redirects=False)

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci)
        assert red.ispravka_zatvorena_u is not None
        assert red.ispravka_zatvorio == "bogdan"


def test_ispravka_ne_radi_dok_kalkulacija_nije_u_erpu(client, db, podaci, monkeypatch):
    """Dok nije upisana, šifra se menja običnim povezivanjem — ne ovim putem."""
    from sefsync.services import nacrti

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    nacrti.osvezi(podaci)

    odgovor = client.post(f"/dokument/{podaci}/ispravi-sifru",
                          data={"line_no": 1, "erp_item_code": "87423"},
                          follow_redirects=False)

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        assert session.get(NacrtKalkulacije, podaci).ispravka is None


class _ErpZaOtvaranje:
    """ERP u kom šifra 86100 nije zauzeta i barkod je slobodan."""

    def __init__(self):
        self.upisano = []
        self.posle_commita = False

    def connect(self):
        return self

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, upit, parametri=None, *a, **k):
        self.upit = str(upit)
        if self.upit.strip().upper().startswith("INSERT"):
            self.upisano.append((self.upit, parametri))
        return _Rezultat(self._odgovor())

    def _odgovor(self):
        u = self.upit
        if "PRODAV" in u:
            return [type("R", (), {"s": "001"})(), type("R", (), {"s": "002"})()]
        if "DB_NAME" in u:
            return [("ELBSX_2026",)]
        return []

    def first(self):
        return _Rezultat(self._odgovor()).first()

    def scalar(self):
        return "ELBSX_2026" if "DB_NAME" in getattr(self, "upit", "") else None


def test_otvaranje_sifre_trazi_cenu_i_barkod(db, podaci):
    """Šifra bez cene prođe u šifarniku, a kalkulacija posle staje — zato uslov."""
    import pytest

    from sefsync.erp.artikal import PredlogArtikla, redovi

    p = PredlogArtikla(document_id=podaci, line_no=1, predlozena_sifra="86100",
                       naziv="Frižider", barkod="8606012345678",
                       magacini=["099"], prodajna_mesta=["001"])
    with pytest.raises(ValueError, match="cena"):
        redovi(p)

    p.cena_mp = 59900.0
    red = redovi(p)["ARTPROD"][0]
    assert red["CENAMALO"] == 59900.0

    p.barkod = ""
    with pytest.raises(ValueError, match="[Bb]arkod"):
        redovi(p)

    # Tek kad čovek izričito kaže da barkoda nema, upisuje se šifra umesto njega.
    p.bez_barkoda = True
    assert redovi(p)["ARTIKLI"][0]["BARCODE"] == "86100"


def test_otvaranje_ne_pise_u_dartikli(db, podaci):
    """DARTIKLI puni noćni posao na serveru — dupli red tamo ne bi ni pukao."""
    from sefsync.erp.artikal import PredlogArtikla, redovi

    p = PredlogArtikla(document_id=podaci, line_no=1, predlozena_sifra="86100",
                       naziv="Frižider", barkod="860", cena_mp=100.0,
                       magacini=["099"], prodajna_mesta=["001"])

    assert set(redovi(p)) == {"ARTIKLI", "ARTPROD"}


def test_slicni_artikli_upozoravaju_na_duplikat(db):
    """Kad EAN-a nema, naziv je jedina provera da se isto ne otvori dvaput."""
    from sefsync.erp.artikal import slicni_artikli

    class Conn:
        def execute(self, upit, *a, **k):
            if "ARTPROD" in str(upit):
                return _Rezultat([type("R", (), {"mp": 12400.0, "nab": 6900.0})()])
            return [
                type("R", (), {"a": "87423", "n": "Lexa LX 302 Nitro", "b": "860", "k": "853",
                                "p": "Kina", "g": 24, "pg": 36})(),
                type("R", (), {"a": "12345", "n": "Nitro gorivo 5l", "b": "861", "k": "411",
                                "p": "", "g": None, "pg": None})(),
            ]

    nadjeni = slicni_artikli(Conn(), "LEXA BT ZVUCNIK LX-302 NITRO")

    assert [x["artikal"] for x in nadjeni] == ["87423"]


class _ErpSaMarzom:
    """ERP u kom dobavljač ima zadate marže u istoriji."""

    def __init__(self, marze):
        self.marze = marze

    def execute(self, upit, *a, **k):
        if "MPRULK" in str(upit):
            return _Rezultat([
                type("R", (), {"nab": 100.0, "mp": 100.0 * (1 + m) * 1.2, "st": 20.0})()
                for m in self.marze
            ])
        return _Rezultat([])


def test_cena_se_predlaze_samo_kad_je_marza_uska():
    """Vimport ide 31–33 % — tu predlog ima smisla."""
    from sefsync.erp.artikal import predlog_cene

    marze = [0.31, 0.32, 0.32, 0.32, 0.33] * 8          # 40 stavki, usko
    p = predlog_cene(_ErpSaMarzom(marze), "00123", 4490.0, 20.0)

    assert p["cena"] == round(4490 * 1.32 * 1.2 / 100) * 100
    assert p["cena"] % 100 == 0, "maloprodajne cene su zaokružene na 100"
    assert "medijana marže" in p["razlog"]


def test_cena_se_ne_predlaze_kad_je_marza_siroka():
    """Coffee Center ide 69–134 % — pogrešan broj je gori od praznog polja."""
    from sefsync.erp.artikal import predlog_cene

    marze = [0.69, 0.80, 0.91, 1.10, 1.34] * 8
    p = predlog_cene(_ErpSaMarzom(marze), "00123", 4490.0, 20.0)

    assert p["cena"] is None
    assert "ne predlažem" in p["razlog"]


def test_cena_se_ne_predlaze_bez_dovoljno_istorije():
    from sefsync.erp.artikal import predlog_cene

    p = predlog_cene(_ErpSaMarzom([0.4] * 5), "00123", 4490.0, 20.0)

    assert p["cena"] is None and "svega 5" in p["razlog"]


def test_strana_predloga_ne_puca_kad_sifra_vec_postoji():
    """Barkod koji već ima šifru: tu se ništa ne otvara, pa nema ni šta da se ispiše.

    `redovi` je strog jer čuva upis — traži barkod i cenu. Prikaz ne sme da
    pada zbog toga; strana je tako vraćala Internal Server Error baš na stavci
    koju treba samo povezati (05.10.).
    """
    from sefsync.erp.artikal import PredlogArtikla, kao_sql

    p = PredlogArtikla(document_id=1, line_no=1, sifra="87423", naziv="Lexa LX 302",
                       barkod="8606012345678", magacini=["099"], prodajna_mesta=["001"])

    assert kao_sql(p) == ""


def test_ispis_sql_a_radi_kad_je_predlog_potpun():
    from sefsync.erp.artikal import PredlogArtikla, kao_sql

    p = PredlogArtikla(document_id=1, line_no=1, predlozena_sifra="86100",
                       naziv="Frižider", barkod="860", cena_mp=59900.0,
                       magacini=["099"], prodajna_mesta=["001"])

    assert "INSERT INTO dbo.ARTIKLI" in kao_sql(p)


def test_ispravka_pre_upisa_je_obicno_povezivanje(client, db, podaci, monkeypatch):
    """Dule pogreši šifru pre nego što kalkulacija ode u ERP.

    Takva stavka ima šifru, pa je nema na tabu Šifre; forma na dokumentu se
    ranije nudila samo kad je kalkulacija već upisana — i nije imao gde da je
    promeni (Bane, 05.10.).
    """
    from sefsync.models import DocumentLine, NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti

    with db.session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(DocumentLine.document_id == podaci)
        )
        linija.erp_item_code = "87422"
        linija.erp_mapping_source = "rucno"
        session.add(NacrtKalkulacije(document_id=podaci, stanje=StanjeNacrta.SPREMAN,
                                     magacin="002"))

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    monkeypatch.setattr(
        nacrti, "_proveri_sifru", lambda *a, **k: None)   # ERP nije u igri

    odgovor = client.post(f"/dokument/{podaci}/ispravi-sifru",
                          data={"line_no": 1, "erp_item_code": "87423"},
                          follow_redirects=False)

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(DocumentLine.document_id == podaci)
        )
        assert linija.erp_item_code == "87423"
        red = session.get(NacrtKalkulacije, podaci)
        assert red.ispravka is None, "nema šta da se ispravlja u ERP-u"


def test_dokument_nudi_ispravku_i_pre_upisa(client, db, podaci, monkeypatch):
    from sefsync.models import NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti

    with db.session_scope() as session:
        session.add(NacrtKalkulacije(document_id=podaci, stanje=StanjeNacrta.SPREMAN,
                                     magacin="002"))

    tekst = client.get(f"/dokument/{podaci}").text

    assert "Pogrešna šifra artikla?" in tekst


def test_sifra_se_otvara_sa_kategorijom():
    """`SHKAT` je NOT NULL — bez nje upis puca na samoj bazi.

    Duletovo otvaranje šifre je tako palo: „Cannot insert the value NULL into
    column 'SHKAT'“ (07.10.). Kategorija nije popuna nego prava vrednost:
    laptop 712, televizor 820, zvučnici 853.
    """
    from sefsync.erp.artikal import PredlogArtikla, redovi

    p = PredlogArtikla(document_id=1, line_no=1, predlozena_sifra="14445",
                       naziv="Dell OEM 15", barkod="217", cena_mp=57000.0,
                       shkat="712", magacini=["099"], prodajna_mesta=["001"])

    red = redovi(p)
    assert red["ARTIKLI"][0]["SHKAT"] == "712"
    assert red["ARTPROD"][0]["SHKAT"] == "712"


def test_kategorija_se_predlaze_po_slicnim_artiklima():
    """Artikli iste serije dele kategoriju — Lexa LX-201 i LX-302 su oba 853."""
    from sefsync.erp.artikal import predlog_kategorije

    assert predlog_kategorije([{"shkat": "853"}, {"shkat": "853"}, {"shkat": "411"}]) == "853"


def test_kategorija_se_ne_pogadja_kad_se_slicni_ne_slazu():
    from sefsync.erp.artikal import predlog_kategorije

    assert predlog_kategorije([{"shkat": "853"}, {"shkat": "411"}]) == ""
    assert predlog_kategorije([]) == ""


def test_dopuna_cenovnika_trazi_cenu_i_postojecu_sifru(monkeypatch):
    """Dopuna ne izmišlja ni šifru ni cenu."""
    import pytest

    from sefsync.erp import artikal as A

    class PrazanErp:
        def begin(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return self

        def first(self):
            return None

        def __iter__(self):
            return iter(())

    monkeypatch.setattr(A, "erp_engine_za_upis",
                        lambda produkcija=False: (PrazanErp(), "ELBSX_2026"))

    with pytest.raises(ValueError, match="cena"):
        A.dopuni_cenovnike("87622", 0)
    with pytest.raises(ValueError, match="nema u jedinstvenom"):
        A.dopuni_cenovnike("87622", 100.0)


def test_cena_poreklo_i_garancije_idu_u_svaki_red():
    """Šifre 14452-14457 su stajale bez cene u svih 11 magacina (Bane, 14.10.).

    Postojeći artikli nose `CENAMALO` u svakom redu `ARTIKLI`, a poreklo i
    garancije je Dule dopisivao ručno posle otvaranja.
    """
    from sefsync.erp.artikal import PredlogArtikla, redovi

    p = PredlogArtikla(document_id=1, line_no=1, predlozena_sifra="14458",
                       naziv="Gorenje ECT 603 FM", barkod="3838782", cena_mp=28400.0,
                       magacini=["001", "099"], prodajna_mesta=["001", "071"],
                       poreklo="Slovenija", garancija=24, produzena_garancija=36)

    red = redovi(p)
    for r in red["ARTIKLI"]:
        assert r["CENAMALO"] == 28400.0
    for r in red["ARTIKLI"] + red["ARTPROD"]:
        assert (r["POREKLO"], r["GARANCIJA"], r["PRODUZENAGARANCIJA"]) == ("Slovenija", 24, 36)


def test_poreklo_i_garancije_po_slicnim_artiklima():
    from sefsync.erp.artikal import _vecina

    slicni = [{"poreklo": "Kina", "garancija": 24}, {"poreklo": "Kina", "garancija": 24},
              {"poreklo": "", "garancija": 36}]
    assert _vecina(slicni, "poreklo") == "Kina"
    assert _vecina(slicni, "garancija") == 24
    assert _vecina([{"garancija": 24}, {"garancija": 36}], "garancija") is None
    assert _vecina([], "garancija") is None
