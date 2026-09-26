"""Panel: prijava, red operatera, prosledjivanje i ekran poslovodje."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sefsync.models import (
    BusinessUnit,
    Document,
    DocumentLine,
    RoutingRule,
    RoutingSource,
    SefStatus,
    UnitKind,
)
from sefsync.config import get_settings
from sefsync.web.auth import Role, User

OPERATER = User("bogdan", "Sunajko Bogdan", Role.OPERATER, None, "FINANSIJE")
POSLOVODJA = User("predrag", "Bajic Predrag", Role.POSLOVODJA, "002", "POSLOVODJA")
TUDJ = User("dane", "Sunajko Dane", Role.POSLOVODJA, "001", "POSLOVODJA")


@pytest.fixture
def client(db) -> TestClient:
    from sefsync.web.app import app

    return TestClient(app)


def prijavi(client: TestClient, user: User) -> None:
    """Ubacuje korisnika u sesiju bez odlaska na SQL server."""
    import sefsync.web.app as app_module

    app_module.app.dependency_overrides[app_module.current_user] = lambda: user


@pytest.fixture(autouse=True)
def _ocisti_override():
    yield
    import sefsync.web.app as app_module

    app_module.app.dependency_overrides.clear()


@pytest.fixture
def podaci(db):
    """Jedna prodavnica i jedan dokument spreman za prosledjivanje."""
    with db.session_scope() as session:
        unit = BusinessUnit(code="MP002", name="PRODAJNO MESTO APATIN", kind=UnitKind.RETAIL,
                            erp_code="002", emails="apatin@elbraco.rs")
        druga = BusinessUnit(code="MP001", name="PRODAJNO MESTO SOMBOR", kind=UnitKind.RETAIL,
                             erp_code="001")
        session.add_all([unit, druga])
        session.flush()
        doc = Document(
            sef_invoice_id=500100, document_number="26-30F-087050",
            supplier_name="EWE COMP DOO", supplier_vat="100042618",
            amount=194666.40, sum_without_vat=162222.0, vat_amount=32444.4, currency="RSD",
            sef_status=SefStatus.NEW, business_unit_id=unit.id,
            delivery_address="Srpskih vladara 46, 25260, Apatin",
        )
        doc.lines.append(DocumentLine(line_no=1, name="Frižider", quantity=2, unit_code="H87",
                                      price=50000.0, line_amount=100000.0, vat_percent=20.0))
        nerazvrstan = Document(
            sef_invoice_id=500200, document_number="FA-5539", supplier_name="FORMA PLUS",
            supplier_vat="101717578", supplier_reg_no="17242792",
            amount=12000.0, sef_status=SefStatus.SEEN,
        )
        session.add_all([doc, nerazvrstan])
        session.flush()
        return {"unit": unit.id, "druga": druga.id, "doc": doc.id, "nerazvrstan": nerazvrstan.id}


class LazniNotifier:
    def __init__(self, uspeh=True):
        self.uspeh = uspeh
        self.pozvano: list[int] = []
        self.potvrde: list[tuple[int, str]] = []

    def notify_document(self, doc_id: int) -> bool:
        self.pozvano.append(doc_id)
        return self.uspeh

    def notify_receipt(self, doc_id: int, confirmed_by: str) -> bool:
        self.potvrde.append((doc_id, confirmed_by))
        return True


# --------------------------------------------------------------------------- #
# prijava
# --------------------------------------------------------------------------- #


def test_bez_prijave_vodi_na_ekran_za_prijavu(client):
    odgovor = client.get("/", follow_redirects=False)

    assert odgovor.status_code == 303
    assert odgovor.headers["location"] == "/prijava"


def test_ekran_za_prijavu_je_dostupan(client):
    assert "Prijava istim nalogom" in client.get("/prijava").text


def test_health_ne_trazi_prijavu(client):
    assert client.get("/health").json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# red operatera
# --------------------------------------------------------------------------- #


def test_red_prikazuje_dokumente_koji_traze_potez(client, podaci):
    prijavi(client, OPERATER)

    tekst = client.get("/red").text

    assert "26-30F-087050" in tekst
    assert "FA-5539" in tekst


def test_prosledjen_i_prihvacen_dokument_izlazi_iz_reda(client, podaci, db):
    prijavi(client, OPERATER)
    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        doc.sef_status = SefStatus.APPROVED
        from sefsync.models import utcnow

        doc.forwarded_at = utcnow()
        doc.forwarded_by = "bogdan"

    assert "26-30F-087050" not in client.get("/red").text
    assert "26-30F-087050" in client.get("/red?prikaz=ceka_prijem").text


def test_nerazvrstano_ima_svoj_prikaz(client, podaci):
    prijavi(client, OPERATER)

    tekst = client.get("/red?prikaz=nerazvrstano").text

    assert "FA-5539" in tekst
    assert "26-30F-087050" not in tekst


def test_dodela_pj_pamti_pravilo(client, podaci, db):
    prijavi(client, OPERATER)

    odgovor = client.post(
        f"/dokument/{podaci['nerazvrstan']}/pj",
        data={"business_unit_id": podaci["unit"], "napravi_pravilo": "1",
              "polje": "supplier_name"},
        follow_redirects=False,
    )

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        doc = session.get(Document, podaci["nerazvrstan"])
        assert doc.business_unit_id == podaci["unit"]
        assert doc.routing_source is RoutingSource.MANUAL
        assert session.scalar(select(RoutingRule)).pattern == "FORMA PLUS"


# --------------------------------------------------------------------------- #
# prosledjivanje
# --------------------------------------------------------------------------- #


def test_prosledjivanje_belezi_ko_je_i_kada(podaci, db):
    from sefsync.services.workflow import Workflow

    notifier = LazniNotifier()
    Workflow(notifier=notifier).forward([podaci["doc"]], actor="bogdan")

    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        assert doc.forwarded_at is not None
        assert doc.forwarded_by == "bogdan"
    assert notifier.pozvano == [podaci["doc"]]


def test_nerazvrstan_dokument_se_ne_prosledjuje(db, podaci):
    from sefsync.services.workflow import Workflow

    notifier = LazniNotifier()
    rezultat = Workflow(notifier=notifier).forward([podaci["nerazvrstan"]], actor="bogdan")

    assert not rezultat.ok
    assert "nije razvrstan" in rezultat.problems[0]
    assert notifier.pozvano == []          # nista nije poslato


def test_neuspelo_slanje_ne_oznacava_dokument_kao_prosledjen(db, podaci):
    from sefsync.services.workflow import Workflow

    rezultat = Workflow(notifier=LazniNotifier(uspeh=False)).forward([podaci["doc"]], actor="bogdan")

    assert not rezultat.ok
    with db.session_scope() as session:
        assert session.get(Document, podaci["doc"]).forwarded_at is None


# --------------------------------------------------------------------------- #
# poslovodja
# --------------------------------------------------------------------------- #


def test_poslovodja_vidi_samo_prosledjeno_svojoj_jedinici(client, podaci, db):
    from sefsync.services.workflow import Workflow

    Workflow(notifier=LazniNotifier()).forward([podaci["doc"]], actor="bogdan")
    prijavi(client, POSLOVODJA)

    tekst = client.get("/moja-jedinica").text

    assert "26-30F-087050" in tekst
    assert "FA-5539" not in tekst


def test_poslovodja_ne_vidi_tudj_dokument(client, podaci):
    from sefsync.services.workflow import Workflow

    Workflow(notifier=LazniNotifier()).forward([podaci["doc"]], actor="bogdan")
    prijavi(client, TUDJ)

    assert client.get(f"/dokument/{podaci['doc']}").status_code == 403


def test_poslovodja_ne_vidi_dokument_koji_mu_nije_prosledjen(client, podaci):
    prijavi(client, POSLOVODJA)

    assert client.get(f"/dokument/{podaci['doc']}").status_code == 403


def test_poslovodja_potvrdjuje_prijem(client, podaci, db):
    from sefsync.services.workflow import Workflow

    Workflow(notifier=LazniNotifier()).forward([podaci["doc"]], actor="bogdan")
    prijavi(client, POSLOVODJA)

    odgovor = client.post(
        f"/dokument/{podaci['doc']}/prijem",
        data={"napomena": "jedan komad oštećen"},
        follow_redirects=False,
    )

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        assert doc.received_by == "predrag"
        assert doc.received_note == "jedan komad oštećen"


def test_poslovodja_ne_sme_u_sifarnike(client, podaci):
    prijavi(client, POSLOVODJA)

    assert client.get("/pj").status_code == 403
    assert client.get("/pravila").status_code == 403
    assert client.get("/red").status_code == 403


def test_operater_vidi_sifarnike(client, podaci):
    prijavi(client, OPERATER)

    assert client.get("/pj").status_code == 200
    assert client.get("/pravila").status_code == 200


# --------------------------------------------------------------------------- #
# arhiva
# --------------------------------------------------------------------------- #


def test_arhiviranje_sklanja_iz_reda_ali_ne_brise(client, podaci, db):
    prijavi(client, OPERATER)

    client.post("/arhiviraj", data={"dokumenti": str(podaci["doc"]), "povratak": "/red"},
                follow_redirects=False)

    assert "26-30F-087050" not in client.get("/red").text
    assert "26-30F-087050" in client.get("/red?prikaz=arhiva").text
    with db.session_scope() as session:
        assert session.get(Document, podaci["doc"]).archived is True


def test_api_poslovodji_daje_samo_njegove_dokumente(client, podaci):
    prijavi(client, POSLOVODJA)

    podaci_api = client.get("/api/dokumenti").json()

    assert [d["broj"] for d in podaci_api] == ["26-30F-087050"]


def test_potvrda_prijema_javlja_operateru(podaci, db):
    """Operater koji je prosledio mora da sazna da je roba stigla."""
    from sefsync.services.workflow import Workflow

    notifier = LazniNotifier()
    tok = Workflow(notifier=notifier)
    tok.forward([podaci["doc"]], actor="nina")
    tok.confirm_receipt(podaci["doc"], actor="predrag", note="sve u redu")

    assert notifier.potvrde == [(podaci["doc"], "predrag")]


def test_dvostruka_potvrda_se_odbija(podaci, db):
    from sefsync.services.workflow import Workflow

    tok = Workflow(notifier=LazniNotifier())
    tok.forward([podaci["doc"]], actor="nina")
    tok.confirm_receipt(podaci["doc"], actor="predrag")
    ponovo = tok.confirm_receipt(podaci["doc"], actor="dane")

    assert not ponovo.ok
    assert "već potvrdio predrag" in ponovo.message


def test_obavestenje_o_prijemu_ide_onome_ko_je_prosledio(db, podaci, monkeypatch):
    """Adresa se traži u šifarniku osoblja, da nema drugog spiska mejlova."""
    from sefsync.notify.dispatcher import Dispatcher
    from sefsync.services.workflow import Workflow

    monkeypatch.setattr(
        "sefsync.erp.staff.find_by_login",
        lambda login: {"ime": "Bajic Predrag", "email": "apatin@elbraco.rs"},
    )
    monkeypatch.setattr("sefsync.erp.staff.email_for_login", lambda login: f"{login}@elbraco.rs")

    poslato: list[tuple[list[str], str]] = []

    class LazniKanal:
        name = "email"

        def enabled(self):
            return True

        def send(self, targets, message):
            from sefsync.notify.base import SendResult

            poslato.append((targets, message.subject))
            return [SendResult(True, t) for t in targets]

    settings = get_settings()
    monkeypatch.setattr(settings, "notify_override_to", "")
    dispatcher = Dispatcher(settings, email=LazniKanal())
    tok = Workflow(settings=settings, notifier=dispatcher)

    with db.session_scope() as session:
        from sefsync.models import Document, utcnow

        doc = session.get(Document, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.forwarded_by = "nina"

    tok.confirm_receipt(podaci["doc"], actor="predrag", note="jedan komad manjka")

    primaoci, naslov = poslato[0]
    assert primaoci == ["nina@elbraco.rs"]
    assert naslov.startswith("Prijem potvrđen · MP002")


def test_filter_sa_praznim_izborom(client, podaci):
    """Forma šalje pj='' kad je izabrano '— sve —' — to nije greška nego 'sve'."""
    prijavi(client, OPERATER)

    odgovor = client.get("/red?prikaz=red&pj=&tip=&q=")

    assert odgovor.status_code == 200
    assert "26-30F-087050" in odgovor.text


def test_filter_po_poslovnoj_jedinici_i_dalje_radi(client, podaci):
    prijavi(client, OPERATER)

    tekst = client.get(f"/red?prikaz=red&pj={podaci['unit']}").text

    assert "26-30F-087050" in tekst
    assert "FA-5539" not in tekst      # nerazvrstan dokument nije u toj PJ


def test_nerazvrstano_preko_filtera_pj_nula(client, podaci):
    prijavi(client, OPERATER)

    tekst = client.get("/red?prikaz=red&pj=0").text

    assert "FA-5539" in tekst
    assert "26-30F-087050" not in tekst


def test_neispravna_strana_ne_ruši_prikaz(client, podaci):
    prijavi(client, OPERATER)

    assert client.get("/red?strana=").status_code == 200
    assert client.get("/red?strana=xyz").status_code == 200


def test_dodela_iz_reda_pamti_pravilo_i_bez_adrese_isporuke(client, podaci, db):
    """Dokument bez adrese isporuke mora da nauči po nečemu drugom.

    Red šalje samo „napravi_pravilo“, bez izbora polja — ranije je ruta tu
    podrazumevala adresu isporuke, pa se kod takvih dokumenata pravilo nije
    pravilo, a operater je to lako previđao.
    """
    from sqlalchemy import select as _select

    from sefsync.models import MatchField as MF

    with db.session_scope() as session:
        doc = session.get(Document, podaci["nerazvrstan"])
        doc.delivery_address = None
        doc.delivery_name = None
        doc.supplier_vat = "111222333"

    prijavi(client, OPERATER)
    odgovor = client.post(
        f"/dokument/{podaci['nerazvrstan']}/pj",
        data={"business_unit_id": podaci["unit"], "napravi_pravilo": "1"},
        follow_redirects=False,
    )

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        pravilo = session.scalar(_select(RoutingRule))
        assert pravilo is not None, "pravilo nije zapamćeno"
        assert pravilo.field is MF.SUPPLIER_VAT
        assert pravilo.pattern == "111222333"


def test_izricito_polje_i_dalje_ima_prednost(client, podaci, db):
    from sqlalchemy import select as _select

    from sefsync.models import MatchField as MF

    prijavi(client, OPERATER)
    client.post(
        f"/dokument/{podaci['nerazvrstan']}/pj",
        data={"business_unit_id": podaci["unit"], "napravi_pravilo": "1",
              "polje": "supplier_name"},
        follow_redirects=False,
    )

    with db.session_scope() as session:
        pravilo = session.scalar(_select(RoutingRule))
        assert pravilo.field is MF.SUPPLIER_NAME
        assert pravilo.pattern == "FORMA PLUS"


# --------------------------------------------------------------------------- #
# "nepregledano" - nasa zamena za status "Nova" na portalu
# --------------------------------------------------------------------------- #


def test_nov_dokument_je_nepregledan_i_vidi_se_u_redu(client, podaci):
    prijavi(client, OPERATER)

    odgovor = client.get("/red?prikaz=nepregledano")

    assert odgovor.status_code == 200
    assert "26-30F-087050" in odgovor.text
    assert "novo" in odgovor.text


def test_oznaka_pregledano_belezi_ko_je_i_kada(client, podaci, db):
    prijavi(client, OPERATER)

    client.post(
        "/pregledano",
        data={"dokumenti": str(podaci["doc"]), "povratak": "/red"},
        follow_redirects=False,
    )

    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        assert doc.pregledano_at is not None
        assert doc.pregledano_by == "bogdan"
    odgovor = client.get("/red?prikaz=nepregledano")
    assert "26-30F-087050" not in odgovor.text


def test_prosledjivanje_samo_po_sebi_skida_oznaku(podaci, db):
    """Ko prosledi dokument objektu, taj ga je i video."""
    from sefsync.services.workflow import Workflow

    Workflow(notifier=LazniNotifier()).forward([podaci["doc"]], actor="nina")

    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        assert doc.pregledano_by == "nina"


def test_ponovno_oznacavanje_ne_menja_ko_je_prvi_video(client, podaci, db):
    prijavi(client, OPERATER)
    client.post("/pregledano", data={"dokumenti": str(podaci["doc"])}, follow_redirects=False)
    with db.session_scope() as session:
        prvi_put = session.get(Document, podaci["doc"]).pregledano_at

    client.post("/pregledano", data={"dokumenti": str(podaci["doc"])}, follow_redirects=False)

    with db.session_scope() as session:
        assert session.get(Document, podaci["doc"]).pregledano_at == prvi_put


def test_prosledjena_istorija_ne_zatrpava_prikaz_novih(client, podaci, db):
    """Polja za oznaku postoje tek od 17.09.; prosledjen dokument je vidjen i bez njih."""
    from sefsync.models import utcnow

    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.forwarded_by = "nina"
        doc.pregledano_at = None          # stara istorija, bez oznake
    prijavi(client, OPERATER)

    odgovor = client.get("/red?prikaz=nepregledano")

    assert "26-30F-087050" not in odgovor.text


# --------------------------------------------------------------------------- #
# slike sa objekta (zamena za Viber grupu)
# --------------------------------------------------------------------------- #

MALI_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfabd40000000049454e44ae426082"
)


def _prosledi(db, doc_id):
    from sefsync.models import utcnow

    with db.session_scope() as session:
        doc = session.get(Document, doc_id)
        doc.forwarded_at = utcnow()
        doc.forwarded_by = "nina"


def test_poslovodja_salje_sliku_ambalaze(client, podaci, db):
    """Slikanje radi poslovodja na licu mesta - zato slika ide uz dokument, ne u Viber."""
    from sefsync.models import Slika

    _prosledi(db, podaci["doc"])
    prijavi(client, POSLOVODJA)

    odgovor = client.post(
        f"/dokument/{podaci['doc']}/slika",
        files={"slika": ("ambalaza.png", MALI_PNG, "image/png")},
        data={"line_no": "1", "napomena": "zemlja porekla Turska"},
        follow_redirects=False,
    )

    assert odgovor.status_code == 303
    with db.session_scope() as session:
        sl = session.scalar(select(Slika))
        assert sl.document_id == podaci["doc"]
        assert sl.line_no == 1
        assert sl.poslao == "predrag"
        assert sl.napomena == "zemlja porekla Turska"
        from pathlib import Path

        assert Path(sl.putanja).exists()


def test_tudj_dokument_ne_prima_sliku(client, podaci, db):
    _prosledi(db, podaci["doc"])
    prijavi(client, TUDJ)          # poslovodja drugog objekta

    odgovor = client.post(
        f"/dokument/{podaci['doc']}/slika",
        files={"slika": ("x.png", MALI_PNG, "image/png")},
        follow_redirects=False,
    )

    assert odgovor.status_code == 403


def test_prima_se_samo_slika(client, podaci, db):
    """Ulaz koji koristi neko van kancelarije ne veruje onome sto je poslato."""
    _prosledi(db, podaci["doc"])
    prijavi(client, POSLOVODJA)

    odgovor = client.post(
        f"/dokument/{podaci['doc']}/slika",
        files={"slika": ("virus.exe", b"MZ\x90\x00", "application/x-msdownload")},
        follow_redirects=False,
    )

    assert odgovor.status_code == 303
    assert "GRE" in odgovor.headers["location"]      # poruka o gresci
    from sefsync.models import Slika

    with db.session_scope() as session:
        assert session.scalar(select(Slika)) is None


def test_rucna_dodela_pj_odmah_prosledjuje(podaci, db):
    """Ko kaze cija je faktura, taj ju je i poslao - dva klika za jednu odluku nema smisla.

    Dok se ne prosledi, poslovodja dokument ni ne vidi, pa ne moze ni da slika
    ambalazu za novu sifru.
    """
    from sefsync.services.workflow import Workflow

    notifier = LazniNotifier()
    Workflow(notifier=notifier).assign_unit(
        podaci["nerazvrstan"], podaci["unit"], actor="nina", remember=False
    )

    with db.session_scope() as session:
        doc = session.get(Document, podaci["nerazvrstan"])
        assert doc.forwarded_at is not None
        assert doc.forwarded_by == "nina"
        assert doc.routing_confirmed_by == "nina"
    assert notifier.pozvano == [podaci["nerazvrstan"]]


def test_vec_prosledjen_dokument_se_ne_salje_ponovo(podaci, db):
    """Ispravka PJ na vec prosledjenom ne sme da okine jos jedan mejl sama od sebe."""
    from sefsync.models import utcnow
    from sefsync.services.workflow import Workflow

    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.forwarded_by = "nina"

    notifier = LazniNotifier()
    Workflow(notifier=notifier).assign_unit(
        podaci["doc"], podaci["druga"], actor="nina", remember=False
    )

    assert notifier.pozvano == []


def test_poslovodja_ima_spisak_sta_treba_slikati(client, podaci, db, monkeypatch):
    """Da ne mora da otvara fakturu po fakturu i pogadja sta je novo."""
    from sefsync.models import utcnow
    from sefsync.services import nacrti

    class LazniNacrt:
        magacin, tabela_zaglavlja, zaglavlje = "002", "MKALKUL", {"IZNOS_ZA_NAP": 1.0}
        problemi: list = []
        nemapirane = [{"line_no": 1, "naziv": "Frižider", "sifra_dobavljaca": "EW-9911",
                       "ean": "860012345", "kolicina": 2.0, "cena": 5.0, "jedinica": "H87"}]
        spreman = False

    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.forwarded_by = "nina"
    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: LazniNacrt())
    nacrti.osvezi(podaci["doc"])

    prijavi(client, POSLOVODJA)
    odgovor = client.get("/moja-jedinica?prikaz=slikanje")

    assert odgovor.status_code == 200
    assert "Frižider" in odgovor.text
    assert "capture=\"environment\"" in odgovor.text     # kamera, ne galerija


def test_covek_se_vezuje_za_jedinicu_spiskom_prijava(client, db):
    """`SifraPovezanogObjekta` deli i LOGISTIKA, pa se u nju ne upisuje nasa sema.

    Ilijana (reklamacije) tamo nema objekat uopste, a treba da vidi magacin 010.
    """
    from sefsync.models import BusinessUnit, UnitKind
    from sefsync.web.auth import Role, User

    with db.session_scope() as session:
        session.add(
            BusinessUnit(code="MAG010", name="Povrat robe", kind=UnitKind.WAREHOUSE,
                         erp_code="010", logins="ilijana", routable=False)
        )
        session.add(
            BusinessUnit(code="MP001", name="SOMBOR", kind=UnitKind.RETAIL, erp_code="001")
        )

    import sefsync.web.app as app_module

    ilijana = User("ilijana", "Krnjajić Ilijana", Role.POSLOVODJA, None, "REKLAMACIJE")
    with db.session_scope() as session:
        jedinica = app_module._unit_for(session, ilijana)
        assert jedinica is not None and jedinica.code == "MAG010"
        assert jedinica.erp_code == "010"      # na kalkulaciji ostaje ERP sifra

    # ko nije na spisku, i dalje se nalazi po sifri objekta
    dane = User("dane", "Sunajko Dane", Role.POSLOVODJA, "001", "POSLOVODJA")
    with db.session_scope() as session:
        assert app_module._unit_for(session, dane).code == "MP001"


# --------------------------------------------------------------------------- #
# predlog „možda je već u ERP-u" - odlučuje čovek, ne aplikacija
# --------------------------------------------------------------------------- #


def _sa_sumnjom(db, doc_id: int) -> None:
    from sefsync.models import NacrtKalkulacije, StanjeNacrta

    with db.session_scope() as session:
        session.add(NacrtKalkulacije(
            document_id=doc_id, stanje=StanjeNacrta.SPREMAN, magacin="088", tabela="ULKALKUL",
            sumnja={"oznaka": "ULKALKUL 088/02874",
                    "razlog": "isti dobavljač, isti datum, isti iznos; broj „26-30F-08705” "
                              "— razlika u jednom znaku"},
        ))


def test_potvrdjen_predlog_sklanja_fakturu_i_ostavlja_trag(client, podaci, db):
    """„Jeste" označava fakturu kao već unetu u ERP, sa tragom ko je rekao."""
    from sefsync.models import NacrtKalkulacije, StanjeNacrta

    _sa_sumnjom(db, podaci["doc"])
    prijavi(client, OPERATER)
    odgovor = client.post(f"/dokument/{podaci['doc']}/sumnja", data={"odluka": "jeste"},
                          follow_redirects=False)
    assert odgovor.status_code in (302, 303)

    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci["doc"])
        assert red.stanje is StanjeNacrta.UPISAN
        assert red.erp_broj == "ULKALKUL 088/02874"
        assert len(red.erp_broj) <= 20          # kolona je varchar(20)
        assert "bogdan" in (red.erp_napomena or "")
        assert red.sumnja is None


def test_odbijen_predlog_ostavlja_fakturu_na_ekranu(client, podaci, db):
    """„Nije to" ne sme ništa da skloni - faktura ostaje da se zaknjiži."""
    from sefsync.models import NacrtKalkulacije, StanjeNacrta

    _sa_sumnjom(db, podaci["doc"])
    prijavi(client, OPERATER)
    client.post(f"/dokument/{podaci['doc']}/sumnja", data={"odluka": "nije"},
                follow_redirects=False)

    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci["doc"])
        assert red.stanje is StanjeNacrta.SPREMAN
        assert red.erp_broj is None
        assert red.sumnja_odbio == "bogdan"
        assert red.sumnja_odbijeno_u is not None
        # predlog se pamti da se ne vrati pri sledecem osvezavanju
        assert red.sumnja["oznaka"] == "ULKALKUL 088/02874"


def test_tudj_dokument_ne_prima_odluku_o_sumnji(client, podaci, db):
    """Poslovođa drugog objekta ne odlučuje o tuđoj fakturi."""
    _sa_sumnjom(db, podaci["doc"])
    prijavi(client, TUDJ)
    odgovor = client.post(f"/dokument/{podaci['doc']}/sumnja", data={"odluka": "jeste"},
                          follow_redirects=False)
    assert odgovor.status_code == 403


# --------------------------------------------------------------------------- #
# storno i odbijanje zaustavljaju upis; štampa vraća SEF-ov PDF
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status, deo_poruke",
    [(SefStatus.STORNO, "stornirana"), (SefStatus.REJECTED, "odbijena")],
)
def test_povucena_faktura_ne_ide_u_erp(podaci, db, status, deo_poruke):
    """Faktura povučena sa SEF-a nema šta da traži u ERP-u."""
    from sefsync.models import Document as D
    from sefsync.services.nacrti import upisi_u_erp

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).sef_status = status

    upisana, poruka = upisi_u_erp(podaci["doc"], korisnik="nina")
    assert not upisana
    assert deo_poruke in poruka


def test_stampa_vraca_sefov_pdf(client, podaci, db, monkeypatch):
    """Na papiru stoji ono što je dobavljač poslao, ne naš prikaz."""
    import sefsync.sef.client as sef_client

    class LazniKlijent:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def purchase_pdf(self, invoice_id):
            assert invoice_id == 500100
            return b"%PDF-1.4 lazni"

    monkeypatch.setattr(sef_client, "SefClient", LazniKlijent)
    prijavi(client, OPERATER)
    odgovor = client.get(f"/dokument/{podaci['doc']}/pdf")
    assert odgovor.status_code == 200
    assert odgovor.headers["content-type"] == "application/pdf"
    assert odgovor.content.startswith(b"%PDF")


def test_tudj_dokument_se_ne_stampa(client, podaci, db):
    prijavi(client, TUDJ)
    assert client.get(f"/dokument/{podaci['doc']}/pdf").status_code == 403
