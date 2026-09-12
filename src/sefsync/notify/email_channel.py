"""Slanje notifikacija e-mailom (SMTP)."""

from __future__ import annotations

import logging
import mimetypes
import smtplib
from email.message import EmailMessage

from email.utils import formataddr

from ..config import Settings, get_settings
from .base import Message, SendResult
from .smtp_config import SmtpConfig, resolve_smtp

log = logging.getLogger(__name__)


class EmailChannel:
    name = "email"

    def __init__(self, settings: Settings | None = None, smtp: SmtpConfig | None = None):
        self.settings = settings or get_settings()
        self.smtp = smtp or resolve_smtp(self.settings)

    def enabled(self) -> bool:
        return bool(self.settings.notify_email_enabled and self.smtp.configured)

    def send(self, targets: list[str], message: Message) -> list[SendResult]:
        targets = [t for t in targets if t]
        if not targets:
            return []
        if not self.enabled():
            razlog = ", ".join(self.smtp.missing()) or "slanje e-maila je isključeno"
            return [SendResult(False, t, f"SMTP nije konfigurisan: {razlog}") for t in targets]

        mail = self._build(targets, message)
        try:
            self._deliver(mail, targets)
        except Exception as exc:  # noqa: BLE001 - SMTP zna da baci sta god
            log.warning("Slanje e-maila nije uspelo (%s): %s", ", ".join(targets), exc)
            return [SendResult(False, t, str(exc)) for t in targets]
        return [SendResult(True, t) for t in targets]

    # ------------------------------------------------------------------ #

    def _build(self, targets: list[str], message: Message) -> EmailMessage:
        mail = EmailMessage()
        ime = self.smtp.sender_name or self.settings.smtp_from_name
        mail["From"] = formataddr((ime, self.smtp.sender)) if ime else self.smtp.sender
        mail["To"] = ", ".join(targets)
        mail["Subject"] = message.subject
        mail.set_content(message.body_text)
        if message.body_html:
            mail.add_alternative(message.body_html, subtype="html")

        for path in message.attachments:
            try:
                data = path.read_bytes()
            except OSError as exc:
                log.warning("Prilog %s nije procitan: %s", path, exc)
                continue
            ctype, _ = mimetypes.guess_type(path.name)
            maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
            mail.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
        return mail

    def _deliver(self, mail: EmailMessage, targets: list[str]) -> None:
        smtp = self.smtp
        recipients = list(targets)
        if self.settings.notify_bcc:
            recipients += [x.strip() for x in self.settings.notify_bcc.split(",") if x.strip()]

        if smtp.ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=30)
        else:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=30)
        try:
            server.ehlo()
            if smtp.starttls and not smtp.ssl:
                server.starttls()
                server.ehlo()
            if smtp.user:
                server.login(smtp.user, smtp.password)
            server.send_message(mail, from_addr=smtp.sender, to_addrs=recipients)
        finally:
            server.quit()
