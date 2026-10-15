"""Konfiguracija aplikacije (env varijable / .env fajl)."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from datetime import date
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
    if (cwd / ".env").exists():
        return cwd

    # Folder u kom stoji virtuelno okruzenje iz kog se komanda pokrece. Na
    # serveru je to `C:\efakture\.venv\Scripts\python.exe` -> `C:\efakture`.
    # To je jedini pokazatelj koji ne zavisi od toga odakle je covek kucao.
    putanje = Path(sys.executable).resolve().parents
    if len(putanje) > 2 and (putanje[2] / ".env").exists():
        return putanje[2]

    izvorni = Path(__file__).resolve().parents[2]
    if (izvorni / "pyproject.toml").exists():
        return izvorni
    return cwd


PROJECT_ROOT = _project_root()


def dom_nije_instalacija() -> str | None:
    r"""Vraca objasnjenje ako je komanda pokrenuta izvan foldera instalacije.

    Aplikacija trazi `.env` i `data\` u radnom folderu. Kad ih tamo nema, uzima
    se podrazumevano podesavanje - a ono pokazuje na novu, praznu bazu pored.
    Ne puca nista: komanda uredno "uspe" nad necim sto nije ta instalacija.

    Tako je 16.09. na serveru proslo `pravila-uvoz` (uvezao pravila u praznu
    bazu u `C:\Users\Administrator`), pa `osvezi-ubl` i `razvrstaj` sa nulom
    dokumenata - a panel je za to vreme radio nad svojom, pravom bazom.
    """
    if os.getenv("SEFSYNC_HOME") or os.getenv("DB_URL") or os.getenv("SEF_DISABLE_DOTENV"):
        return None  # izricito zadato - covek zna gde radi
    # Racuna se u trenutku poziva, ne pri uvozu modula: pitanje je gde komanda
    # radi sada, a ne gde je Python startovan.
    koren = _project_root()
    # Trazi se bas `.env`. Ranije je prolazio i goli `data\` folder - a on
    # nastane cim komanda jednom promasi, pa je zastita od tada bila slepa.
    # Tako je 06.10. `nacrti --osvezi` "zavrsio za 0 s" nad praznom bazom u
    # `C:\Users\Administrator`, a panel je radio nad svojom.
    if (koren / ".env").exists():
        return None
    return (
        f"Ovde nema instalacije: {koren}\n"
        r"Ocekujem `.env` u tom folderu, a nema ga - komanda bi "
        "napravila novu praznu bazu pored i radila nad njom.\n"
        r"Predji u folder instalacije (na serveru: cd C:\efakture) ili zadaj "
        "SEFSYNC_HOME."
    )


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
    # Bazen veza ka bazi panela. Podrazumevanih 5+10 je palo prvog radnog dana:
    # "QueuePool limit of size 5 overflow 10 reached" pa 502 kroz IIS, cim je
    # vise ljudi uslo odjednom (28.09.). Stranica jedinice drzi vezu dok se
    # sablon iscrtava, pa je potrosnja po zahtevu duza nego sto izgleda.
    db_pool_size: int = 20
    db_max_overflow: int = 30
    # Kratko cekanje je bolje od dugog: zahtev koji nema vezu neka padne odmah,
    # umesto da 30 sekundi drzi radnika i sacuva 502 umesto greske.
    db_pool_timeout: int = 10
    db_pool_recycle: int = 1800
    # Upit duzi od ovoliko sekundi ide u log sa svojim tekstom. Bez toga se ne
    # zna sta drzi vezu: merac kaze gde je uzeta, ne gde se zaglavila.
    log_spor_upit_s: float = 2.0
    # Koliko zapelih nacrta se preracuna u svakom ciklusu. Sifra se otvara u
    # ERP-u, pa panel mora sam da primeti da je stavka odmrznuta.
    osvezi_zapele_limit: int = 200
    # Koliko zaostalih upisa ciklus dovrsi odjednom. Namerno malo: ako se iz
    # bilo kog razloga nakupi gomila, bolje da ulaze u ERP u ritmu koji covek
    # stigne da isprati, nego dvesta kalkulacija u jednom minutu.
    dovrsi_upise_limit: int = 20
    # Isto vazi i za vezu ka ERP-u, koja je do 29.09. isla sa podrazumevanih
    # 5+10 - i pala je prvog radnog dana ("QueuePool limit of size 5 overflow
    # 10 reached"), pa je IIS vracao 502. Provera duplikata pretrazuje sve
    # kalkulacije dobavljaca, pa veza stoji zauzeta sekundama.
    erp_pool_size: int = 20
    erp_max_overflow: int = 30
    erp_pool_timeout: int = 10
    erp_pool_recycle: int = 1800

    # --- ERP baza (faza 2, read-only u fazi 1) ---
    erp_db_url: str | None = None

    # Test kopija ERP baze (ELBSX_2026). Svaki upis kalkulacije ide ovde dok
    # se ne dokaze da je ispravan; produkcija trazi izricito --produkcija.
    erp_test_db_url: str | None = None
    # Ako je zadato samo ime baze, uzima se produkciona konekcija sa zamenjenom
    # bazom - da se lozinka ne prepisuje na dva mesta.
    erp_test_db_name: str | None = "ELBSX_2026"
    # Zastita: bez ovoga nijedan upis ne moze u produkcionu ERP bazu.
    erp_allow_production_write: bool = False
    # Da li potvrda prijema robe uopste pokrece upis kalkulacije. Podrazumevano
    # ne: dok se ne uvezba, kalkulacije prave poslovodje kao i do sada.
    erp_upis_kalkulacije: bool = False

    # Dan od kog vazi novi nacin rada (YYYY-MM-DD). Dokument koji je u panel
    # stigao pre tog dana ide po starom: kalkulaciju pravi covek u ERP-u, a
    # panel za njega ne nudi ni potvrdu prijema ni upis.
    #
    # Bez ovoga bi prelazak bio samo recenica u uputstvu. Na dan prelaska je u
    # panelu stajalo 46 spremnih kalkulacija za fakture koje niko jos nije
    # zaknjizio - jedan pogresan klik i ERP bi dobio kalkulaciju za robu koju je
    # poslovodja vec odavno primio i proknjizio na svoj nacin.
    novi_tok_od: date | None = None

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
    # Koliko dana UBL-ova ostaje na disku. Starije `arhiviraj-ubl` pakuje u zip
    # po mesecima i sklanja; kad zatrebaju, skidaju se ponovo sa SEF-a.
    ubl_arhiva_dana: int = 60

    # --- Sinhronizacija ---
    poll_interval_minutes: int = 15
    # Koliko dana unazad se preklapa prozor pri svakoj sinhronizaciji (hvata zakasnele izmene)
    sync_overlap_days: int = 3
    # Prva sinhronizacija ide od ovog datuma (YYYY-MM-DD); prazno = 30 dana unazad
    sync_start_date: str | None = None

    # Preuzimanje UBL-a obara status dokumenta iz "Nova" u "Vidjena" na SEF-u -
    # izmereno: dogadjaj na SEF-u stigne 70-90 ms posle naseg poziva. Dok ljudi
    # jos rade po starom (gledaju sta je novo na portalu), to im remeti posao.
    # Sa ovim ukljucenim, dokumenti u statusu "Nova" se NE preuzimaju: u redu se
    # vide iz pregleda (dobavljac, iznos, datum), a UBL i stavke se povlace tek
    # kad ih neko otvori na portalu ili kad se prekidac ugasi.
    sef_preserve_new: bool = True

    # --- Politika prihvatanja na SEF-u ---
    # off       = nikad automatski (operater klikce u dashboardu)
    # routed    = automatski prihvati samo dokumente koji su uspesno razvrstani na PJ
    # all       = prihvati sve (ne preporucuje se)
    auto_accept: Literal["off", "routed", "all"] = "off"
    # Automatsko prihvatanje tek nakon N sati od prijema (ostavlja prostor za reklamaciju)
    auto_accept_delay_hours: int = 0
    auto_accept_max_amount: float | None = None
    # Prihvatanje posle uspesnog upisa kalkulacije, samo za KOMPLETNE fakture -
    # one kojima nista ne fali. Bane, 29.09.: "automatski prihvati svaku koja je
    # kompletna, kojoj ne fali roba i slicno; ove nekompletne kad postanu
    # kompletne ili se vrate operateru na odlucivanje".
    #
    # Faktura sa otvorenom prijavom manjka se NE prihvata: dok se ne zna hoce li
    # ostatak stici, mora da ostane moguce odbijanje. Kad se manjak zatvori
    # (stigao ostatak ili knjizno odobrenje), prihvatanje se pokusava tada.
    prihvati_posle_upisa: bool = False

    # --- Claude: predlog specifikacije artikla (DEKA) pri otvaranju sifre ---
    # Bez kljuca dugme „Predloži sa weba" se ne nudi; sve ostalo radi isto.
    anthropic_api_key: str = ""
    spec_model: str = "claude-opus-5-5"

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
