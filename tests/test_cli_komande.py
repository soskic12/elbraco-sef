"""Komande se bar jednom pokrenu.

Dve komande su otisle na server sa `NameError: name 'select' is not defined`,
jer ih nijedan test nije ni pozvao - greska se videla tek kad ju je Bane
pokrenuo na dan prelaska. Ovo su dimni testovi: ne proveravaju ishod nego da
komanda uopste prolazi kroz svoj kod.
"""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from sefsync.cli import app
from sefsync.config import get_settings

runner = CliRunner()


def _pokreni(*argumenti):
    ishod = runner.invoke(app, list(argumenti))
    if ishod.exception and not isinstance(ishod.exception, SystemExit):
        raise ishod.exception
    return ishod


def test_sumnje_prolazi_i_kad_nema_predloga(db):
    ishod = _pokreni("sumnje")
    assert "Nema nijednog predloga" in ishod.stdout


def test_sumnje_ispisuje_predlog(db, monkeypatch):
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=1, document_number="OTP1882131",
                       supplier_name="OTP BANKA")
        session.add(doc)
        session.flush()
        session.add(NacrtKalkulacije(
            document_id=doc.id, stanje=StanjeNacrta.SPREMAN, magacin="088",
            sumnja={"oznaka": "ULKALKUL 088/02874", "razlog": "razlika u jednom znaku"},
        ))

    ishod = _pokreni("sumnje")
    assert "OTP1882131" in ishod.stdout
    assert "ULKALKUL 088/02874" in ishod.stdout
    assert "čeka odluku" in ishod.stdout


def test_arhiviraj_stare_trazi_granicu(db, monkeypatch):
    """Bez NOVI_TOK_OD se ne zna sta je staro - komanda odbija da radi."""
    monkeypatch.setattr(get_settings(), "novi_tok_od", None, raising=False)
    ishod = _pokreni("arhiviraj-stare")
    assert ishod.exit_code == 1
    assert "NOVI_TOK_OD nije zadat" in ishod.stdout


def test_arhiviraj_stare_dira_samo_staro_i_gotovo(db, monkeypatch):
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta

    monkeypatch.setattr(get_settings(), "novi_tok_od", dt.date(2026, 9, 28), raising=False)
    staro = dt.datetime(2026, 9, 20, 9, 0)
    novo = dt.datetime(2026, 9, 28, 9, 0)

    with db.session_scope() as session:
        slucajevi = [
            ("STARO-GOTOVO", staro, StanjeNacrta.UPISAN),
            ("STARO-AVANS", staro, StanjeNacrta.NE_TREBA),
            ("STARO-CEKA", staro, StanjeNacrta.CEKA_SIFRU),    # posao jos postoji
            ("STARO-SPREMNO", staro, StanjeNacrta.SPREMAN),    # posao jos postoji
            ("NOVO-GOTOVO", novo, StanjeNacrta.UPISAN),        # nije staro
        ]
        for broj, kada, stanje in slucajevi:
            doc = Document(sef_invoice_id=hash(broj) % 10**6, document_number=broj,
                           created_at=kada)
            session.add(doc)
            session.flush()
            session.add(NacrtKalkulacije(document_id=doc.id, stanje=stanje))

    suvo = _pokreni("arhiviraj-stare")
    assert "STARO-GOTOVO" in suvo.stdout or "2" in suvo.stdout
    assert "Suvi prolaz" in suvo.stdout

    with db.session_scope() as session:
        assert not any(d.archived for d in session.query(Document).all())

    _pokreni("arhiviraj-stare", "--stvarno")

    with db.session_scope() as session:
        stanje = {d.document_number: d.archived for d in session.query(Document).all()}
    assert stanje["STARO-GOTOVO"] is True
    assert stanje["STARO-AVANS"] is True
    assert stanje["STARO-CEKA"] is False
    assert stanje["STARO-SPREMNO"] is False
    assert stanje["NOVO-GOTOVO"] is False
