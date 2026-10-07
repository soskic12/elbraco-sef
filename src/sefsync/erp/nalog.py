"""Sta aplikaciji treba od ERP naloga, i sta taj nalog stvarno ima.

Panel do sada pise u ERP kao `elbraco_dev_ro` - isti nalog kojim se kacim i sa
razvojne masine, i po imenu "dev read-only". Gore od imena: panel nema svoju
konekciju nego **pozajmljuje Logistikinu** (`ConnectionStrings:ErpDatabase` iz
.NET user-secrets na razvojnoj, iz `web.config` na serveru), pa se u logovima
baze njegov saobracaj ne razlikuje od Logistikinog.

Ovaj modul sluzi dvema stvarima:

* `pregled()` - sta nalog koji je trenutno u upotrebi sme, po bazama, i sta mu
  od toga aplikaciji fali. To je provera posle prelaska na zaseban nalog.
* `skripta()` - gotove naredbe za pravljenje zasebnog naloga. Aplikacija ih ne
  izvrsava i nikad nece moci: `CREATE LOGIN` trazi prava na nivou servera, a
  panel ima `db_datareader` plus INSERT na osam tabela. Zato ih samo sastavi.

Mereno 07.10.2026. na nalogu `elbraco_dev_ro`:

* `ELBS_2026` - cita sve, INSERT na ULKALKUL, PRULKALK, MKALKUL, MPRULK,
  PDVEVIDX, ARTIKLI, ARTPROD, NAZIVI **i DARTIKLI**;
* `ELBSX_2026` (test) - cita sve, INSERT samo na prvih pet;
* `ELBS_2025`, `ELBS_2024`, `ELBS_2023`, `ELBS_2022` - samo citanje;
* `ELBS_Logistika`, `MobAssist`, `Reklamacije`, `Vozila`, `Webkroler`, starije
  godine i `SefSync` - **nema pristup** (`HAS_DBACCESS` = 0). Obim je vec uzak;
  problem je identitet i pozajmljena konekcija, ne sirina prava.

Dve stvari koje iz toga ispadaju:

1. **`DARTIKLI` ne treba.** Nalog ima INSERT na nju, a aplikacija je namerno ne
   dira - puni je nocni posao na serveru i duplirani kljuc bi ga oborio. Pravo
   koje se ne koristi je pravo koje jednog dana neko iskoristi slucajno.
2. **U test bazi fali INSERT na `ARTIKLI`, `ARTPROD` i `NAZIVI`.** Dok je
   `ERP_ALLOW_PRODUCTION_WRITE` iskljucen, otvaranje sifre i komitenta ide u
   `ELBSX_2026` - i tamo puca na dozvolu. Na serveru radi jer je upis u
   produkciju ukljucen, ali proba pre postavljanja ne bi prosla.
"""

from __future__ import annotations

import secrets
import string

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ..config import get_settings
from .connection import get_erp_engine, resolve_erp_url

# Tabele u koje aplikacija upisuje. DARTIKLI nije na spisku namerno - vidi gore.
TABELE_UPISA = (
    "ULKALKUL",   # zaglavlje kalkulacije za magacin
    "PRULKALK",   # stavke kalkulacije za magacin
    "MKALKUL",    # zaglavlje kalkulacije za maloprodaju
    "MPRULK",     # stavke kalkulacije za maloprodaju
    "PDVEVIDX",   # red PDV evidencije uz kalkulaciju
    "ARTIKLI",    # otvaranje nove sifre artikla
    "ARTPROD",    # cenovnik nove sifre po prodajnim mestima
    "NAZIVI",     # otvaranje novog komitenta
)

# Koliko godina unazad se cita. Knjizno odobrenje po reklamaciji stize za robu
# kupljenu ranije, pa pretraga samo tekuce godine promasuje - zato i stare baze.
GODINA_UNAZAD = 4

# Nista od ovoga aplikaciji ne treba; nalog `elbraco_dev_ro` ume da se kaci i na
# njih, pa je spisak tu da se vidi sta zasebnim nalogom namerno nestaje.
NE_TREBA = (
    "ELBS_Logistika",
    "ELBS_MobAssist",
    "ELBS_Reklamacije",
    "ELBS_Vozila",
    "ELBS_Webkroler",
)


def _stanje_baze(osnovna, baza: str) -> dict:
    """Role i izricite INSERT dozvole trenutnog naloga u jednoj bazi."""
    eng = create_engine(osnovna.set(database=baza))
    try:
        with eng.connect() as conn:
            role = sorted(
                r[0]
                for r in conn.execute(
                    text(
                        "select r.name from sys.database_role_members m "
                        "join sys.database_principals r "
                        "  on r.principal_id = m.role_principal_id "
                        "join sys.database_principals u "
                        "  on u.principal_id = m.member_principal_id "
                        "where u.name = user_name()"
                    )
                )
            )
            upis = sorted(
                r[0]
                for r in conn.execute(
                    text(
                        "select object_name(p.major_id) from sys.database_permissions p "
                        "join sys.database_principals u "
                        "  on u.principal_id = p.grantee_principal_id "
                        "where u.name = user_name() and p.permission_name = 'INSERT' "
                        "and p.major_id <> 0"
                    )
                )
                if r[0]
            )
        return {"cita": "db_datareader" in role, "role": role, "upis": upis}
    except Exception as exc:  # noqa: BLE001 - nema prava ili baza nije podignuta
        return {"greska": str(exc).splitlines()[0][:120]}
    finally:
        eng.dispose()


def objasni_gresku(exc: Exception, nalog: str) -> list[str]:
    """Ljudski prevod cestih ODBC gresaka pri kacenju na ERP.

    Komanda se pokrece bas kad veza ne radi, pa je traceback od dvesta redova
    najgore sto moze da ispise. Ovde se vraca kratko objasnjenje i sta proveriti.
    """
    poruka = str(exc)
    if "18456" in poruka:
        return [
            f"Prijava naloga „{nalog}“ je odbijena.",
            "",
            "SQL Server namerno ne razlikuje „nalog ne postoji“ od „lozinka ne",
            "odgovara“ — obe greške izgledaju isto. Po redu verovatnoće:",
            "",
            "  1. Naredbe (CREATE LOGIN...) još nisu puštene na bazi.",
            "  2. Lozinka u .env-u i lozinka u naredbama nisu iz ISTOG pokretanja —",
            "     `erp-nalog --novi` izmisli novu lozinku svaki put. Pusti ga jednom,",
            "     sa `--sql-fajl`, pa uzmi i naredbe i red za .env iz tog jednog ispisa.",
            "  3. Naredbe su puštene na drugom serveru ili instanci.",
            "",
            "Ako nisi siguran koja je lozinka prošla, postavi je ponovo:",
            f"  ALTER LOGIN [{nalog}] WITH PASSWORD = '<ista ona iz .env-a>';",
        ]
    if "4060" in poruka or "cannot open database" in poruka.lower():
        return [
            f"Nalog „{nalog}“ se prijavio, ali ne sme da otvori bazu iz veze.",
            "Fali mu CREATE USER u toj bazi — pusti deo naredbi za nju.",
        ]
    if "08001" in poruka or "timeout" in poruka.lower():
        return [
            "Server baze se ne javlja. Adresa, port ili mreža — nalog tu nije kriv.",
        ]
    return [str(exc).splitlines()[0][:200]]


def pregled() -> dict:
    """Sta nalog koji panel trenutno koristi sme, po bazama, i sta mu fali."""
    podesavanja = get_settings()
    url = resolve_erp_url()
    if not url:
        return {"greska": "Veza ka ERP bazi nije podešena."}
    osnovna = make_url(url)

    try:
        veza = get_erp_engine().connect()
    except Exception as exc:  # noqa: BLE001 - ovde se bas meri da li veza radi
        return {
            "greska": f"Ne mogu da se nakačim na ERP kao „{osnovna.username}“.",
            "objasnjenje": objasni_gresku(exc, osnovna.username or "?"),
            "pokusani_nalog": osnovna.username,
        }

    with veza as conn:
        nalog = str(conn.execute(text("select suser_sname()")).scalar())
        korisnik = str(conn.execute(text("select user_name()")).scalar())
        radna = str(conn.execute(text("select db_name()")).scalar())
        kandidati = [
            r[0]
            for r in conn.execute(
                text(
                    "select name from sys.databases where name like 'ELBS%' and state = 0 "
                    "and has_dbaccess(name) = 1 order by name desc"
                )
            )
        ]

    test = podesavanja.erp_test_db_name or ""
    za_upis = {radna, test} - {""}

    baze: dict[str, dict] = {}
    for baza in kandidati:
        stanje = _stanje_baze(osnovna, baza)
        if baza in za_upis and "greska" not in stanje:
            stanje["fali_upis"] = [t for t in TABELE_UPISA if t not in stanje["upis"]]
            stanje["suvisno"] = [t for t in stanje["upis"] if t not in TABELE_UPISA]
        baze[baza] = stanje

    return {
        "nalog": nalog,
        "korisnik": korisnik,
        "radna_baza": radna,
        "test_baza": test,
        "iz_secrets_fajla": not podesavanja.erp_db_url,
        "secrets_fajl": str(podesavanja.erp_secrets_file or ""),
        "baze": baze,
        "suvisan_pristup": [b for b in NE_TREBA if b in kandidati],
    }


def nova_lozinka(duzina: int = 28) -> str:
    """Lozinka bez znakova koji se gube u `.env`, URL-u i ODBC stringu."""
    azbuka = string.ascii_letters + string.digits + "-_.~"
    return "".join(secrets.choice(azbuka) for _ in range(duzina))


def skripta(nalog: str = "sefsync_erp", lozinka: str | None = None) -> dict:
    """Naredbe za zaseban nalog panela i red koji ide u `.env`.

    Stari nalog se **ne dira**. `elbraco_dev_ro` koristi drugi projekat, pa mu se
    ovim putem nista ne oduzima (Bane, 06.10.: "ovo elbraco dev ro ne bismo
    smeli tek tako oduzimati kad ga koristi drugi projekat").
    """
    podesavanja = get_settings()
    url = resolve_erp_url()
    if not url:
        return {"greska": "Veza ka ERP bazi nije podešena."}
    osnovna = make_url(url)
    lozinka = lozinka or nova_lozinka()

    radna = osnovna.database or "ELBS_2026"
    rep = radna.rsplit("_", 1)[-1]
    godina = int(rep) if rep.isdigit() else 2026
    test = podesavanja.erp_test_db_name or ""

    citanje = [f"ELBS_{g}" for g in range(godina, godina - GODINA_UNAZAD - 1, -1)]
    if test and test not in citanje:
        citanje.append(test)
    upis = [radna] + ([test] if test and test != radna else [])

    # Skripta se sme pustiti vise puta: ako nalog vec postoji, samo mu se
    # postavi lozinka. Bez toga bi drugo pustanje puklo na CREATE LOGIN, a bas
    # se ponavlja kad se lozinka iz .env-a i ona iz skripte raziđu.
    redovi = [
        "-- Zaseban nalog za panel e-faktura.",
        "-- Pokrece ga neko sa pravima na SQL serveru; aplikacija ovo ne moze sama.",
        "-- Stari nalog se ne dira - koristi ga drugi projekat.",
        "-- Sme da se pusti vise puta: nista ne duplira, samo dopuni sta fali.",
        "",
        "USE master;",
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{nalog}')",
        f"    CREATE LOGIN [{nalog}] WITH PASSWORD = '{lozinka}',",
        f"        DEFAULT_DATABASE = [{radna}], CHECK_POLICY = ON;",
        "ELSE",
        f"    ALTER LOGIN [{nalog}] WITH PASSWORD = '{lozinka}';",
        "",
    ]
    for baza in citanje:
        redovi.append(f"USE [{baza}];")
        redovi.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '{nalog}')"
        )
        redovi.append(f"    CREATE USER [{nalog}] FOR LOGIN [{nalog}];")
        redovi.append("IF NOT EXISTS (SELECT 1 FROM sys.database_role_members m")
        redovi.append("    JOIN sys.database_principals r ON r.principal_id = m.role_principal_id")
        redovi.append("    JOIN sys.database_principals u ON u.principal_id = m.member_principal_id")
        redovi.append(f"    WHERE r.name = 'db_datareader' AND u.name = '{nalog}')")
        redovi.append(f"    ALTER ROLE db_datareader ADD MEMBER [{nalog}];")
        if baza in upis:
            for tabela in TABELE_UPISA:
                redovi.append(f"GRANT INSERT ON dbo.{tabela} TO [{nalog}];")
        redovi.append("")
    redovi += [
        "-- Nista vise: ni UPDATE, ni DELETE, ni EXECUTE, ni INSERT na DARTIKLI",
        "-- (nju puni nocni posao), ni pristup bazama Logistike i ostalih projekata.",
        "",
        "-- ---------------------------------------------------------------------",
        "-- Provera. Pusti i ovaj red, pa pogledaj sta je vratio:",
        f"--   server - mora da bude onaj na koji se panel kaci ({osnovna.host})",
        "--   nalog  - NULL znaci da naredbe iznad NISU prosle",
        "-- Nalog koji se vidi ovde, a prijava i dalje pada, znaci da lozinka u",
        "-- .env-u nije iz ovog fajla - prepisi je iz komentara na dnu.",
        "USE master;",
        f"SELECT @@SERVERNAME AS server, SUSER_ID('{nalog}') AS nalog;",
    ]

    veza = osnovna.set(username=nalog, password=lozinka).render_as_string(hide_password=False)
    return {
        "nalog": nalog,
        "lozinka": lozinka,
        "citanje": citanje,
        "upis": upis,
        "sql": "\n".join(redovi),
        "env": f"ERP_DB_URL={veza}",
    }
