"""Citanje ERP konekcije iz .NET user-secrets skladista (bez kopiranja lozinke)."""

from __future__ import annotations

import json

import pytest

from sefsync.erp.dotnet_secrets import (
    SecretsError,
    describe,
    dotnet_to_sqlalchemy,
    load_connection_url,
)

CS = (
    "Server=194.146.57.216,1433;Database=ELBS_2026;User Id=elbraco_dev_ro;"
    "Password=taj-na;TrustServerCertificate=True;Connect Timeout=30;"
)


def test_prevod_konekcije():
    url = dotnet_to_sqlalchemy(CS)

    assert url.startswith("mssql+pyodbc://elbraco_dev_ro:taj-na@194.146.57.216:1433/ELBS_2026?")
    assert "driver=ODBC+Driver+18+for+SQL+Server" in url
    assert "TrustServerCertificate=yes" in url


def test_lozinka_sa_specijalnim_znacima_se_koduje():
    url = dotnet_to_sqlalchemy("Server=srv;Database=B;User Id=u;Password=a@b:c/d;")

    assert "a%40b%3Ac%2Fd@srv" in url


def test_windows_autentikacija_bez_naloga():
    url = dotnet_to_sqlalchemy("Server=srv;Database=B;Trusted_Connection=True;")

    assert "@" not in url.split("//", 1)[1].split("?")[0].replace("srv", "")
    assert "trusted_connection=yes" in url


def test_ucitavanje_iz_fajla(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"ConnectionStrings:ErpDatabase": CS}), encoding="utf-8")

    url = load_connection_url(path)
    assert "ELBS_2026" in url


def test_ucitavanje_iz_ugnjezdenog_oblika(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"ConnectionStrings": {"ErpDatabase": CS}}), encoding="utf-8")

    assert "ELBS_2026" in load_connection_url(path)


def test_jasna_greska_kad_nema_fajla_ili_kljuca(tmp_path):
    with pytest.raises(SecretsError, match="Nema secrets fajla"):
        load_connection_url(tmp_path / "nema.json")

    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"Nesto:Drugo": "x"}), encoding="utf-8")
    with pytest.raises(SecretsError, match="nema ključa"):
        load_connection_url(path)


def test_opis_sakriva_lozinku():
    assert describe(dotnet_to_sqlalchemy(CS)) == (
        "mssql+pyodbc://elbraco_dev_ro:***@194.146.57.216:1433/ELBS_2026"
        "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
    )


WEB_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration><location><system.webServer><aspNetCore>
  <environmentVariables>
    <environmentVariable name="ConnectionStrings__ErpDatabase"
      value="Server=194.146.57.216,1433;Database=ELBS_2026;User Id=citac;Password=tajna;TrustServerCertificate=True;" />
    <environmentVariable name="Mail__Host" value="smtp.gmail.com" />
  </environmentVariables>
</aspNetCore></system.webServer></location></configuration>
"""


def test_veza_iz_web_configa_sa_servera(tmp_path):
    """Na serveru kredencijali stoje u web.config-u sajta, ne u user-secrets."""
    path = tmp_path / "web.config"
    path.write_text(WEB_CONFIG, encoding="utf-8")

    url = load_connection_url(path, "ConnectionStrings__ErpDatabase")

    assert url.startswith("mssql+pyodbc://citac:tajna@194.146.57.216:1433/ELBS_2026")


def test_kljuc_se_prihvata_u_oba_zapisa(tmp_path):
    path = tmp_path / "web.config"
    path.write_text(WEB_CONFIG, encoding="utf-8")

    assert load_connection_url(path, "ConnectionStrings:ErpDatabase") == load_connection_url(
        path, "ConnectionStrings__ErpDatabase"
    )
