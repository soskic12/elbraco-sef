"""End-to-end tok: /overview -> UBL -> baza -> razvrstavanje -> obavestenje."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from sefsync.config import get_settings
from sefsync.models import (
    BusinessUnit,
    Document,
    DocumentType,
    MatchField,
    MatchOp,
    ProcessState,
    RoutingRule,
    SefStatus,
    UnitKind,
)
from sefsync.sef import SefClient
from sefsync.services.ingest import IngestService

OVERVIEW = [
    {
        "InvoiceId": 494482,
        "GlobUniqId": "dd3a8083-a952-4066-a793-6d1cd50eb874",
        "DocumentNumber": "2026-114/25",
        "DocumentType": "Invoice",
        "CirInvoiceId": None,
        "Status": "Seen",
        "SupplierName": "DOBAVLJAČ TRGOVINA DOO",
        "SupplierRegistrationNumber": "20123456",
        "SupplierVatRegistrationNumber": "100200300",
        "Amount": 14400.0,
        "SumWithoutVat": 12000.0,
        "VatAmount": 2400.0,
        "RoundingAmount": 0.0,
        "Currency": "RSD",
        "DeliveryDate": "2026-08-31T10:59:41.0000000+00:00",
        "DueDate": "2026-10-01T10:59:41.0000000+00:00",
        "SentDate": "2026-09-01T11:01:10.5030093+00:00",
    }
]


class FakeNotifier:
    def __init__(self):
        self.calls: list[int] = []

    def notify_document(self, doc_id: int) -> bool:
        self.calls.append(doc_id)
        return True


class FakeAcceptor:
    def __init__(self):
        self.calls: list[int] = []

    def maybe_auto_accept(self, doc_id: int) -> bool:
        self.calls.append(doc_id)
        return False


def build_service(faktura_xml: bytes, notifier=None, acceptor=None) -> IngestService:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/purchase-invoice/overview"):
            return httpx.Response(200, json=OVERVIEW)
        if path.endswith("/purchase-invoice/xml"):
            return httpx.Response(200, content=faktura_xml)
        return httpx.Response(404, text=f"nepoznat endpoint {path}")

    settings = get_settings()
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.base_url)
    return IngestService(
        client=SefClient(settings, client=http),
        settings=settings,
        notifier=notifier or FakeNotifier(),
        acceptor=acceptor or FakeAcceptor(),
    )


def test_sync_upisuje_dokument_sa_stavkama(db, faktura_xml):
    notifier = FakeNotifier()
    service = build_service(faktura_xml, notifier=notifier)

    stats = service.sync("2026-09-01", "2026-09-10")

    assert (stats.seen, stats.created, stats.errors) == (1, 1, 0)
    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 494482))
        assert doc.document_number == "2026-114/25"
        assert doc.document_type is DocumentType.INVOICE
        assert doc.sef_status is SefStatus.SEEN
        assert doc.supplier_vat == "100200300"
        assert doc.amount == 14400.0
        assert doc.delivery_address == "Војводе Мишића 12, 26000, Панчево"
        assert len(doc.lines) == 2
        assert doc.lines[0].sellers_item_id == "ART-551"
        assert doc.ubl_path and doc.ubl_path.endswith("494482.xml")
        # bez pravila i bez PJ - dokument ceka rucno razvrstavanje
        assert doc.state is ProcessState.UNASSIGNED
    # preuzimanje nikoga ne obavestava - dokument ceka operatera
    assert notifier.calls == []


def test_sync_razvrstava_po_pravilu_i_obavestava(db, faktura_xml):
    with db.session_scope() as session:
        unit = BusinessUnit(code="MP02", name="Maloprodaja Pančevo", kind=UnitKind.RETAIL,
                            emails="mp02@elbraco.rs")
        session.add(unit)
        session.flush()
        session.add(
            RoutingRule(priority=10, field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
                        pattern="Vojvode Misica 12", business_unit_id=unit.id)
        )
        unit_id = unit.id

    notifier = FakeNotifier()
    service = build_service(faktura_xml, notifier=notifier)
    service.sync("2026-09-01", "2026-09-10")

    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 494482))
        assert doc.business_unit_id == unit_id
        assert doc.state is ProcessState.ROUTED
        rule = session.scalar(select(RoutingRule))
        assert rule.hits == 1
    assert notifier.calls == []  # razvrstano jeste, ali prosledjuje operater


def test_ponovni_sync_ne_duplira_dokument(db, faktura_xml):
    service = build_service(faktura_xml)
    service.sync("2026-09-01", "2026-09-10")
    stats = service.sync("2026-09-01", "2026-09-10")

    assert stats.created == 0
    with db.session_scope() as session:
        assert session.scalar(select(Document).where(Document.sef_invoice_id == 494482)) is not None
        assert len(list(session.scalars(select(Document)))) == 1


def test_greska_na_ublu_ne_obara_sync(db):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/overview"):
            return httpx.Response(200, json=OVERVIEW)
        return httpx.Response(500, text="SEF nedostupan")

    settings = get_settings()
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.base_url)
    service = IngestService(
        client=SefClient(settings, client=http),
        settings=settings,
        notifier=FakeNotifier(),
        acceptor=FakeAcceptor(),
    )
    service.client._request.retry.wait = lambda *_: 0  # bez cekanja u testu

    stats = service.sync("2026-09-01", "2026-09-10")

    assert stats.created == 1
    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 494482))
        assert doc.state is ProcessState.ERROR
        assert "UBL" in (doc.error_message or "")


def test_automatsko_obavestavanje_kad_se_izricito_ukljuci(db, faktura_xml, monkeypatch):
    """Za potpuno automatski tok postoji prekidač, ali nije podrazumevan."""
    notifier = FakeNotifier()
    service = build_service(faktura_xml, notifier=notifier)
    monkeypatch.setattr(service.settings, "notify_on_ingest", True)

    service.sync("2026-09-01", "2026-09-10")

    assert len(notifier.calls) == 1


# --------------------------------------------------------------------------- #
# cuvanje statusa "Nova" na SEF-u
# --------------------------------------------------------------------------- #

NOVA = [{**OVERVIEW[0], "Status": "New"}]


def build_service_sa(records, faktura_xml, notifier=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/purchase-invoice/overview"):
            return httpx.Response(200, json=records)
        if request.url.path.endswith("/purchase-invoice/xml"):
            return httpx.Response(200, content=faktura_xml)
        return httpx.Response(404, text="nepoznat endpoint")

    settings = get_settings()
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.base_url)
    return IngestService(
        client=SefClient(settings, client=http),
        settings=settings,
        notifier=notifier or FakeNotifier(),
        acceptor=FakeAcceptor(),
    )


def test_dokument_u_statusu_nova_se_ne_preuzima(db, faktura_xml, monkeypatch):
    """Preuzimanje UBL-a obara "Nova" u "Vidjena" na SEF-u i remeti rad na portalu."""
    service = build_service_sa(NOVA, faktura_xml)
    monkeypatch.setattr(service.settings, "sef_preserve_new", True)

    service.sync("2026-09-01", "2026-09-10")

    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 494482))
        assert doc.ubl_path is None
        assert doc.lines == []
        assert doc.ubl_pending is True
        # ono sto stoji u pregledu ipak je upisano
        assert doc.supplier_name == "DOBAVLJAČ TRGOVINA DOO"
        assert doc.amount == 14400.0


def test_sa_ugasenim_prekidacem_se_preuzima_i_nova(db, faktura_xml, monkeypatch):
    service = build_service_sa(NOVA, faktura_xml)
    monkeypatch.setattr(service.settings, "sef_preserve_new", False)

    service.sync("2026-09-01", "2026-09-10")

    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 494482))
        assert doc.ubl_path is not None
        assert len(doc.lines) == 2
        assert doc.ubl_pending is False


def test_ubl_se_povlaci_cim_status_prestane_da_bude_nova(db, faktura_xml, monkeypatch):
    """Kad dokument neko otvori na portalu, sledece preuzimanje povuce i ostalo."""
    service = build_service_sa(NOVA, faktura_xml)
    monkeypatch.setattr(service.settings, "sef_preserve_new", True)
    service.sync("2026-09-01", "2026-09-10")

    posle = build_service_sa(OVERVIEW, faktura_xml)          # isti dokument, sada "Seen"
    monkeypatch.setattr(posle.settings, "sef_preserve_new", True)
    posle.sync("2026-09-01", "2026-09-10")

    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 494482))
        assert doc.sef_status is SefStatus.SEEN
        assert len(doc.lines) == 2
        assert doc.delivery_address is not None


def test_nova_se_i_dalje_razvrstava_po_dobavljacu(db, faktura_xml, monkeypatch):
    """Bez UBL-a nema adrese, ali PIB iz pregleda je dovoljan za pravilo po dobavljacu."""
    with db.session_scope() as session:
        office = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(office)
        session.flush()
        session.add(
            RoutingRule(priority=60, field=MatchField.SUPPLIER_VAT, op=MatchOp.EQUALS,
                        pattern="100200300", business_unit_id=office.id)
        )
        office_id = office.id

    service = build_service_sa(NOVA, faktura_xml)
    monkeypatch.setattr(service.settings, "sef_preserve_new", True)
    service.sync("2026-09-01", "2026-09-10")

    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 494482))
        assert doc.business_unit_id == office_id
