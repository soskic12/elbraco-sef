"""Push notifikacije (Viber / WhatsApp / SMS) preko HTTP gateway-a.

Namerno je genericki: u Srbiji se Viber/SMS obicno kupuje preko posrednika
(Infobip, Vox, Smsapi, Twilio...), a svi imaju HTTP endpoint koji prima
telefon + tekst. U `PUSH_WEBHOOK_URL` se upise URL, u `PUSH_AUTH_HEADER`
ceo header (npr. `Authorization: App abc123`), a payload je:

    {"to": "+381641234567", "text": "...", "provider": "generic",
     "meta": {"document_id": 123, "unit": "MP01"}}

Ako izabrani provajder trazi drugaciji oblik, dovoljno je podesiti mali
adapter (npr. n8n/Make scenario) ili izmeniti `_payload`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings, get_settings
from .base import Message, SendResult

log = logging.getLogger(__name__)


class PushChannel:
    name = "push"

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self._client = client

    def enabled(self) -> bool:
        return bool(self.settings.push_enabled and self.settings.push_webhook_url)

    def send(self, targets: list[str], message: Message, meta: dict[str, Any] | None = None) -> list[SendResult]:
        targets = [t for t in targets if t]
        if not targets:
            return []
        if not self.enabled():
            return [SendResult(False, t, "Push kanal nije konfigurisan") for t in targets]

        results: list[SendResult] = []
        client = self._client or httpx.Client(timeout=20.0)
        try:
            for target in targets:
                try:
                    resp = client.post(
                        self.settings.push_webhook_url,
                        json=self._payload(target, message, meta),
                        headers=self._headers(),
                    )
                    if resp.status_code >= 400:
                        results.append(SendResult(False, target, f"HTTP {resp.status_code}: {resp.text[:200]}"))
                    else:
                        results.append(SendResult(True, target))
                except httpx.HTTPError as exc:
                    log.warning("Push ka %s nije poslat: %s", target, exc)
                    results.append(SendResult(False, target, str(exc)))
        finally:
            if self._client is None:
                client.close()
        return results

    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        raw = self.settings.push_auth_header.strip()
        if not raw:
            return {}
        name, _, value = raw.partition(":")
        return {name.strip(): value.strip()} if value else {}

    def _payload(self, target: str, message: Message, meta: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "to": target,
            "text": message.short_text or message.subject,
            "provider": self.settings.push_provider,
            "meta": meta or {},
        }
