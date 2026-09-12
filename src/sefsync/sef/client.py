"""HTTP klijent za SEF (Sistem elektronskih faktura) - Public API.

Dokumentacija: docs/API dokumentacija_31_07_2026.pdf
Autentikacija: header `ApiKey` (kljuc se generise na portalu, Podesavanja -> API management).

Pokriveni su endpointi potrebni za obradu ULAZNIH dokumenata; izlazne fakture
nisu deo ovog projekta pa su izostavljene.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

API = "/api/publicApi"


class SefError(RuntimeError):
    """Greska u komunikaciji sa SEF-om."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SefAuthError(SefError):
    """Nevalidan ili istekao API kljuc (401/403)."""


class SefTransientError(SefError):
    """Privremena greska (5xx, timeout, 429) - vredi ponoviti."""


def _fmt_date(value: date | datetime | str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


_RETRY = dict(
    retry=retry_if_exception_type(SefTransientError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)


class SefClient:
    """Sinhroni klijent. Koristiti kao context manager ili pozvati `close()`."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        if not self.settings.sef_api_key:
            log.warning("SEF_API_KEY nije podesen - pozivi ka SEF-u ce vracati 401.")
        self._client = client or httpx.Client(
            base_url=self.settings.base_url,
            timeout=self.settings.sef_timeout_s,
            headers={
                "ApiKey": self.settings.sef_api_key,
                "accept": "application/json",
                "User-Agent": "elbraco-sefsync/0.1",
            },
        )

    # ------------------------------------------------------------------ #
    # infrastruktura
    # ------------------------------------------------------------------ #

    def __enter__(self) -> SefClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @retry(**_RETRY)
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SefTransientError(f"Mrezna greska ka SEF-u: {exc}") from exc

        if resp.status_code in (401, 403):
            raise SefAuthError(
                "SEF je odbio API kljuc (proveri SEF_API_KEY i okruzenje demo/prod).",
                resp.status_code,
                resp.text[:500],
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise SefTransientError(
                f"SEF privremeno nedostupan ({resp.status_code}).", resp.status_code, resp.text[:500]
            )
        if resp.status_code >= 400:
            raise SefError(
                f"SEF greska {resp.status_code} na {path}", resp.status_code, resp.text[:1000]
            )
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:  # SEF ponekad vrati text/plain sa JSON telom
            raise SefError(f"Neocekivan odgovor SEF-a: {resp.text[:300]}") from exc

    # ------------------------------------------------------------------ #
    # ulazni dokumenti
    # ------------------------------------------------------------------ #

    def purchase_overview(
        self,
        date_from: date | str,
        date_to: date | str,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Prosireni skup podataka o ulaznim dokumentima u periodu.

        GET /api/publicApi/purchase-invoice/overview
        Vraca listu zapisa sa: InvoiceId, GlobUniqId, DocumentNumber, DocumentType,
        Status, SupplierName, SupplierVatRegistrationNumber, Amount, datumi...
        Ovo je glavni izvor za sinhronizaciju - jedan poziv umesto N poziva po dokumentu.
        """
        params: dict[str, Any] = {"dateFrom": _fmt_date(date_from), "dateTo": _fmt_date(date_to)}
        if status:
            params["status"] = status
        data = self._json(self._request("GET", f"{API}/purchase-invoice/overview", params=params))
        return list(data or [])

    def purchase_ids(
        self, status: str, date_from: date | str, date_to: date | str
    ) -> list[int]:
        """POST /api/publicApi/purchase-invoice/ids - ID-jevi dokumenata po statusu."""
        params = {
            "status": status,
            "dateFrom": _fmt_date(date_from),
            "dateTo": _fmt_date(date_to),
        }
        data = self._json(self._request("POST", f"{API}/purchase-invoice/ids", params=params))
        if isinstance(data, dict):
            return list(data.get("PurchaseInvoiceIds") or [])
        return list(data or [])

    def purchase_invoice(self, invoice_id: int) -> dict[str, Any]:
        """GET /api/publicApi/purchase-invoice - status i meta podaci jednog dokumenta."""
        data = self._json(
            self._request("GET", f"{API}/purchase-invoice", params={"invoiceId": invoice_id})
        )
        return dict(data or {})

    def purchase_ubl(self, invoice_id: int) -> bytes:
        """GET /api/publicApi/purchase-invoice/xml - UBL dokument (FileStream)."""
        resp = self._request(
            "GET",
            f"{API}/purchase-invoice/xml",
            params={"invoiceId": invoice_id},
            headers={"accept": "*/*"},
        )
        return resp.content

    def purchase_pdf(self, invoice_id: int) -> bytes:
        """GET /api/publicApi/purchase-invoice/pdf - prosireni PDF prikaz dokumenta."""
        resp = self._request(
            "GET",
            f"{API}/purchase-invoice/pdf",
            params={"invoiceId": invoice_id},
            headers={"accept": "*/*"},
        )
        return resp.content

    def purchase_changes(self, on_date: date | str) -> list[dict[str, Any]]:
        """POST /api/publicApi/purchase-invoice/changes - promene statusa na dati datum.

        SEF dozvoljava samo datume iz proslosti (ne tekuci dan) i cuva
        notifikacije mesec dana unazad.
        """
        data = self._json(
            self._request(
                "POST", f"{API}/purchase-invoice/changes", params={"date": _fmt_date(on_date)}
            )
        )
        return list(data or [])

    def accept_reject(self, invoice_id: int, accepted: bool, comment: str = "") -> dict[str, Any]:
        """POST /api/publicApi/purchase-invoice/acceptRejectPurchaseInvoice.

        Kod odbijanja je komentar obavezan po dokumentaciji.
        """
        if not accepted and not comment.strip():
            raise ValueError("Za odbijanje dokumenta komentar je obavezan.")
        payload = {"invoiceId": invoice_id, "accepted": accepted, "comment": comment}
        data = self._json(
            self._request(
                "POST",
                f"{API}/purchase-invoice/acceptRejectPurchaseInvoice",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        )
        return dict(data or {})

    def subscribe(self) -> str:
        """POST /api/publicApi/subscribe - pretplata na notifikacije o promenama statusa."""
        resp = self._request("POST", f"{API}/subscribe")
        return resp.text.strip().strip('"')

    # ------------------------------------------------------------------ #
    # pomocni sifarnici
    # ------------------------------------------------------------------ #

    def unit_measures(self) -> list[dict[str, Any]]:
        """GET /api/publicApi/get-unit-measures - sifarnik jedinica mere (faza 2)."""
        data = self._json(self._request("GET", f"{API}/get-unit-measures"))
        return list(data or [])

    def version(self) -> str:
        """GET /api/publicApi/getEfakturaVersion - provera konekcije i kljuca."""
        data = self._json(self._request("GET", f"{API}/getEfakturaVersion"))
        if isinstance(data, dict):
            return str(data.get("Version") or data)
        return str(data)

    def company_registered(
        self, vat_number: str = "", registration_number: str = "", jbkjs: str = ""
    ) -> bool:
        """POST .../Company/CheckIfCompanyRegisteredOnEfaktura - da li firma ima SEF nalog.

        Po dokumentaciji su PIB i maticni broj obavezni, JBKJS samo za budzetske korisnike.
        """
        payload: dict[str, Any] = {
            "registrationNumber": registration_number,
            "vatNumber": vat_number,
        }
        if jbkjs:
            payload["jbkjs"] = jbkjs
        data = self._json(
            self._request(
                "POST",
                f"{API}/Company/CheckIfCompanyRegisteredOnEfaktura",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        )
        if isinstance(data, dict):
            for key in ("eFakturaRegisteredCompany", "EFakturaRegisteredCompany"):
                if key in data:
                    return bool(data[key])
        return bool(data)
