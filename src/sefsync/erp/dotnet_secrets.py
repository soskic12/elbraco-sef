"""Citanje ERP konekcije iz .NET user-secrets skladista.

Projekat ELBRACO LOGISTIKA vec drzi kredencijale ERP baze u
`%APPDATA%\\Microsoft\\UserSecrets\\<id>\\secrets.json`. Umesto da se lozinka
prepisuje jos jednom u nas `.env`, cita se odatle u trenutku upotrebe:

  - postoji samo jedna kopija lozinke, na mestu koje vec postoji,
  - kad je vlasnik baze promeni, ovaj projekat je odmah vidi,
  - nas `.env` ostaje bez tajni.

Podesava se sa ERP_SECRETS_FILE (putanja do secrets.json) i, ako treba,
ERP_SECRETS_KEY (podrazumevano `ConnectionStrings:ErpDatabase`).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

DEFAULT_KEY = "ConnectionStrings:ErpDatabase"
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


class SecretsError(RuntimeError):
    pass


def _field(connection_string: str, name: str) -> str:
    match = re.search(rf"(?i)(?:^|;)\s*{re.escape(name)}\s*=\s*([^;]*)", connection_string)
    return match.group(1).strip() if match else ""


def dotnet_to_sqlalchemy(connection_string: str, driver: str = DEFAULT_DRIVER) -> str:
    """'Server=host,1433;Database=X;User Id=u;Password=p' -> mssql+pyodbc URL."""
    server = _field(connection_string, "Server") or _field(connection_string, "Data Source")
    database = _field(connection_string, "Database") or _field(connection_string, "Initial Catalog")
    user = _field(connection_string, "User Id") or _field(connection_string, "UID")
    password = _field(connection_string, "Password") or _field(connection_string, "PWD")
    if not server or not database:
        raise SecretsError("Connection string nema Server i Database.")

    host, _, port = server.partition(",")
    host = host.strip()
    port = port.strip() or "1433"

    trust = _field(connection_string, "TrustServerCertificate").lower() in ("true", "yes", "1")
    options = f"driver={quote_plus(driver)}"
    if trust:
        options += "&TrustServerCertificate=yes"

    if user:
        auth = f"{quote_plus(user)}:{quote_plus(password)}@"
    else:  # Windows autentikacija
        auth = ""
        options += "&trusted_connection=yes"

    return f"mssql+pyodbc://{auth}{host}:{port}/{quote_plus(database)}?{options}"


def _from_json(path: Path, key: str) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SecretsError(f"Ne mogu da pročitam {path}: {exc}") from exc

    value = data.get(key)
    if value is None:  # i ugnježdeni oblik {"ConnectionStrings": {"ErpDatabase": ...}}
        section, _, name = key.replace("__", ":").partition(":")
        section_data = data.get(section)
        value = section_data.get(name) if isinstance(section_data, dict) else None
    return value


def load_connection_url(path: str | Path, key: str = DEFAULT_KEY) -> str:
    """Veza iz `dotnet user-secrets` JSON-a ili iz IIS `web.config`-a.

    Na razvojnoj mašini kredencijali stoje u user-secrets, a na serveru u
    web.config-u sajta. Isti ključ se u ta dva sveta piše različito
    (`A:B` i `A__B`), pa se prihvataju oba zapisa.
    """
    secrets_path = Path(path).expanduser()
    if not secrets_path.is_file():
        raise SecretsError(f"Nema secrets fajla: {secrets_path}")

    if secrets_path.suffix.lower() == ".config":
        env = load_webconfig_env(secrets_path)
        value = env.get(key) or env.get(key.replace(":", "__")) or env.get(key.replace("__", ":"))
    else:
        value = _from_json(secrets_path, key)

    if not value:
        raise SecretsError(f"U {secrets_path.name} nema ključa '{key}'.")

    url = dotnet_to_sqlalchemy(str(value))
    log.info("ERP konekcija učitana iz %s (ključ %s)", secrets_path.name, key)
    return url


def describe(url: str) -> str:
    """Opis konekcije bez lozinke - za ispis korisniku."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def load_webconfig_env(path: str | Path) -> dict[str, str]:
    """Cita <environmentVariable name=... value=.../> iz IIS web.config-a.

    Projekat ELBRACO LOGISTIKA tako drzi Mail__* podesavanja. Citanje odatle
    znaci da app password postoji na jednom mestu i da se menja na jednom mestu.
    """
    from xml.etree import ElementTree

    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise SecretsError(f"Nema web.config fajla: {config_path}")
    try:
        root = ElementTree.parse(config_path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise SecretsError(f"Ne mogu da pročitam {config_path}: {exc}") from exc

    values: dict[str, str] = {}
    for node in root.iter("environmentVariable"):
        name = node.get("name")
        if name:
            values[name] = node.get("value") or ""
    if not values:
        raise SecretsError(f"U {config_path.name} nema environmentVariable stavki.")
    return values
