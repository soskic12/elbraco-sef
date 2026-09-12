"""Konfiguracija aplikacije (env varijable / .env fajl)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _project_root() -> Path:
    r"""Folder u kom su `.env` i `data\` — dakle gde aplikacija "zivi".

    Racunanje od lokacije koda radi samo dok se radi iz izvornog foldera. Kad
    je paket instaliran u `.venv\Lib\site-packages`, ista racunica pokaze u
    `.venv\Lib` — pa bi baza i preuzeti UBL-ovi zavrsili unutar okruzenja i
    nestali pri prvoj nadogradnji.

    Redom: izricito zadato `SEFSYNC_HOME`, pa radni folder (servis se pokrece
    iz `C:\efakture`), pa izvorni folder projekta.
    """
    izricito = os.getenv("SEFSYNC_HOME")
    if izricito:
        return Path(izricito).resolve()

    cwd = Path.cwd()
    if (cwd / ".env").exists() or (cwd / "data").exists():
        return cwd

    izvorni = Path(__file__).resolve().parents[2]
    if (izvorni / "pyproject.toml").exists():
        return izvorni
    return cwd


PROJECT_ROOT = _project_root()

SEF_URLS = {
    "prod": "https://efaktura.mfin.gov.rs",
    "demo": "https://demoefaktura.mfin.gov.rs",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- SEF ---
    sef_env: Literal["prod", "demo"] = "demo"
    sef_base_url: str | None = None
    sef_api_key: str = ""
    sef_timeout_s: float = 60.0
    # PIB nase firme - koristi se kao sanity check da UBL zaista stize nama
    company_vat: str = ""
    company_reg_no: str = ""   # maticni broj - SEF ga trazi uz PIB
    company_name: str = ""

    # --- Baza aplikacije ---
    # Dev: sqlite. Prod: mssql+pyodbc://user:pass@host/SefSync?driver=ODBC+Driver+18+for+SQL+Server
    db_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'sefsync.db').as_posix()}"
    db_echo: bool = False

    # --- ERP baza (faza 2, read-only u fazi 1) ---
    erp_db_url: str | None = None

    # --- Izvor sifarnika poslovnih jedinica (postojeca tabela na serveru) ---
    # Ako nije zadat, koristi se erp_db_url. Upit mora da vrati kolone:
    # code, name, [kind, erp_code, address, city, emails, phones, active]
    bu_source_db_url: str | None = None
    bu_source_sql: str | None = None

    # Umesto prepisivanja lozinke u ovaj .env, konekcija se moze citati iz
    # postojeceg .NET user-secrets skladista (projekat ELBRACO LOGISTIKA).
    erp_secrets_file: Path | None = None
    erp_secrets_key: str = "ConnectionStrings:ErpDatabase"

    # --- Skladiste UBL/PDF fajlova ---
    storage_dir: Path = PROJECT_ROOT / "data"

    # --- Sinhronizacija ---
    poll_interval_minutes: int = 15
    # Koliko dana unazad se preklapa prozor pri svakoj sinhronizaciji (hvata zakasnele izmene)
    sync_overlap_days: int = 3
    # Prva sinhronizacija ide od ovog datuma (YYYY-MM-DD); prazno = 30 dana unazad
    sync_start_date: str | None = None

    # --- Politika prihvatanja na SEF-u ---
    # off       = nikad automatski (operater klikce u dashboardu)
    # routed    = automatski prihvati samo dokumente koji su uspesno razvrstani na PJ
    # all       = prihvati sve (ne preporucuje se)
    auto_accept: Literal["off", "routed", "all"] = "off"
    # Automatsko prihvatanje tek nakon N sati od prijema (ostavlja prostor za reklamaciju)
    auto_accept_delay_hours: int = 0
    auto_accept_max_amount: float | None = None

    # --- Email notifikacije ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    notify_email_enabled: bool = True
    # Dokument se NE prosledjuje poslovnoj jedinici sam od sebe. Preuzimanje ga
    # samo stavlja u red operatera; poslovodja ga vidi tek kad operater klikne.
    # Ukljuciti samo ako se negde zeli potpuno automatski tok.
    notify_on_ingest: bool = False
    smtp_from_name: str = ""
    # Ako SMTP nije podesen ovde, cita se iz postojeceg izvora (IIS web.config
    # projekta koji vec salje mejlove) - da app password postoji u jednoj kopiji.
    smtp_source_file: Path | None = None
    # Kopija svih notifikacija (npr. racunovodstvo)
    notify_bcc: str = ""
    # PROBNI REZIM: kad je postavljeno, nijedno obavestenje ne ide poslovnim
    # jedinicama nego samo na ovu adresu (push se preskace). Za pilot period,
    # dok se ne potvrdi da su poruke tacne. Prazno = redovan rad.
    notify_override_to: str = ""

    # --- Push (Viber / WhatsApp / SMS) preko generickog HTTP gateway-a ---
    push_enabled: bool = False
    push_webhook_url: str = ""
    push_auth_header: str = ""
    push_provider: str = "generic"

    # Adresa dashboarda koja se ubacuje u notifikacije
    public_base_url: str = "http://localhost:8080"

    # --- Web ---
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_users: str = "admin:admin"  # rezervna prijava: "user:pass,user2:pass2"
    # Ko sme da radi u panelu kao operater (prijave iz kontakti_osobe, zarezom
    # odvojene). Prihvatanje fakture je pravni cin, pa se pravo ne dodeljuje
    # samim postojanjem naloga. Prazno = svako bez upisanog objekta je operater.
    web_operators: str = ""
    web_secret: str = "change-me"

    log_level: str = "INFO"
    log_file: Path | None = None

    @field_validator("storage_dir", mode="after")
    @classmethod
    def _mkdir(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v

    @property
    def base_url(self) -> str:
        return (self.sef_base_url or SEF_URLS[self.sef_env]).rstrip("/")

    @property
    def ubl_dir(self) -> Path:
        p = self.storage_dir / "ubl"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def pdf_dir(self) -> Path:
        p = self.storage_dir / "pdf"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def operator_logins(self) -> set[str]:
        return {x.strip().lower() for x in self.web_operators.split(",") if x.strip()}

    def web_credentials(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self.web_users.split(","):
            pair = pair.strip()
            if not pair:
                continue
            user, _, pwd = pair.partition(":")
            out[user.strip()] = pwd
        return out


@lru_cache
def get_settings() -> Settings:
    # testovi (i CI) rade sa cistim okruzenjem, bez lokalnog .env fajla
    if os.getenv("SEF_DISABLE_DOTENV"):
        return Settings(_env_file=None)
    return Settings()
