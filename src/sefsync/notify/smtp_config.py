"""Razresavanje SMTP podesavanja.

Prvo iz naseg `.env`. Ako tamo nema, cita se iz postojeceg izvora koji vec drzi
Mail__* vrednosti (IIS web.config projekta ELBRACO LOGISTIKA) - isti razlog kao
kod ERP konekcije: jedna kopija lozinke, jedno mesto za izmenu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SmtpConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    sender_name: str = ""
    starttls: bool = True
    ssl: bool = False
    source: str = "env"

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def missing(self) -> list[str]:
        nedostaje = []
        if not self.host:
            nedostaje.append("SMTP_HOST")
        if not self.sender:
            nedostaje.append("SMTP_FROM")
        if self.user and not self.password:
            nedostaje.append("SMTP_PASSWORD")
        return nedostaje

    def describe(self) -> str:
        lozinka = "<postavljena>" if self.password else "<prazna>"
        return (
            f"{self.host}:{self.port} korisnik={self.user or '-'} lozinka={lozinka} "
            f"posiljalac={self.sender} (izvor: {self.source})"
        )


def _ocisti_app_password(password: str, host: str) -> str:
    """Google prikazuje app password sa razmacima; nalepljen takav ruši prijavu.

    Ciscenje je vezano za Gmail - kod drugih servera razmak u lozinci moze
    da bude namerni deo lozinke.
    """
    if password and "gmail" in host.lower() and " " in password:
        return password.replace(" ", "")
    return password


def resolve_smtp(settings: Settings | None = None) -> SmtpConfig:
    s = settings or get_settings()
    cfg = SmtpConfig(
        host=s.smtp_host,
        port=s.smtp_port,
        user=s.smtp_user,
        password=s.smtp_password,
        sender=s.smtp_from,
        starttls=s.smtp_starttls,
        ssl=s.smtp_ssl,
    )
    cfg.password = _ocisti_app_password(cfg.password, cfg.host)
    if cfg.host and cfg.sender and (cfg.password or not cfg.user):
        return cfg
    if not s.smtp_source_file:
        return cfg

    from ..erp.dotnet_secrets import SecretsError, load_webconfig_env

    try:
        env = load_webconfig_env(s.smtp_source_file)
    except SecretsError as exc:
        log.warning("SMTP podešavanja iz %s nisu učitana: %s", s.smtp_source_file, exc)
        return cfg

    host_iz_env_fajla = not cfg.host
    cfg.host = cfg.host or env.get("Mail__Host", "")
    cfg.user = cfg.user or env.get("Mail__User", "")
    cfg.password = cfg.password or env.get("Mail__Password", "")
    cfg.sender = cfg.sender or env.get("Mail__From", "") or cfg.user
    cfg.sender_name = cfg.sender_name or env.get("Mail__FromName", "")
    port = env.get("Mail__Port")
    # port se preuzima samo kad i host dolazi odatle - inace bi tudji port
    # pregazio onaj koji je namerno podesen u nasem .env
    if host_iz_env_fajla and port and port.isdigit():
        cfg.port = int(port)
    cfg.password = _ocisti_app_password(cfg.password, cfg.host)
    cfg.source = str(s.smtp_source_file)
    log.info("SMTP podešavanja preuzeta iz %s", s.smtp_source_file)
    return cfg
