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
    odgovor = client.get("/health").json()
    assert odgovor["status"] == "ok"
    # Stanje bazena je tu da se vidi da li veze cure - bez uzimanja ijedne.
    assert "u_upotrebi" in odgovor["panel"]
    assert "erp" in odgovor


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
    # Naslov je namerno kratak i bez egzotičnih znakova — svaki takav znak
    # preseca naslov na još jedan =?utf-8?q?…?= odsečak, a Gmail na Androidu
    # ih loše spaja.
    assert naslov.startswith("Prijem potvrđen:")
    assert "MP002" in naslov


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


def test_ponovna_dodela_iste_jedinice_ne_salje_mejl(podaci, db):
    """Kad operater samo hoće da zapamti pravilo, poslovođa ne treba da dobije mejl."""
    from sefsync.models import utcnow
    from sefsync.services.workflow import Workflow

    with db.session_scope() as session:
        doc = session.get(Document, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.forwarded_by = "nina"

    notifier = LazniNotifier()
    Workflow(notifier=notifier).assign_unit(
        podaci["doc"], podaci["unit"], actor="nina", remember=False
    )

    assert notifier.pozvano == []


def test_prebacivanje_obavestava_novu_jedinicu(podaci, db):
    """Mereno 29.09.: 49 dokumenata prebačeno posle prosleđivanja, niko obavešten.

    Nova jedinica o dokumentu ne zna ništa dok joj se ne javi.
    """
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

    assert notifier.pozvano == [podaci["doc"]]


def test_poslovodja_ima_spisak_sta_treba_slikati(client, podaci, db, monkeypatch):
    """Da ne mora da otvara fakturu po fakturu i pogadja sta je novo."""
    from sefsync.models import utcnow
    from sefsync.services import nacrti

    class LazniNacrt:
        magacin, tabela_zaglavlja, zaglavlje = "002", "MKALKUL", {"IZNOS_ZA_NAP": 1.0}
        problemi: list = []
        nemapirane = [{"line_no": 1, "naziv": "Frižider", "sifra_dobavljaca": "EW-9911",
                       "ean": "860012345", "kolicina": 2.0, "cena": 5.0, "jedinica": "H87"}]
        napomene: list = []
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


# --------------------------------------------------------------------------- #
# manjak pri isporuci: stiglo je manje nego što faktura kaže
# --------------------------------------------------------------------------- #


def _manjak_forma(odluka: str, stiglo: str = "1"):
    """Poslovođa dira samo red gde ne valja; ostali idu neizmenjeni."""
    return {"odluka": odluka, "line_no": ["1"], "stiglo": [stiglo]}


def test_dovoze_sledecom_turom_upisuje_punu_fakturu(client, podaci, db, monkeypatch):
    """Kalkulacija ide za pun iznos — za manju količinu se ne bi složila sa PDVEVIDX."""
    from sefsync.models import Document as D, OdlukaManjka, PrijavaManjka

    upisano = []
    monkeypatch.setattr("sefsync.services.nacrti.upisi_u_erp",
                        lambda doc_id, **k: (upisano.append(doc_id), (True, "Upisana."))[1])
    monkeypatch.setattr(get_settings(), "erp_upis_kalkulacije", True, raising=False)

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow_test()

    prijavi(client, POSLOVODJA)
    odgovor = client.post(f"/dokument/{podaci['doc']}/manjak",
                          data=_manjak_forma("kasnije"), follow_redirects=False)
    assert odgovor.status_code in (302, 303)

    with db.session_scope() as session:
        m = session.get(PrijavaManjka, podaci["doc"])
        assert m is not None and m.otvoren
        assert m.odluka is OdlukaManjka.KASNIJE
        assert m.stavke == [{"line_no": 1, "naziv": "Frižider",
                             "fakturisano": 2.0, "stiglo": 1.0}]
        assert m.prijavio == "predrag"
        assert session.get(D, podaci["doc"]).received_at is not None
    assert upisano == [podaci["doc"]]


@pytest.mark.parametrize("odluka", ["storno", "operater"])
def test_dok_se_ne_zna_sta_je_sa_fakturom_nema_upisa(client, podaci, db, monkeypatch, odluka):
    """Faktura tek treba da nestane ili da se o njoj odluči — ERP čeka."""
    from sefsync.models import Document as D, PrijavaManjka

    upisano = []
    monkeypatch.setattr("sefsync.services.nacrti.upisi_u_erp",
                        lambda doc_id, **k: (upisano.append(doc_id), (True, "Upisana."))[1])
    monkeypatch.setattr(get_settings(), "erp_upis_kalkulacije", True, raising=False)

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow_test()

    prijavi(client, POSLOVODJA)
    client.post(f"/dokument/{podaci['doc']}/manjak",
                data=_manjak_forma(odluka), follow_redirects=False)

    with db.session_scope() as session:
        assert session.get(PrijavaManjka, podaci["doc"]) is not None
    assert upisano == []


def test_kolicina_veca_od_fakturisane_se_odbija(client, podaci, db):
    """Ovo je manjak, ne višak — 3 od 2 nema smisla."""
    from sefsync.models import Document as D, PrijavaManjka

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow_test()
    prijavi(client, POSLOVODJA)
    client.post(f"/dokument/{podaci['doc']}/manjak",
                data=_manjak_forma("kasnije", stiglo="3"), follow_redirects=False)
    with db.session_scope() as session:
        assert session.get(PrijavaManjka, podaci["doc"]) is None


def test_stigao_ostatak_skida_crvenu_liniju(client, podaci, db):
    from sefsync.models import Document as D, OdlukaManjka, PrijavaManjka

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow_test()
        session.add(PrijavaManjka(
            document_id=podaci["doc"], odluka=OdlukaManjka.KASNIJE, prijavio="predrag",
            stavke=[{"line_no": 1, "naziv": "Frižider", "fakturisano": 2.0, "stiglo": 1.0}],
        ))

    prijavi(client, POSLOVODJA)
    client.post(f"/dokument/{podaci['doc']}/manjak-zatvori",
                data={"kako": "stigao ostatak"}, follow_redirects=False)

    with db.session_scope() as session:
        m = session.get(PrijavaManjka, podaci["doc"])
        assert not m.otvoren
        assert m.zatvorio == "predrag"
        assert m.zatvoreno_kako == "stigao ostatak"


def test_tudja_jedinica_ne_prijavljuje_manjak(client, podaci, db):
    prijavi(client, TUDJ)
    odgovor = client.post(f"/dokument/{podaci['doc']}/manjak",
                          data=_manjak_forma("kasnije"), follow_redirects=False)
    assert odgovor.status_code == 403


def utcnow_test():
    from sefsync.models import utcnow

    return utcnow()


def test_knjizno_odobrenje_zatvara_manjak(podaci, db):
    """Ostatak nikad nije stigao — odobrenje skida crvenu liniju samo."""
    from sefsync.models import Document as D, DocumentType, OdlukaManjka, PrijavaManjka
    from sefsync.services.odobrenja import zatvori_manjak_odobrenjem

    with db.session_scope() as session:
        faktura = session.get(D, podaci["doc"])
        session.add(PrijavaManjka(
            document_id=faktura.id, odluka=OdlukaManjka.KASNIJE, prijavio="predrag",
            stavke=[{"line_no": 1, "naziv": "Frižider", "fakturisano": 2.0, "stiglo": 1.0}],
        ))
        # dobavljac broj kuca kako mu dodje - poredi se normalizovano
        odobrenje = D(
            sef_invoice_id=500300, document_number="KO-77/26",
            document_type=DocumentType.CREDIT_NOTE,
            supplier_name="EWE COMP DOO", supplier_vat="100042618",
            billing_reference="26 30F 087050", amount=-50000.0,
        )
        session.add(odobrenje)
        session.flush()
        odobrenje_id = odobrenje.id

    assert zatvori_manjak_odobrenjem(odobrenje_id) == "26-30F-087050"

    with db.session_scope() as session:
        m = session.get(PrijavaManjka, podaci["doc"])
        assert not m.otvoren
        assert m.zatvoreno_kako == "stiglo knjižno odobrenje"
        assert "KO-77/26" in m.zatvorio


def test_odobrenje_drugog_dobavljaca_ne_zatvara_tudji_manjak(podaci, db):
    from sefsync.models import Document as D, DocumentType, OdlukaManjka, PrijavaManjka
    from sefsync.services.odobrenja import zatvori_manjak_odobrenjem

    with db.session_scope() as session:
        session.add(PrijavaManjka(
            document_id=podaci["doc"], odluka=OdlukaManjka.KASNIJE, prijavio="predrag",
            stavke=[{"line_no": 1, "naziv": "Frižider", "fakturisano": 2.0, "stiglo": 1.0}],
        ))
        drugi = D(
            sef_invoice_id=500400, document_number="KO-99/26",
            document_type=DocumentType.CREDIT_NOTE,
            supplier_name="NEKO DRUGI", supplier_vat="999999999",
            billing_reference="26-30F-087050", amount=-50000.0,
        )
        session.add(drugi)
        session.flush()
        drugi_id = drugi.id

    assert zatvori_manjak_odobrenjem(drugi_id) is None
    with db.session_scope() as session:
        assert session.get(PrijavaManjka, podaci["doc"]).otvoren


# --------------------------------------------------------------------------- #
# pomoć na ekranu
# --------------------------------------------------------------------------- #


def test_pomoc_operateru_pokriva_njegov_posao(client, db):
    prijavi(client, OPERATER)
    odgovor = client.get("/pomoc")
    assert odgovor.status_code == 200
    tekst = odgovor.text
    assert "kancelarija" in tekst
    assert "Prijavljen manjak" in tekst
    assert "Dovoze sledećom turom" in tekst
    # ne sme da mu se prikaze tudji deo
    assert "Za slikanje" not in tekst


def test_pomoc_poslovodji_objasnjava_manjak(client, podaci, db):
    prijavi(client, POSLOVODJA)
    odgovor = client.get("/pomoc")
    assert odgovor.status_code == 200
    tekst = odgovor.text
    assert "Kad roba stigne" in tekst
    assert "Stigao ostatak" in tekst
    assert "Za slikanje" in tekst
    # operaterski deo mu ne treba
    assert "Prihvatanje i odbijanje na SEF-u" not in tekst


def test_pomoc_stoji_na_svakom_ekranu(client, podaci, db):
    """Veza ka pomoći je u zaglavlju, pa je ima gde god čovek bio."""
    prijavi(client, OPERATER)
    for putanja in ("/red", "/pj", "/pravila"):
        assert 'href="/pomoc"' in client.get(putanja).text


def test_pomoc_tazi_prijavu(client, db):
    import sefsync.web.app as app_module

    app_module.app.dependency_overrides.clear()
    assert client.get("/pomoc", follow_redirects=False).status_code in (302, 303, 401, 403)


def test_pomoc_ilijani_i_bogdanu_daje_njihov_deo(client, db):
    """010 i uprava imaju svoje ekrane, pa i svoju pomoć."""
    from sefsync.models import BusinessUnit as BU, UnitKind as UK

    with db.session_scope() as session:
        session.add_all([
            BU(code="MAG010", name="POVRAĆAJ ROBE", kind=UK.WAREHOUSE, erp_code="010"),
            BU(code="OFFICE", name="UPRAVA — FINANSIJE", kind=UK.HQ),
        ])

    # covek se vezuje za jedinicu sifrom objekta iz sifarnika osoblja
    ilijana = User("ilijana", "Ilijana", Role.POSLOVODJA, "010", "POSLOVODJA")
    prijavi(client, ilijana)
    tekst = client.get("/pomoc").text
    assert "povrati i odobrenja" in tekst
    assert "Popusti za prenos" in tekst
    assert "Kad roba stigne" not in tekst          # to je posao objekta

    # 088 u sifarniku osoblja znaci "radi ulazne racune" i vodi na upravu
    bogdan = User("bogdan", "Sunajko Bogdan", Role.POSLOVODJA, "088", "FINANSIJE")
    prijavi(client, bogdan)
    tekst = client.get("/pomoc").text
    assert "ulazni računi" in tekst
    assert "Upiši kalkulaciju" in tekst
    assert "Popusti za prenos" not in tekst        # to je Ilijanin posao


# --------------------------------------------------------------------------- #
# granica prelaska i arhiva ne smeju da obećavaju posao koji ne postoji
# --------------------------------------------------------------------------- #


def test_brojac_ne_racuna_fakture_od_pre_prelaska(client, podaci, db, monkeypatch):
    """Bogdanu je pisalo „8 spremno za upis“, a svaki red je govorio „po starom“."""
    import datetime as dt

    from sefsync.models import Document as D, NacrtKalkulacije, StanjeNacrta, utcnow

    with db.session_scope() as session:
        doc = session.get(D, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.created_at = dt.datetime(2026, 9, 20, 10, 0)     # pre prelaska
        session.add(NacrtKalkulacije(document_id=doc.id, stanje=StanjeNacrta.SPREMAN))

    monkeypatch.setattr(get_settings(), "novi_tok_od", dt.date(2026, 9, 28), raising=False)
    prijavi(client, POSLOVODJA)
    tekst = client.get("/moja-jedinica?prikaz=novo").text
    assert "Za potvrdu <b>0</b>" in tekst

    # bez granice se broji kao i ranije
    monkeypatch.setattr(get_settings(), "novi_tok_od", None, raising=False)
    assert "Za potvrdu <b>1</b>" in client.get("/moja-jedinica?prikaz=novo").text


def test_arhivirano_ne_stoji_poslovodji_na_ekranu(client, podaci, db):
    """Čišćenje koje rade Nina i Dušan mora da očisti i objekte."""
    from sefsync.models import Document as D, utcnow

    with db.session_scope() as session:
        doc = session.get(D, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.received_at = utcnow()
        doc.received_by = "bane"          # probna potvrda iz testiranja
        broj = doc.document_number

    prijavi(client, POSLOVODJA)
    assert broj in client.get("/moja-jedinica?prikaz=sve").text

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).archived = True

    assert broj not in client.get("/moja-jedinica?prikaz=sve").text


def test_arhiva_je_dostupna_poslovodji(client, podaci, db):
    """Ako sklanjamo fakture sa ekrana, mora da postoji gde da se pogledaju."""
    from sefsync.models import Document as D, utcnow

    with db.session_scope() as session:
        doc = session.get(D, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.archived = True
        broj = doc.document_number

    prijavi(client, POSLOVODJA)
    assert broj not in client.get("/moja-jedinica?prikaz=sve").text
    assert broj in client.get("/moja-jedinica?prikaz=arhiva").text


def test_predlog_se_vidi_i_na_fakturi_od_pre_prelaska(client, db, monkeypatch):
    """Baš te su najčešće već unete rukom — tu je predlog najpotrebniji."""
    import datetime as dt

    from sefsync.models import (
        BusinessUnit as BU, Document as D, NacrtKalkulacije, SefStatus,
        StanjeNacrta, UnitKind as UK,
    )

    with db.session_scope() as session:
        uprava = BU(code="OFFICE", name="UPRAVA — FINANSIJE", kind=UK.HQ)
        session.add(uprava)
        session.flush()
        doc = D(
            sef_invoice_id=500900, document_number="OTP1882131",
            supplier_name="OTP BANKA", supplier_vat="100000000",
            amount=180.18, sef_status=SefStatus.NEW, business_unit_id=uprava.id,
            created_at=dt.datetime(2026, 9, 20, 9, 0),      # pre prelaska
        )
        session.add(doc)
        session.flush()
        session.add(NacrtKalkulacije(
            document_id=doc.id, stanje=StanjeNacrta.SPREMAN, magacin="088",
            sumnja={"oznaka": "ULKALKUL 088/02874", "razlog": "razlika u jednom znaku"},
        ))

    monkeypatch.setattr(get_settings(), "novi_tok_od", dt.date(2026, 9, 28), raising=False)
    bogdan = User("bogdan", "Sunajko Bogdan", Role.POSLOVODJA, "088", "FINANSIJE")
    prijavi(client, bogdan)
    tekst = client.get("/moja-jedinica").text

    assert "Stiglo pre prelaska" in tekst          # i dalje pise da ide po starom
    assert "ULKALKUL 088/02874" in tekst           # ali predlog se vidi
    assert "Jeste, skloni" in tekst


# --------------------------------------------------------------------------- #
# ista prijava uz više jedinica — ishod ne sme da zavisi od redosleda
# --------------------------------------------------------------------------- #


def test_prijava_uz_vise_jedinica_daje_uvek_istu(client, db, caplog):
    """Bogdan je upisao kalkulaciju, pa je ista provera sekund kasnije vratila 403.

    Uzrok je bio upit bez ORDER BY: MS SQL sme da vrati redove drugim redosledom
    pri svakom pozivu, pa je isti čovek dobijao čas jednu čas drugu jedinicu.
    """
    import sefsync.web.app as app_module
    from sefsync.models import BusinessUnit as BU, UnitKind as UK

    with db.session_scope() as session:
        session.add_all([
            BU(code="MAG088", name="BEZ EVIDENCIJE", kind=UK.WAREHOUSE,
               erp_code="088", logins="bogdan"),
            BU(code="OFFICE", name="UPRAVA — FINANSIJE", kind=UK.HQ, logins="bogdan"),
        ])

    bogdan = User("bogdan", "Sunajko Bogdan", Role.POSLOVODJA, "088", "FINANSIJE")
    with db.session_scope() as session:
        izbori = {app_module._unit_for(session, bogdan).code for _ in range(5)}
    # uvek isti izbor, i to uprava - MAG088 nema nijedan dokument
    assert izbori == {"OFFICE"}, "izbor mora da bude isti pri svakom pozivu"

    # dvosmislenost se prijavljuje, da se šifarnik očisti
    assert any("upisana uz više jedinica" in z.message for z in caplog.records)


def test_bez_dvosmislenosti_088_vodi_na_upravu(client, db):
    """Šifra 088 u šifarniku osoblja znači „radi ulazne račune“ — to je uprava."""
    import sefsync.web.app as app_module
    from sefsync.models import BusinessUnit as BU, UnitKind as UK

    with db.session_scope() as session:
        session.add_all([
            BU(code="MAG088", name="BEZ EVIDENCIJE", kind=UK.WAREHOUSE, erp_code="088"),
            BU(code="OFFICE", name="UPRAVA — FINANSIJE", kind=UK.HQ),
        ])

    bogdan = User("bogdan", "Sunajko Bogdan", Role.POSLOVODJA, "088", "FINANSIJE")
    with db.session_scope() as session:
        assert app_module._unit_for(session, bogdan).code == "OFFICE"


# --------------------------------------------------------------------------- #
# usluge i povrati nemaju prosleđivanje — ne smeju da traže forwarded_at
# --------------------------------------------------------------------------- #


def test_bogdan_otvara_svoju_fakturu_bez_prosledjivanja(client, db):
    """Stranica dokumenta je tražila forwarded_at, a 088 se nikad ne prosleđuje."""
    from sefsync.models import (
        BusinessUnit as BU, Document as D, SefStatus, UnitKind as UK,
    )

    with db.session_scope() as session:
        uprava = BU(code="OFFICE", name="UPRAVA — FINANSIJE", kind=UK.HQ)
        session.add(uprava)
        session.flush()
        doc = D(sef_invoice_id=501000, document_number="BK7626794/26",
                supplier_name="BANCA INTESA", amount=117.0,
                sef_status=SefStatus.NEW, business_unit_id=uprava.id)
        session.add(doc)
        session.flush()
        doc_id = doc.id

    bogdan = User("bogdan", "Sunajko Bogdan", Role.POSLOVODJA, "088", "FINANSIJE")
    prijavi(client, bogdan)
    assert client.get(f"/dokument/{doc_id}").status_code == 200


def test_objekat_i_dalje_ceka_prosledjivanje(client, podaci, db):
    """U prodavnici dokument stiže tek kad ga operater prosledi."""
    from sefsync.models import Document as D, utcnow

    prijavi(client, POSLOVODJA)
    assert client.get(f"/dokument/{podaci['doc']}").status_code == 403

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow()

    assert client.get(f"/dokument/{podaci['doc']}").status_code == 200


# --------------------------------------------------------------------------- #
# prihvatanje na SEF-u posle upisa — samo kompletne fakture
# --------------------------------------------------------------------------- #


class _LazniPrihvat:
    """Belezi koga bi prihvatio, bez odlaska na SEF."""

    prihvaceni: list = []

    def __init__(self, *a, **k):
        pass

    def accept(self, doc_id, actor="system", comment=""):
        from sefsync.services.acceptance import AcceptResult

        type(self).prihvaceni.append((doc_id, actor))
        return AcceptResult(True, "Prihvaćeno.")


def _pripremi_prihvat(monkeypatch):
    import sefsync.services.acceptance as acc

    _LazniPrihvat.prihvaceni = []
    monkeypatch.setattr(acc, "AcceptanceService", _LazniPrihvat)
    monkeypatch.setattr(get_settings(), "prihvati_posle_upisa", True, raising=False)


def test_kompletna_faktura_se_prihvata_posle_upisa(podaci, db, monkeypatch):
    from sefsync.services.nacrti import prihvati_ako_je_kompletna

    _pripremi_prihvat(monkeypatch)
    poruka = prihvati_ako_je_kompletna(podaci["doc"], "bogdan")
    assert "Prihvaćena je i na SEF-u" in poruka
    assert _LazniPrihvat.prihvaceni == [(podaci["doc"], "bogdan")]


def test_faktura_sa_otvorenim_manjkom_se_ne_prihvata(podaci, db, monkeypatch):
    """Dok se ne zna hoće li ostatak stići, mora da ostane moguće odbijanje."""
    from sefsync.models import OdlukaManjka, PrijavaManjka
    from sefsync.services.nacrti import prihvati_ako_je_kompletna

    _pripremi_prihvat(monkeypatch)
    with db.session_scope() as session:
        session.add(PrijavaManjka(
            document_id=podaci["doc"], odluka=OdlukaManjka.KASNIJE, prijavio="predrag",
            stavke=[{"line_no": 1, "naziv": "Frižider", "fakturisano": 2.0, "stiglo": 1.0}],
        ))

    poruka = prihvati_ako_je_kompletna(podaci["doc"], "predrag")
    assert "nije prihvaćena" in poruka
    assert _LazniPrihvat.prihvaceni == []


def test_zatvaranje_manjka_prihvata_fakturu(client, podaci, db, monkeypatch):
    """Nekompletna koja postane kompletna — tada se prihvata."""
    from sefsync.models import (
        Document as D, NacrtKalkulacije, OdlukaManjka, PrijavaManjka, StanjeNacrta, utcnow,
    )
    from sefsync.services.nacrti import prihvati_ako_je_kompletna  # noqa: F401

    _pripremi_prihvat(monkeypatch)
    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow()
        session.add(PrijavaManjka(
            document_id=podaci["doc"], odluka=OdlukaManjka.KASNIJE, prijavio="predrag",
            stavke=[{"line_no": 1, "naziv": "Frižider", "fakturisano": 2.0, "stiglo": 1.0}],
        ))
        session.add(NacrtKalkulacije(
            document_id=podaci["doc"], stanje=StanjeNacrta.UPISAN, erp_broj="021/00286",
        ))

    prijavi(client, POSLOVODJA)
    client.post(f"/dokument/{podaci['doc']}/manjak-zatvori",
                data={"kako": "stigao ostatak"}, follow_redirects=False)

    assert _LazniPrihvat.prihvaceni == [(podaci["doc"], "predrag")]


def test_bez_podesavanja_se_ne_prihvata_nista(podaci, db, monkeypatch):
    from sefsync.services.nacrti import prihvati_ako_je_kompletna

    _pripremi_prihvat(monkeypatch)
    monkeypatch.setattr(get_settings(), "prihvati_posle_upisa", False, raising=False)
    assert prihvati_ako_je_kompletna(podaci["doc"], "bogdan") == ""
    assert _LazniPrihvat.prihvaceni == []


def test_health_kaze_ko_drzi_vezu(client, db):
    """Bazen se dvaput napunio u produkciji, a iz koda se nije videlo ko ga drži."""
    from sefsync.db import ko_drzi_veze, session_scope
    from sefsync.models import Document

    with session_scope() as session:
        session.query(Document).count()
        drze = ko_drzi_veze()
        assert drze, "dok je sesija otvorena, veza mora da se vidi"
        assert "sekundi" in drze[0] and "gde" in drze[0]

    # zatvorena sesija vraća vezu — inače merač ne bi vredeo ništa
    assert ko_drzi_veze() == []


def test_brojanje_ne_zamotava_ceo_red_u_podupit(client, podaci, db):
    """Stranica reda je brojala tako što izvuče sve kolone pa prebroji.

    Na 8.000 dokumenata to je držalo vezu ka bazi pedesetak sekundi, sedam puta
    po otvaranju stranice — dok ljudi nisu mogli da se prijave.
    """
    import sefsync.web.app as app_module
    from sqlalchemy import select

    from sefsync.models import Document

    upit = app_module._prebroj(app_module._queue_filter(select(Document), "red"))
    sql = str(upit)
    assert "count(" in sql.lower()
    assert "anon_1" not in sql, "ne sme da se zamotava u podupit"

    with db.session_scope() as session:
        assert session.scalar(upit) is not None


def test_svaka_uloga_zna_da_njen_klik_prihvata_na_sefu(client, db):
    """Prihvatanje je nepovratno — ne sme da piše samo operaterima."""
    from sefsync.models import BusinessUnit as BU, UnitKind as UK

    with db.session_scope() as session:
        session.add_all([
            BU(code="MAG010", name="POVRAĆAJ ROBE", kind=UK.WAREHOUSE, erp_code="010"),
            BU(code="OFFICE", name="UPRAVA — FINANSIJE", kind=UK.HQ),
            BU(code="MP002", name="PRODAJNO MESTO APATIN", kind=UK.RETAIL, erp_code="002"),
        ])

    ljudi = [
        User("predrag", "Bajic Predrag", Role.POSLOVODJA, "002", "POSLOVODJA"),
        User("ilijana", "Ilijana", Role.POSLOVODJA, "010", "POSLOVODJA"),
        User("bogdan", "Sunajko Bogdan", Role.POSLOVODJA, "088", "FINANSIJE"),
        OPERATER,
    ]
    for covek in ljudi:
        prijavi(client, covek)
        tekst = client.get("/pomoc").text
        assert "prihvata" in tekst.lower(), f"{covek.login} ne vidi ništa o prihvatanju"
        assert "nepovratno" in tekst.lower() or "ne radi sama" in tekst.lower()


# --------------------------------------------------------------------------- #
# prebacivanje dokumenta posle prosleđivanja / upisa / prijema
# --------------------------------------------------------------------------- #


def test_prebacivanje_posle_upisa_ostavlja_upozorenje(podaci, db):
    """App ne dira ERP, ali ne sme ni da ćuti da kalkulacija stoji u tuđem magacinu."""
    from sefsync.models import NacrtKalkulacije, StanjeNacrta
    from sefsync.services.workflow import Workflow

    with db.session_scope() as session:
        session.add(NacrtKalkulacije(
            document_id=podaci["doc"], stanje=StanjeNacrta.UPISAN,
            erp_broj="002/00471", magacin="002",
        ))

    Workflow(notifier=LazniNotifier()).assign_unit(
        podaci["doc"], podaci["druga"], actor="nina", remember=False
    )

    with db.session_scope() as session:
        red = session.get(NacrtKalkulacije, podaci["doc"])
        assert red.upozorenje
        assert "002/00471" in red.upozorenje
        assert "MP001" in red.upozorenje      # sada pripada drugoj jedinici


def test_prebacivanje_ponistava_tudju_potvrdu_prijema(podaci, db):
    """Nova jedinica mora sama da potvrdi da je roba kod nje."""
    from sefsync.models import AuditLog, Document as D, utcnow
    from sefsync.services.workflow import Workflow

    with db.session_scope() as session:
        doc = session.get(D, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.received_at = utcnow()
        doc.received_by = "predrag"

    Workflow(notifier=LazniNotifier()).assign_unit(
        podaci["doc"], podaci["druga"], actor="nina", remember=False
    )

    with db.session_scope() as session:
        doc = session.get(D, podaci["doc"])
        assert doc.received_at is None
        assert doc.received_by is None
        akcije = [a.action for a in session.query(AuditLog).all()]
        assert "document.prijem.ponisten" in akcije


def test_prebacivanje_bez_upisa_ne_dize_uzbunu(podaci, db):
    """Dok kalkulacije nema, prebacivanje je sasvim obična ispravka."""
    from sefsync.models import NacrtKalkulacije, StanjeNacrta
    from sefsync.services.workflow import Workflow

    with db.session_scope() as session:
        session.add(NacrtKalkulacije(
            document_id=podaci["doc"], stanje=StanjeNacrta.SPREMAN, magacin="002",
        ))

    Workflow(notifier=LazniNotifier()).assign_unit(
        podaci["doc"], podaci["druga"], actor="nina", remember=False
    )

    with db.session_scope() as session:
        assert session.get(NacrtKalkulacije, podaci["doc"]).upozorenje is None


def test_poslovodja_vraca_tudji_dokument_kancelariji(client, podaci, db):
    """Do sada je jedino rešenje bilo javiti telefonom — bez ikakvog traga."""
    from sefsync.models import AuditLog, Document as D, ProcessState, utcnow

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow()

    prijavi(client, POSLOVODJA)
    client.post(f"/dokument/{podaci['doc']}/nije-moje",
                data={"napomena": "ovo je za Ilijanu"}, follow_redirects=False)

    with db.session_scope() as session:
        doc = session.get(D, podaci["doc"])
        assert doc.business_unit_id is None
        assert doc.forwarded_at is None
        assert doc.state is ProcessState.UNASSIGNED
        assert "ovo je za Ilijanu" in doc.routing_note
        assert "MP002" in doc.routing_note          # odakle je vraćen
        assert "document.vracen" in [a.action for a in session.query(AuditLog).all()]


def test_ne_vraca_se_ono_sto_je_vec_primljeno(client, podaci, db):
    """Tu je već nešto urađeno — to rešava kancelarija, ne dugme."""
    from sefsync.models import Document as D, utcnow

    with db.session_scope() as session:
        doc = session.get(D, podaci["doc"])
        doc.forwarded_at = utcnow()
        doc.received_at = utcnow()
        doc.received_by = "predrag"

    prijavi(client, POSLOVODJA)
    client.post(f"/dokument/{podaci['doc']}/nije-moje", data={}, follow_redirects=False)

    with db.session_scope() as session:
        assert session.get(D, podaci["doc"]).business_unit_id is not None


def test_ne_vraca_se_ono_sto_je_vec_u_erpu(client, podaci, db):
    from sefsync.models import Document as D, NacrtKalkulacije, StanjeNacrta, utcnow

    with db.session_scope() as session:
        session.get(D, podaci["doc"]).forwarded_at = utcnow()
        session.add(NacrtKalkulacije(
            document_id=podaci["doc"], stanje=StanjeNacrta.UPISAN, erp_broj="002/00471",
        ))

    prijavi(client, POSLOVODJA)
    client.post(f"/dokument/{podaci['doc']}/nije-moje", data={}, follow_redirects=False)

    with db.session_scope() as session:
        assert session.get(D, podaci["doc"]).business_unit_id is not None


# --------------------------------------------------------------------------- #
# „zapamti kao pravilo" ne sme da pravi pravila koja se bore
# --------------------------------------------------------------------------- #


def test_zapamti_preusmerava_postojece_pravilo(podaci, db):
    """Ista adresa je vodila na deset jedinica — jer se svaki put dodavalo novo."""
    from sqlalchemy import select

    from sefsync.models import MatchField, RoutingRule
    from sefsync.services.workflow import Workflow

    Workflow(notifier=LazniNotifier()).assign_unit(
        podaci["doc"], podaci["unit"], actor="nina", remember=True,
        field_name=MatchField.DELIVERY_ADDRESS.value,
    )
    # isti dokument, ista adresa, druga jedinica — operater se predomislio
    Workflow(notifier=LazniNotifier()).assign_unit(
        podaci["doc"], podaci["druga"], actor="nina", remember=True,
        field_name=MatchField.DELIVERY_ADDRESS.value,
    )

    with db.session_scope() as session:
        pravila = list(session.scalars(
            select(RoutingRule).where(RoutingRule.active == True)  # noqa: E712
        ))
        adresna = [p for p in pravila if p.field is MatchField.DELIVERY_ADDRESS]
        assert len(adresna) == 1, "za isti obrazac sme da stoji samo jedno pravilo"
        assert adresna[0].business_unit_id == podaci["druga"]


def test_jedinica_bez_mejla_kaze_sta_fali(podaci, db):
    """„Slanje nije uspelo" ne kaže čoveku šta da uradi."""
    from sefsync.models import BusinessUnit as BU
    from sefsync.services.workflow import Workflow

    with db.session_scope() as session:
        session.get(BU, podaci["unit"]).emails = None

    class NeProlazi:
        def notify_document(self, doc_id):
            return False

        def notify_receipt(self, doc_id, confirmed_by):
            return True

    ishod = Workflow(notifier=NeProlazi()).forward([podaci["doc"]], actor="nina")
    assert not ishod.ok
    poruka = " ".join(ishod.problems)
    assert "nema upisan mejl" in poruka
    assert "MP002" in poruka


def test_prazan_spisak_primalaca_ostavlja_trag(podaci, db, caplog):
    """Bez ovoga u dnevniku nema nijednog reda — čovek nema gde da proveri zašto."""
    import logging

    from sefsync.models import BusinessUnit as BU
    from sefsync.notify.dispatcher import Dispatcher

    with db.session_scope() as session:
        jedinica = session.get(BU, podaci["unit"])
        jedinica.emails = None
        jedinica.phones = None

    with caplog.at_level(logging.WARNING):
        assert Dispatcher().notify_document(podaci["doc"]) is False

    poruke = " ".join(z.getMessage() for z in caplog.records)
    assert "nema nijednu adresu" in poruke
    assert "MP002" in poruke


def test_mejl_se_uzima_iz_sifarnika_osoblja_kad_jedinica_nema_svoj(podaci, db, monkeypatch):
    """Adresa magacina povrata stajala je samo uz osobu, ne i uz jedinicu.

    Zbog toga je prosleđivanje padalo sa „slanje nije uspelo", iako je
    reklamacije@elbraco.rs sve vreme bio upisan uz objekat 010.
    """
    import sefsync.notify.dispatcher as d
    from sefsync.models import BusinessUnit as BU

    with db.session_scope() as session:
        jedinica = session.get(BU, podaci["unit"])
        jedinica.emails = None
        sifra = jedinica.erp_code

    pitano = []

    def iz_sifarnika(objekat):
        pitano.append(objekat)
        return ["reklamacije@elbraco.rs"]

    monkeypatch.setattr(d, "staff_emails_for_unit", iz_sifarnika)

    primaoci = []

    class LazniKanal:
        def send(self, adrese, poruka):
            from sefsync.notify.base import SendResult

            primaoci.extend(adrese)
            return [SendResult(True, a) for a in adrese]

    class NemiPush:
        def send(self, telefoni, poruka, meta):
            return []

    dispecer = d.Dispatcher(email=LazniKanal(), push=NemiPush())
    assert dispecer.notify_document(podaci["doc"]) is True

    assert pitano == [sifra], "šifarnik se pita baš za šifru objekta"
    assert primaoci == ["reklamacije@elbraco.rs"]


def test_zaustavljene_kalkulacije_se_broje_u_navigaciji(client, podaci, db):
    """Stoje pri dnu ekrana „Šifre" i lako se propuste — a roba dotle stoji."""
    from sefsync.models import NacrtKalkulacije, StanjeNacrta

    prijavi(client, OPERATER)
    assert "Šifre <b" not in client.get("/red").text      # dok ih nema, nema ni broja

    with db.session_scope() as session:
        session.add(NacrtKalkulacije(
            document_id=podaci["doc"], stanje=StanjeNacrta.PROBLEM, magacin="002",
            problemi=["stavka 1: artikal 87606 nema cenu u objektu 002"],
        ))

    tekst = client.get("/red").text
    assert "Šifre <b" in tekst and "(1)" in tekst

    # i na samom ekranu, sa razlogom
    sifre = client.get("/sifre").text
    assert "nema cenu u objektu 002" in sifre
    assert "maloprodajnu cenu" in sifre       # objašnjenje imenuje pravi uzrok


# --------------------------------------------------------------------------- #
# obaveštenje o prethodnom porezu — uz prihvatanje knjižnog odobrenja
# --------------------------------------------------------------------------- #


def _odobrenje(db, **izmene):
    from sefsync.models import Document as D, DocumentType

    podrazumevano = dict(
        sef_invoice_id=7001, document_number="26-3X3-002855",
        document_type=DocumentType.CREDIT_NOTE,
        supplier_name="ERG DOO ŠIMANOVCI", supplier_vat="103034716",
        amount=-7140.0, vat_amount=-1190.0,
        issue_date=__import__("datetime").date(2026, 9, 25),
    )
    podrazumevano.update(izmene)
    with db.session_scope() as session:
        doc = D(**podrazumevano)
        session.add(doc)
        session.flush()
        return doc.id


def test_telo_obavestenja_nosi_broj_i_datum_odobrenja(db):
    """Broj obaveštenja je doslovno broj knjižnog odobrenja (Bane, 30.09.)."""
    from sefsync.models import Document as D
    from sefsync.services.obavestenje import telo_zahteva

    doc_id = _odobrenje(db)
    with db.session_scope() as session:
        telo = telo_zahteva(session.get(D, doc_id))

    assert telo["noticeNumber"] == "26-3X3-002855"
    assert telo["relatedDocumentNumber"] == "26-3X3-002855"
    assert telo["relatedDocumentIssueDate"] == "2026-09-25"
    assert telo["totalVatAmount"] == 1190.0          # uvek pozitivno
    assert telo["documentIssueOrigin"] == "DocumentIssuedViaSef"
    assert telo["basisOfNotice"] == "CreditNoteIssued"
    assert telo["documentReferenceType"] == "CreditNoteReferenceToInvoice"
    assert telo["recipient"]["vatRegistrationCode"] == "103034716"


def test_odobrenje_na_vise_faktura_ide_kao_period(db):
    from sefsync.models import Document as D
    from sefsync.services.obavestenje import telo_zahteva

    doc_id = _odobrenje(db, billing_reference_count=3)
    with db.session_scope() as session:
        telo = telo_zahteva(session.get(D, doc_id))
    assert telo["documentReferenceType"] == "CreditNoteReferenceToPeriod"


def test_nulti_pdv_nema_obavestenje(db):
    """Pravilo iz dokumentacije: iznos mora biti veći od nule."""
    from sefsync.services.obavestenje import posalji

    doc_id = _odobrenje(db, vat_amount=0.0)
    poslato, poruka = posalji(doc_id, korisnik="bogdan")
    assert not poslato
    assert "nula" in poruka


def test_faktura_nema_obavestenje(db):
    from sefsync.models import DocumentType
    from sefsync.services.obavestenje import posalji

    doc_id = _odobrenje(db, document_type=DocumentType.INVOICE)
    poslato, poruka = posalji(doc_id, korisnik="bogdan")
    assert not poslato
    assert "knjižno odobrenje" in poruka


def test_prihvatanje_odobrenja_salje_obavestenje(db, monkeypatch):
    """Obaveštenje ide uz prihvatanje i ni u jednom drugom trenutku."""
    import sefsync.sef.client as sef_client
    from sefsync.models import ObavestenjeOPrethodnomPorezu as O
    from sefsync.services.acceptance import AcceptanceService

    poslato = []

    class LazniSef:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def accept_reject(self, sef_id, accepted, comment):
            return {"Success": True, "Invoice": {"Status": "Approved"}}

        def posalji_obavestenje_o_prethodnom_porezu(self, telo):
            poslato.append(telo)
            return {"Id": 482, "SendingStatus": "Sent"}

    monkeypatch.setattr(sef_client, "SefClient", LazniSef)
    doc_id = _odobrenje(db)

    ishod = AcceptanceService(client=LazniSef()).accept(doc_id, actor="bogdan")

    assert ishod.ok
    assert "Obaveštenje 26-3X3-002855 je poslato" in ishod.message
    assert poslato and poslato[0]["noticeNumber"] == "26-3X3-002855"

    with db.session_scope() as session:
        zapis = session.get(O, doc_id)
        assert zapis.uspelo
        assert zapis.sef_notice_id == 482
        assert zapis.poslao == "bogdan"


def test_prihvatanje_fakture_ne_salje_obavestenje(db, monkeypatch):
    import sefsync.sef.client as sef_client
    from sefsync.models import DocumentType, ObavestenjeOPrethodnomPorezu as O
    from sefsync.services.acceptance import AcceptanceService

    poslato = []

    class LazniSef:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def accept_reject(self, sef_id, accepted, comment):
            return {"Success": True, "Invoice": {"Status": "Approved"}}

        def posalji_obavestenje_o_prethodnom_porezu(self, telo):
            poslato.append(telo)
            return {"Id": 1}

    monkeypatch.setattr(sef_client, "SefClient", LazniSef)
    doc_id = _odobrenje(db, document_type=DocumentType.INVOICE, sef_invoice_id=7002)

    AcceptanceService(client=LazniSef()).accept(doc_id, actor="nina")

    assert poslato == []
    with db.session_scope() as session:
        assert session.get(O, doc_id) is None


def test_pad_obavestenja_ne_ponistava_prihvatanje(db, monkeypatch):
    """Prihvatanje je već prošlo na SEF-u — poruka kaže da obaveštenje treba ručno."""
    import sefsync.sef.client as sef_client
    from sefsync.models import Document as D, ObavestenjeOPrethodnomPorezu as O, SefStatus
    from sefsync.services.acceptance import AcceptanceService

    class LazniSef:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def accept_reject(self, sef_id, accepted, comment):
            return {"Success": True, "Invoice": {"Status": "Approved"}}

        def posalji_obavestenje_o_prethodnom_porezu(self, telo):
            raise RuntimeError("SEF greska 400")

    monkeypatch.setattr(sef_client, "SefClient", LazniSef)
    doc_id = _odobrenje(db, sef_invoice_id=7003)

    ishod = AcceptanceService(client=LazniSef()).accept(doc_id, actor="ilijana")

    assert ishod.ok                                   # prihvatanje stoji
    assert "nije poslato" in ishod.message
    with db.session_scope() as session:
        assert session.get(D, doc_id).sef_status is SefStatus.APPROVED
        zapis = session.get(O, doc_id)
        assert zapis is not None and not zapis.uspelo  # trag o padu ostaje


# --------------------------------------------------------------------------- #
# slika sa telefona i veza šifre po nazivu
# --------------------------------------------------------------------------- #


def test_slika_se_prima_i_kad_pregledac_ne_kaze_vrstu(podaci, db):
    """Android šalje „application/octet-stream" kad se bira iz menadžera fajlova."""
    from sefsync.services.slike import sacuvaj

    jpg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"x" * 200
    zapis = sacuvaj(
        podaci["doc"], jpg, mime="application/octet-stream",
        naziv_fajla="IMG_2031", korisnik="maja",
    )
    assert zapis is not None


def test_ono_sto_nije_slika_se_i_dalje_odbija(podaci, db):
    from sefsync.services.slike import sacuvaj

    with pytest.raises(ValueError, match="samo slike"):
        sacuvaj(podaci["doc"], b"%PDF-1.4 ...", mime="application/octet-stream",
                naziv_fajla="racun.pdf", korisnik="maja")


def test_prevelika_slika_kaze_koliko_je_velika(podaci, db):
    from sefsync.services.slike import NAJVECA, sacuvaj

    with pytest.raises(ValueError, match="MB"):
        sacuvaj(podaci["doc"], b"x" * (NAJVECA + 1), mime="image/jpeg",
                naziv_fajla="velika.jpg", korisnik="maja")


def test_sifra_se_nalazi_po_nazivu_kad_nema_ni_sifre_ni_barkoda(db):
    """Vitel šalje „LEXA BT ZVUCNIK LX-302 NITRO" bez šifre i bez EAN-a."""
    from sefsync.erp.kalkulacija import _nasa_sifra
    from sefsync.models import DocumentLine, ItemMapping

    class PraznaVeza:
        def execute(self, *a, **k):
            class Prazno:
                def all(self):
                    return []

            return Prazno()

    with db.session_scope() as session:
        session.add(ItemMapping(
            supplier_vat="100123456", supplier_item_name="LEXA BT ZVUCNIK LX-302 NITRO",
            erp_item_code="98120", source="rucno",
        ))
        session.flush()
        linija = DocumentLine(
            document_id=1, line_no=1, name="LEXA BT ZVUCNIK LX-302 NITRO",
        )
        sifra, odakle = _nasa_sifra(session, PraznaVeza(), "100123456", linija)

    assert sifra == "98120"
    assert odakle == "mapiranje po nazivu"


def test_opsti_naziv_ne_odlucuje_sam(db):
    """"MASINA ZA PRANJE VESA" je kod jednog dobavljača sedam različitih artikala."""
    from sefsync.erp.kalkulacija import _nasa_sifra
    from sefsync.models import DocumentLine, ItemMapping

    class PraznaVeza:
        def execute(self, *a, **k):
            class Prazno:
                def all(self):
                    return []

            return Prazno()

    with db.session_scope() as session:
        for sifra in ("15064", "84154"):
            session.add(ItemMapping(
                supplier_vat="107351003", supplier_item_name="MASINA ZA PRANJE VESA",
                erp_item_code=sifra, source="rucno",
            ))
        session.flush()
        linija = DocumentLine(document_id=1, line_no=1, name="MASINA ZA PRANJE VESA")
        sifra, odakle = _nasa_sifra(session, PraznaVeza(), "107351003", linija)

    assert sifra is None
    assert "više šifara" in odakle


def test_pretraga_nalazi_i_arhiviranu_fakturu_po_sifri_artikla(client, podaci, db):
    prijavi(client, OPERATER)
    """Dule traži fakturu na kojoj je pogrešno povezao šifru — zna šifru, ne broj.

    Takva faktura je odavno sklonjena sa ekrana, pa pretraga mora i kroz arhivu.
    """
    from sefsync.models import Document, DocumentLine
    from sqlalchemy import select

    with db.session_scope() as session:
        doc = session.scalar(select(Document))
        doc.archived = True
        linija = session.scalar(
            select(DocumentLine).where(DocumentLine.document_id == doc.id)
        )
        linija.erp_item_code = "87423"
        broj = doc.document_number

    odgovor = client.get("/red?prikaz=svuda&q=87423")

    assert odgovor.status_code == 200
    assert broj in odgovor.text, "arhivirana faktura se ne nalazi po šifri artikla"


def test_sifre_nude_otvaranje_nove_sifre(client, db, podaci, monkeypatch):
    """Dule i Nina moraju da vide gde se otvara šifra, i da se tako zove.

    Veza je postojala, ali pod imenom „predlog artikla“ — pa je niko nije
    prepoznao kao dugme za otvaranje (Bane, 05.10.).
    """
    from sefsync.models import NacrtKalkulacije, StanjeNacrta
    from sefsync.services import nacrti

    doc_id = podaci["doc"]
    prijavi(client, OPERATER)
    with db.session_scope() as session:
        session.add(NacrtKalkulacije(
            document_id=doc_id, stanje=StanjeNacrta.CEKA_SIFRU, magacin="002",
            nemapirane=[{"line_no": 1, "naziv": "Frižider", "sifra_dobavljaca": "EW-1",
                         "ean": "", "kolicina": 1.0, "cena": 10.0, "jedinica": "kom"}],
        ))

    tekst = client.get("/sifre").text

    assert "otvori šifru" in tekst
    assert f"/sifre/predlog/{doc_id}/1" in tekst


def test_dugme_za_prilog_samo_kad_prilog_postoji(db, client):
    """Kod MTS-a i struje potrošnje po objektima stoje baš u prilogu."""
    from sefsync.models import Document

    prijavi(client, OPERATER)

    with db.session_scope() as session:
        bez = Document(sef_invoice_id=995501, document_number="BEZ")
        sa = Document(sef_invoice_id=995502, document_number="SA", ima_prilog=True)
        session.add_all([bez, sa])
        session.flush()
        id_bez, id_sa = bez.id, sa.id

    assert "Štampaj prilog" not in client.get(f"/dokument/{id_bez}").text
    assert "Štampaj prilog" in client.get(f"/dokument/{id_sa}").text
