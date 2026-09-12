import httpx
import pytest

from sefsync.config import get_settings
from sefsync.sef import SefAuthError, SefClient, SefError


def make_client(handler) -> SefClient:
    transport = httpx.MockTransport(handler)
    settings = get_settings()
    http = httpx.Client(
        transport=transport, base_url=settings.base_url, headers={"ApiKey": "test-key"}
    )
    return SefClient(settings, client=http)


def test_overview_salje_apikey_i_datume():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["apikey"] = request.headers.get("ApiKey")
        return httpx.Response(200, json=[{"InvoiceId": 1, "DocumentNumber": "1/26"}])

    with make_client(handler) as client:
        rows = client.purchase_overview("2026-09-01", "2026-09-10")

    assert rows[0]["InvoiceId"] == 1
    assert seen["path"] == "/api/publicApi/purchase-invoice/overview"
    assert seen["params"] == {"dateFrom": "2026-09-01", "dateTo": "2026-09-10"}
    assert seen["apikey"] == "test-key"


def test_ids_vraca_listu_iz_omota():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"PurchaseInvoiceIds": [4, 8, 15]})

    with make_client(handler) as client:
        assert client.purchase_ids("New", "2026-09-01", "2026-09-10") == [4, 8, 15]


def test_odbijanje_bez_komentara_nije_dozvoljeno():
    with make_client(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ValueError):
            client.accept_reject(1, accepted=False, comment="  ")


def test_pogresan_kljuc_daje_auth_gresku():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    with make_client(handler) as client:
        with pytest.raises(SefAuthError):
            client.version()


def test_greska_400_se_ne_ponavlja():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="Bad request")

    with make_client(handler) as client:
        with pytest.raises(SefError):
            client.purchase_invoice(5)
    assert calls["n"] == 1


def test_provera_naloga_ide_postom_sa_telom():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"EFakturaRegisteredCompany": True})

    with make_client(handler) as client:
        assert client.company_registered(vat_number="104220952", registration_number="20123456")

    import json

    assert seen["method"] == "POST"
    body = json.loads(seen["body"])
    assert body == {"registrationNumber": "20123456", "vatNumber": "104220952"}
