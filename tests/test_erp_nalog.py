"""Naredbe za zaseban ERP nalog panela.

Panel pise u ERP kao `elbraco_dev_ro`, nalogom koji deli sa razvojnom masinom i
cija konekcija je pozajmljena od drugog projekta. Ovde se proverava da ono sto
aplikacija sastavi daje tacno onoliko prava koliko joj treba - ni manje (test
baza mora da dobije ARTIKLI/ARTPROD/NAZIVI, bez njih otvaranje sifre u probi
puca na dozvolu), ni vise (DARTIKLI puni nocni posao, aplikacija je ne dira).
"""

from __future__ import annotations

from sefsync.erp import nalog as n

VEZA = (
    "mssql+pyodbc://elbraco_dev_ro:taj-na@194.146.57.216:1433/ELBS_2026"
    "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
)


def _skripta(monkeypatch, **podesi):
    monkeypatch.setattr(n, "resolve_erp_url", lambda: VEZA)

    class Lazna:
        erp_test_db_name = podesi.get("test", "ELBSX_2026")
        erp_db_url = None
        erp_secrets_file = None

    monkeypatch.setattr(n, "get_settings", lambda: Lazna())
    return n.skripta("sefsync_erp", lozinka="l-o-z-1-n-k-a")


def _naredbe(sql: str) -> str:
    """Skripta bez komentara - u komentarima se te reci spominju namerno."""
    redovi = [r for r in sql.splitlines() if not r.lstrip().startswith("--")]
    return chr(10).join(redovi).upper()


def test_pise_samo_u_tekucu_i_test_bazu(monkeypatch):
    s = _skripta(monkeypatch)

    assert s["upis"] == ["ELBS_2026", "ELBSX_2026"]
    assert s["citanje"] == [
        "ELBS_2026",
        "ELBS_2025",
        "ELBS_2024",
        "ELBS_2023",
        "ELBS_2022",
        "ELBSX_2026",
    ]
    # Stare godine dobijaju citanje, a ni jedan GRANT INSERT.
    for stara in ("ELBS_2025", "ELBS_2024", "ELBS_2023", "ELBS_2022"):
        deo = s["sql"].split(f"USE [{stara}];", 1)[1].split("USE [", 1)[0]
        assert "GRANT INSERT" not in deo


def test_test_baza_dobija_i_sifarnike(monkeypatch):
    """Bez ovoga otvaranje sifre i komitenta pre prelaska na produkciju puca."""
    s = _skripta(monkeypatch)
    deo = s["sql"].split("USE [ELBSX_2026];", 1)[1]

    for tabela in ("ARTIKLI", "ARTPROD", "NAZIVI"):
        assert f"GRANT INSERT ON dbo.{tabela} TO [sefsync_erp];" in deo


def test_dartikli_ne_dobija_dozvolu(monkeypatch):
    """Nju puni nocni posao; duplirani kljuc bi ga oborio."""
    s = _skripta(monkeypatch)

    assert "DARTIKLI" not in _naredbe(s["sql"])
    assert "DARTIKLI" not in n.TABELE_UPISA


def test_bez_update_delete_i_rola_koje_pisu(monkeypatch):
    s = _skripta(monkeypatch)
    veliko = _naredbe(s["sql"])

    for zabranjeno in ("UPDATE", "DELETE", "EXECUTE", "DB_DATAWRITER", "DB_OWNER", "SYSADMIN"):
        assert zabranjeno not in veliko


def test_stari_nalog_se_ne_dira(monkeypatch):
    """Nalog koristi drugi projekat - skripta mu nista ne oduzima."""
    s = _skripta(monkeypatch)

    assert "elbraco_dev_ro" not in s["sql"]
    assert "REVOKE" not in _naredbe(s["sql"])
    assert "DROP" not in _naredbe(s["sql"])


def test_env_red_nosi_novi_nalog_i_istu_bazu(monkeypatch):
    s = _skripta(monkeypatch)

    assert s["env"].startswith("ERP_DB_URL=mssql+pyodbc://sefsync_erp:l-o-z-1-n-k-a@")
    assert "194.146.57.216:1433/ELBS_2026" in s["env"]
    assert "driver=ODBC+Driver+18+for+SQL+Server" in s["env"]
    # Lozinka starog naloga ne sme da se prepise u novu vezu.
    assert "taj-na" not in s["env"]


def test_bez_test_baze_nema_praznog_use(monkeypatch):
    s = _skripta(monkeypatch, test="")

    assert s["upis"] == ["ELBS_2026"]
    assert "USE [];" not in s["sql"]


def test_izmisljena_lozinka_prolazi_kroz_url_nedirnuta():
    lozinka = n.nova_lozinka()

    assert len(lozinka) == 28
    # Znakovi koji bi se u .env-u, URL-u ili ODBC stringu izgubili ili prekodirali.
    assert not set(lozinka) & set("@:/?#&%\"' ;=")


def test_skripta_sme_da_se_pusti_dva_puta(monkeypatch):
    """Drugo puštanje ne sme da pukne na CREATE LOGIN - tako se resetuje lozinka."""
    s = _skripta(monkeypatch)

    assert "IF NOT EXISTS (SELECT 1 FROM sys.server_principals" in s["sql"]
    assert "ALTER LOGIN [sefsync_erp] WITH PASSWORD" in s["sql"]
    # CREATE USER i dodavanje u rolu takođe pod uslovom, u svakoj bazi.
    assert s["sql"].count("IF NOT EXISTS (SELECT 1 FROM sys.database_principals") == 6
    assert s["sql"].count("ALTER ROLE db_datareader ADD MEMBER") == 6


def test_objasnjenje_za_odbijenu_prijavu():
    redovi = n.objasni_gresku(RuntimeError("[28000] Login failed for user 'x'. (18456)"), "x")
    tekst = chr(10).join(redovi)

    assert "odbijena" in tekst
    assert "ISTOG pokretanja" in tekst
    assert "ALTER LOGIN [x] WITH PASSWORD" in tekst
    # Bez traceback-a i bez ODBC smeća.
    assert "28000" not in tekst


def test_objasnjenje_razlikuje_bazu_od_prijave():
    nema_baze = n.objasni_gresku(RuntimeError("(4060) Cannot open database"), "x")
    mreza = n.objasni_gresku(RuntimeError("[08001] server does not exist"), "x")

    assert "CREATE USER" in chr(10).join(nema_baze)
    assert "mreža" in chr(10).join(mreza)


def test_skripta_nosi_sopstvenu_proveru(monkeypatch):
    """Da SSMS odmah kaze da li je proslo i na kom serveru - bez pogadjanja."""
    s = _skripta(monkeypatch)

    assert "SELECT @@SERVERNAME AS server, SUSER_ID('sefsync_erp') AS nalog;" in s["sql"]
    assert "194.146.57.216" in s["sql"]


def test_nalog_za_razvojnu_ne_pise_u_produkciju(monkeypatch):
    """Inace bi razvojna i server opet delili nalog, pa bi razdvajanje bilo samo ime."""
    monkeypatch.setattr(n, "resolve_erp_url", lambda: VEZA)

    class Lazna:
        erp_test_db_name = "ELBSX_2026"
        erp_db_url = None
        erp_secrets_file = None

    monkeypatch.setattr(n, "get_settings", lambda: Lazna())
    s = n.skripta("sefsync_dev", "l-o-z-1-n-k-a", upis_u_produkciju=False)

    assert s["upis"] == ["ELBSX_2026"]
    # Citanje ostaje svuda - razvoj mora da vidi prave podatke.
    assert "ELBS_2026" in s["citanje"]
    produkcija = s["sql"].split("USE [ELBS_2026];", 1)[1].split("USE [", 1)[0]
    assert "GRANT INSERT" not in produkcija
    test = s["sql"].split("USE [ELBSX_2026];", 1)[1]
    assert test.count("GRANT INSERT") == len(n.TABELE_UPISA)


def _pregled(monkeypatch, stanja: dict, radna="ELBS_2026", test="ELBSX_2026"):
    """`pregled()` nad izmerenim stanjem, bez kacenja na pravu bazu."""
    monkeypatch.setattr(n, "resolve_erp_url", lambda: VEZA)
    monkeypatch.setattr(n, "_stanje_baze", lambda osnovna, baza: dict(stanja[baza]))

    class Lazna:
        erp_test_db_name = test
        erp_db_url = VEZA
        erp_secrets_file = None

    monkeypatch.setattr(n, "get_settings", lambda: Lazna())

    class LaznaVeza:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, upit, *a):
            tekst = str(upit)

            class R:
                def scalar(self_):
                    if "suser_sname" in tekst:
                        return "nalog"
                    if "user_name" in tekst:
                        return "nalog"
                    return radna

                def __iter__(self_):
                    return iter((b,) for b in stanja)

            return R()

    monkeypatch.setattr(n, "get_erp_engine", lambda: type("E", (), {"connect": lambda s: LaznaVeza()})())
    return n.pregled()


def test_razvojni_nalog_nije_manjkav(monkeypatch):
    """Nema upis u produkciju jer tako treba - ne sme da se zuti svaki put."""
    sve = {"cita": True, "role": ["db_datareader"], "upis": list(n.TABELE_UPISA)}
    p = _pregled(monkeypatch, {
        "ELBS_2026": {"cita": True, "role": ["db_datareader"], "upis": []},
        "ELBSX_2026": sve,
    })

    assert p["razvojni"] is True
    assert "fali_upis" not in p["baze"]["ELBS_2026"]


def test_nalog_panela_bez_upisa_u_test_bazi_se_prijavljuje(monkeypatch):
    """Obrnut slucaj: pise u produkciju, a u test bazi fali - to je greska."""
    p = _pregled(monkeypatch, {
        "ELBS_2026": {"cita": True, "role": ["db_datareader"], "upis": list(n.TABELE_UPISA)},
        "ELBSX_2026": {"cita": True, "role": ["db_datareader"], "upis": ["ULKALKUL"]},
    })

    assert p["razvojni"] is False
    assert "ARTIKLI" in p["baze"]["ELBSX_2026"]["fali_upis"]
