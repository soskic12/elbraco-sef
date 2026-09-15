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
