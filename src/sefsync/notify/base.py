"""Zajednicki tipovi za notifikacije."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Message:
    subject: str
    body_text: str
    body_html: str | None = None
    short_text: str | None = None          # za SMS/Viber (do ~300 karaktera)
    attachments: list[Path] = field(default_factory=list)


@dataclass
class SendResult:
    ok: bool
    target: str
    error: str | None = None


class Channel(Protocol):
    name: str

    def enabled(self) -> bool: ...

    def send(self, targets: list[str], message: Message) -> list[SendResult]: ...
