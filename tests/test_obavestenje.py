"""Obaveštenje o prethodnom porezu — slanje je nepovratno."""

from __future__ import annotations

import pytest

from sefsync.models import (
    Document,
    DocumentType,
    ObavestenjeOPrethodnomPorezu,
    SefStatus,
    utcnow,
)


def _odobrenje_sa_palim_obavestenjem(db):
    import datetime as dt

    with db.session_scope() as session:
        doc = Document(
            sef_invoice_id=479537104, document_number="FA-KO-92/26",
            supplier_name="VIMPORT", supplier_vat="101743120",
            document_type=DocumentType.CREDIT_NOTE, amount=-281400.0,
            vat_amount=-46900.0, sef_status=SefStatus.APPROVED,
            issue_date=dt.date(2026, 10, 1),
        )
        session.add(doc)
        session.flush()
        session.add(ObavestenjeOPrethodnomPorezu(
            document_id=doc.id, broj="FA-KO-92/26", poslao="bogdan",
            poslato_u=utcnow(), greska="SEF greška 400",
        ))
        return doc.id


def test_rucno_napravljeno_obavestenje_se_belezi(db):
    """Ako je već napravljeno na portalu, app više ne sme da nudi slanje.

    Bez toga bi izveštaj zauvek prijavljivao grešku, a neko bi pre ili kasnije
    kliknuo „pošalji ponovo“ i poslao dobavljaču **drugo** obaveštenje — što se
    ne može povući (Bane, 07.10.).
    """
    from sefsync.services.obavestenje import zabelezi_rucno

    doc_id = _odobrenje_sa_palim_obavestenjem(db)

    poruka = zabelezi_rucno(doc_id, "bogdan")

    assert "ručno na portalu" in poruka
    with db.session_scope() as session:
        red = session.get(ObavestenjeOPrethodnomPorezu, doc_id)
        assert red.rucno_u is not None and red.rucno_potvrdio == "bogdan"
        assert red.zavrseno is True
        assert red.uspelo is False, "nije ga poslala app — to ostaje tačno"


def test_vec_poslato_app_se_ne_belezi_kao_rucno(db):
    from sefsync.services.obavestenje import zabelezi_rucno

    doc_id = _odobrenje_sa_palim_obavestenjem(db)
    with db.session_scope() as session:
        red = session.get(ObavestenjeOPrethodnomPorezu, doc_id)
        red.sef_notice_id = 12345
        red.greska = None

    poruka = zabelezi_rucno(doc_id, "bogdan")

    assert "već poslala" in poruka
    with db.session_scope() as session:
        assert session.get(ObavestenjeOPrethodnomPorezu, doc_id).rucno_u is None


def test_bez_zapisa_nema_sta_da_se_belezi(db):
    from sefsync.services.obavestenje import NeMozeObavestenje, zabelezi_rucno

    with pytest.raises(NeMozeObavestenje):
        zabelezi_rucno(999, "bogdan")


def test_rucno_napravljeno_nije_propalo(tmp_path, monkeypatch):
    """Obavestenje koje je covek napravio na portalu dobavljac ima - ne prijavljuj ga.

    Do 08.10. je `bez_obavestenja` gledala `uspelo`, pa su ona cetiri koja je
    Bogdan napravio rucno zauvek ulazila u spisak propalih i naduvavala broj u
    `provera-prelaska`.
    """
    from sefsync.db import session_scope
    from sefsync.models import (
        Document,
        DocumentType,
        ObavestenjeOPrethodnomPorezu,
        SefStatus,
        utcnow,
    )
    from sefsync.services.obavestenje import bez_obavestenja

    with session_scope() as session:
        doc = Document(
            sef_invoice_id=991001,
            document_number="KO-991001",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            vat_amount=-1200.0,
        )
        session.add(doc)
        session.flush()

        # Bez ikakvog zapisa - jeste propalo.
        assert [d.id for d in bez_obavestenja(session)] == [doc.id]

        zapis = ObavestenjeOPrethodnomPorezu(
            document_id=doc.id, broj="KO-991001", poslao="app", greska="SEF 400"
        )
        session.add(zapis)
        session.flush()
        assert [d.id for d in bez_obavestenja(session)] == [doc.id]

        # Covek ga napravio na portalu - vise nije propalo.
        zapis.rucno_potvrdio = "bogdan"
        zapis.rucno_u = utcnow()
        session.flush()
        assert bez_obavestenja(session) == []
