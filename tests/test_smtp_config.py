"""Razresavanje SMTP podesavanja iz .env i iz tudjeg web.config-a."""

from __future__ import annotations

import pytest

from sefsync.config import get_settings
from sefsync.erp.dotnet_secrets import SecretsError, load_webconfig_env
from sefsync.notify.email_channel import EmailChannel
from sefsync.notify.smtp_config import resolve_smtp

WEB_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <location path="." inheritInChildApplications="false">
    <system.webServer>
      <aspNetCore processPath="dotnet">
        <environmentVariables>
          <environmentVariable name="Mail__Host" value="smtp.gmail.com" />
          <environmentVariable name="Mail__Port" value="587" />
          <environmentVariable name="Mail__User" value="elbracogroup@gmail.com" />
          <environmentVariable name="Mail__Password" value="tajna-app-lozinka" />
          <environmentVariable name="Mail__From" value="elbracogroup@gmail.com" />
          <environmentVariable name="Mail__FromName" value="Elbraco logistika" />
        </environmentVariables>
      </aspNetCore>
    </system.webServer>
  </location>
</configuration>
"""


@pytest.fixture
def web_config(tmp_path):
    path = tmp_path / "web.config"
    path.write_text(WEB_CONFIG, encoding="utf-8")
    return path


def test_citanje_environment_varijabli(web_config):
    env = load_webconfig_env(web_config)

    assert env["Mail__Host"] == "smtp.gmail.com"
    assert env["Mail__FromName"] == "Elbraco logistika"


def test_web_config_bez_stavki_prijavljuje_gresku(tmp_path):
    path = tmp_path / "web.config"
    path.write_text("<configuration />", encoding="utf-8")

    with pytest.raises(SecretsError, match="nema environmentVariable"):
        load_webconfig_env(path)


def test_podesavanja_se_preuzimaju_kad_nasa_nedostaju(web_config, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "")
    monkeypatch.setattr(s, "smtp_from", "")
    monkeypatch.setattr(s, "smtp_port", 25)
    monkeypatch.setattr(s, "smtp_source_file", web_config)

    smtp = resolve_smtp(s)

    assert smtp.host == "smtp.gmail.com"
    assert smtp.port == 587                      # port ide uz host iz istog izvora
    assert smtp.sender == "elbracogroup@gmail.com"
    assert smtp.password == "tajna-app-lozinka"
    assert smtp.configured


def test_nase_podesavanje_ima_prednost(web_config, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "posta.elbraco.rs")
    monkeypatch.setattr(s, "smtp_port", 2525)
    monkeypatch.setattr(s, "smtp_from", "efakture@elbraco.rs")
    monkeypatch.setattr(s, "smtp_password", "nasa")
    monkeypatch.setattr(s, "smtp_source_file", web_config)

    smtp = resolve_smtp(s)

    assert (smtp.host, smtp.port, smtp.sender) == ("posta.elbraco.rs", 2525, "efakture@elbraco.rs")


def test_nedostajuca_podesavanja_se_imenuju(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "")
    monkeypatch.setattr(s, "smtp_from", "")
    monkeypatch.setattr(s, "smtp_source_file", None)

    smtp = resolve_smtp(s)

    assert smtp.missing() == ["SMTP_HOST", "SMTP_FROM"]
    assert not smtp.configured


def test_kanal_kaze_sta_fali_umesto_tihog_preskakanja(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "")
    monkeypatch.setattr(s, "smtp_from", "")
    monkeypatch.setattr(s, "smtp_source_file", None)
    monkeypatch.setattr(s, "notify_email_enabled", True)

    from sefsync.notify.base import Message

    ishodi = EmailChannel(s).send(["pj@elbraco.rs"], Message(subject="x", body_text="y"))

    assert not ishodi[0].ok
    assert "SMTP_HOST" in ishodi[0].error


def test_ime_posiljaoca_ide_u_zaglavlje(web_config, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "")
    monkeypatch.setattr(s, "smtp_from", "")
    monkeypatch.setattr(s, "smtp_source_file", web_config)

    from sefsync.notify.base import Message

    mail = EmailChannel(s)._build(["pj@elbraco.rs"], Message(subject="Test", body_text="telo"))

    assert mail["From"] == "Elbraco logistika <elbracogroup@gmail.com>"


def test_gmail_app_password_sa_razmacima(monkeypatch):
    """Google ga prikazuje kao 'abcd efgh ijkl mnop' — kopira se tako."""
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(s, "smtp_user", "elbracogroup@gmail.com")
    monkeypatch.setattr(s, "smtp_from", "elbracogroup@gmail.com")
    monkeypatch.setattr(s, "smtp_password", "abcd efgh ijkl mnop")

    assert resolve_smtp(s).password == "abcdefghijklmnop"


def test_razmak_u_lozinci_drugog_servera_se_ne_dira(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "posta.elbraco.rs")
    monkeypatch.setattr(s, "smtp_user", "efakture")
    monkeypatch.setattr(s, "smtp_from", "efakture@elbraco.rs")
    monkeypatch.setattr(s, "smtp_password", "dve reci")

    assert resolve_smtp(s).password == "dve reci"


def test_probni_rezim_preusmerava_sva_obavestenja(db, monkeypatch):
    """Dok traje pilot, nijedna poruka ne sme da ode u objekat."""
    from sefsync.models import BusinessUnit, Document, SefStatus, UnitKind
    from sefsync.notify.dispatcher import Dispatcher

    with db.session_scope() as session:
        unit = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL,
                            emails="apatin@elbraco.rs", phones="+381600000000")
        session.add(unit)
        session.flush()
        doc = Document(sef_invoice_id=9001, document_number="1/26", supplier_name="Dobavljač",
                       amount=1200.0, sef_status=SefStatus.NEW, business_unit_id=unit.id)
        session.add(doc)
        session.flush()
        doc_id = doc.id

    poslato: list[tuple[list[str], str]] = []

    class LazniKanal:
        name = "email"

        def enabled(self):
            return True

        def send(self, targets, message):
            from sefsync.notify.base import SendResult

            poslato.append((targets, message.subject))
            return [SendResult(True, t) for t in targets]

    s = get_settings()
    monkeypatch.setattr(s, "notify_override_to", "bane12@elbraco.rs")
    Dispatcher(s, email=LazniKanal()).notify_document(doc_id)

    primaoci, naslov = poslato[0]
    assert primaoci == ["bane12@elbraco.rs"]      # ne apatin@elbraco.rs
    assert naslov.startswith("[PROBA → MP002]")
