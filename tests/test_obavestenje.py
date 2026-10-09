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


def test_rucna_potvrda_i_kad_slanje_nije_pokusano():
    """Odobrenje prihvaceno na portalu nema zapis - dugme je i tad potrebno.

    81 odobrenje je 08.10. stajalo bez ikakvog zapisa, pa je `zabelezi_rucno`
    pucala bas na onome cemu je namenjena.
    """
    from sefsync.db import session_scope
    from sefsync.models import (
        Document,
        DocumentType,
        ObavestenjeOPrethodnomPorezu,
        SefStatus,
    )
    from sefsync.services.obavestenje import bez_obavestenja, zabelezi_rucno

    with session_scope() as session:
        doc = Document(
            sef_invoice_id=991002,
            document_number="KO-991002",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            vat_amount=-330.0,
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id
        assert session.get(ObavestenjeOPrethodnomPorezu, doc_id) is None

    poruka = zabelezi_rucno(doc_id, "bogdan")

    assert "KO-991002" in poruka
    with session_scope() as session:
        red = session.get(ObavestenjeOPrethodnomPorezu, doc_id)
        assert red is not None and red.rucno_potvrdio == "bogdan"
        assert red.iznos_pdv == 330.0
        assert red.uspelo is False and red.zavrseno is True
        assert doc_id not in [d.id for d in bez_obavestenja(session)]


# --------------------------------------------------------------------------- #
# telo zahteva po dokumentaciji (API dokumentacija 31.07.2026, str. 174-178)
# --------------------------------------------------------------------------- #

def _odobrenje(**polja):
    from datetime import date as _d

    from sefsync.models import Document, DocumentType

    osnovno = dict(
        sef_invoice_id=1,
        document_number="KO-1",
        document_type=DocumentType.CREDIT_NOTE,
        supplier_vat="101599656",
        issue_date=_d(2026, 10, 2),
        vat_amount=-1200.0,
    )
    osnovno.update(polja)
    return Document(**osnovno)


def test_period_je_obavezan_kad_odobrenje_pokriva_vise_faktura():
    """Tabela polja, str. 177: Mandatory when CreditNoteReferenceToPeriod.

    U proznom delu (pravilo 12 i 13) stoji „опционо“ i zato je ovo promaseno —
    cetiri obavestenja su 02.10. pala sa 400, sva na vise faktura.
    """
    from datetime import date as _d

    from sefsync.services.obavestenje import telo_zahteva

    doc = _odobrenje(
        billing_reference_count=3,
        period_start=_d(2026, 9, 1),
        period_end=_d(2026, 9, 30),
    )
    telo = telo_zahteva(doc)

    assert telo["documentReferenceType"] == "CreditNoteReferenceToPeriod"
    assert telo["relatedInvoicePeriodStartDate"] == "2026-09-01"
    assert telo["relatedInvoicePeriodEndDate"] == "2026-09-30"


def test_bez_perioda_u_ublu_pada_na_datum_odobrenja():
    """Bez ikakvog perioda zahtev sigurno pada, pa je i gruba procena bolja."""
    from sefsync.services.obavestenje import telo_zahteva

    telo = telo_zahteva(_odobrenje(billing_reference_count=2))

    assert telo["relatedInvoicePeriodStartDate"] == "2026-10-02"
    assert telo["relatedInvoicePeriodEndDate"] == "2026-10-02"


def test_jedna_faktura_ne_nosi_period():
    from sefsync.services.obavestenje import telo_zahteva

    telo = telo_zahteva(_odobrenje(billing_reference_count=1))

    assert telo["documentReferenceType"] == "CreditNoteReferenceToInvoice"
    assert "relatedInvoicePeriodStartDate" not in telo


def test_odobrenje_na_avans_ima_svoju_vrstu_veze():
    """Poslovno pravilo 5 dopusta i CreditNoteReferenceToPrepaymentInvoice."""
    from datetime import date as _d

    from sefsync.services.obavestenje import telo_zahteva

    telo = telo_zahteva(_odobrenje(billing_reference_count=1, prepayment_date=_d(2026, 9, 15)))

    assert telo["documentReferenceType"] == "CreditNoteReferenceToPrepaymentInvoice"


def test_jbkjs_ili_maticni_ide_u_registrationCode():
    """Pravilo 17: obavezno za korisnika javnih sredstava; SEF nam tu i salje JBKJS."""
    from sefsync.services.obavestenje import telo_zahteva

    telo = telo_zahteva(_odobrenje(supplier_reg_no="82080"))
    assert telo["recipient"]["registrationCode"] == "82080"

    bez = telo_zahteva(_odobrenje(supplier_reg_no=None))
    assert "registrationCode" not in bez["recipient"]


def test_uskladjivanje_prepoznaje_obavestenje_poslato_sa_portala():
    """SEF zna i za ono što je čovek poslao rukom — to je dokaz, ne pretpostavka."""
    from datetime import date as _d

    from sefsync.db import session_scope
    from sefsync.models import (
        Document,
        DocumentType,
        ObavestenjeOPrethodnomPorezu,
        SefStatus,
    )
    from sefsync.services.obavestenje import bez_obavestenja, uskladi_sa_sefom

    with session_scope() as session:
        po_dokumentu = Document(
            sef_invoice_id=771001,
            document_number="KO-A",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            supplier_vat="100000001",
            vat_amount=-500.0,
        )
        po_broju = Document(
            sef_invoice_id=771002,
            document_number="KO-B",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            supplier_vat="100000002",
            vat_amount=-600.0,
        )
        nema = Document(
            sef_invoice_id=771003,
            document_number="KO-C",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            supplier_vat="100000003",
            vat_amount=-700.0,
        )
        session.add_all([po_dokumentu, po_broju, nema])
        session.flush()
        ids = (po_dokumentu.id, po_broju.id, nema.id)

    class LazniSef:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def poslata_obavestenja(self, od, do):
            return [
                {"Id": 11, "NoticeNumber": "KO-A", "PurchaseInvoiceId": 771001,
                 "SendingStatus": "Sent", "Recipient": {"VatRegistrationCode": "100000001"}},
                {"Id": 12, "NoticeNumber": "KO-B", "PurchaseInvoiceId": None,
                 "SendingStatus": "Sent", "Recipient": {"VatRegistrationCode": "100000002"}},
            ]

    nalaz = uskladi_sa_sefom(_d(2026, 1, 1), _d(2026, 12, 31), klijent=LazniSef())

    assert nalaz["po_dokumentu"] == 1
    assert nalaz["po_broju"] == 1
    assert nalaz["nema_obavestenje"] == [ids[2]]
    assert nalaz["zabelezeno"] == 0  # bez --stvarno se nista ne upisuje

    nalaz = uskladi_sa_sefom(
        _d(2026, 1, 1), _d(2026, 12, 31), stvarno=True, klijent=LazniSef()
    )

    assert nalaz["zabelezeno"] == 2
    with session_scope() as session:
        red = session.get(ObavestenjeOPrethodnomPorezu, ids[0])
        assert red is not None and red.sef_notice_id == 11 and red.uspelo is True
        ostali = [d.id for d in bez_obavestenja(session)]
        assert ostali == [ids[2]]


def test_obavestenje_u_statusu_greske_nije_zavrseno():
    """SEF ga ima, ali ga nije poslao dalje — dobavljač ga nema."""
    from datetime import date as _d

    from sefsync.db import session_scope
    from sefsync.models import Document, DocumentType, SefStatus
    from sefsync.services.obavestenje import uskladi_sa_sefom

    with session_scope() as session:
        doc = Document(
            sef_invoice_id=772001,
            document_number="KO-G",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            supplier_vat="100000009",
            vat_amount=-900.0,
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id

    class LazniSef:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def poslata_obavestenja(self, od, do):
            return [
                {"Id": 99, "NoticeNumber": "KO-G", "PurchaseInvoiceId": 772001,
                 "SendingStatus": "Mistake",
                 "Recipient": {"VatRegistrationCode": "100000009"}},
            ]

    nalaz = uskladi_sa_sefom(
        _d(2026, 1, 1), _d(2026, 12, 31), stvarno=True, klijent=LazniSef()
    )

    assert nalaz["sa_greskom"] == [doc_id]
    assert nalaz["po_dokumentu"] == 0
    assert nalaz["zabelezeno"] == 0
    assert nalaz["statusi"] == {"Mistake": 1}


def test_obavestenje_koje_je_proslo_pa_palo_kod_sefa_se_ponovo_otvara():
    """SEF ume da ga posle prebaci u grešku i tada ga ne pošalje dalje.

    Naš zapis o tome ne zna ništa, a `bez_obavestenja` takav dokument preskače
    jer kod nas „uspelo“ — pa ga niko više ne bi pogledao.
    """
    from datetime import date as _d

    from sefsync.db import session_scope
    from sefsync.models import (
        Document,
        DocumentType,
        ObavestenjeOPrethodnomPorezu,
        SefStatus,
    )
    from sefsync.services.obavestenje import bez_obavestenja, uskladi_sa_sefom

    with session_scope() as session:
        doc = Document(
            sef_invoice_id=773001,
            document_number="KO-P",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            supplier_vat="100000011",
            vat_amount=-1100.0,
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id
        session.add(
            ObavestenjeOPrethodnomPorezu(
                document_id=doc_id, broj="KO-P", sef_notice_id=555, poslao="app"
            )
        )
        session.flush()
        # Za nas je zavrseno, pa se ne prijavljuje.
        assert doc_id not in [d.id for d in bez_obavestenja(session)]

    class LazniSef:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def poslata_obavestenja(self, od, do):
            return [{"Id": 555, "NoticeNumber": "KO-P", "SendingStatus": "Mistake",
                     "PurchaseInvoiceId": 773001,
                     "Recipient": {"VatRegistrationCode": "100000011"}}]

    nalaz = uskladi_sa_sefom(
        _d(2026, 1, 1), _d(2026, 12, 31), stvarno=True, klijent=LazniSef()
    )

    assert nalaz["naknadno_palo"] == [doc_id]
    with session_scope() as session:
        red = session.get(ObavestenjeOPrethodnomPorezu, doc_id)
        assert red.uspelo is False and "Mistake" in (red.greska or "")
        assert doc_id in [d.id for d in bez_obavestenja(session)]


def test_poklapanje_po_broju_trpi_RS_prefiks_i_razmake(db):
    """SEF vraća PIB kao „RS100000104“, mi čuvamo „100000104“.

    Zbog te razlike poklapanje po broju nikad nije proradilo (`po_broju: 0` na
    1.358 obaveštenja), pa je komanda prijavljivala kao neposlata ona koja na
    SEF-u postoje — a to vodi u drugo obaveštenje dobavljaču, koje se ne može
    povući. Bane je to uhvatio ručnom proverom 09.10.
    """
    from datetime import date as _d

    from sefsync.db import session_scope
    from sefsync.models import Document, DocumentType, SefStatus
    from sefsync.services.obavestenje import bez_obavestenja, uskladi_sa_sefom

    with session_scope() as session:
        doc = Document(
            sef_invoice_id=774001,
            document_number="RS2604421",
            document_type=DocumentType.CREDIT_NOTE,
            sef_status=SefStatus.APPROVED,
            supplier_vat="100000104",
            vat_amount=-289.99,
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id

    class LazniSef:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def poslata_obavestenja(self, od, do):
            # Bez PurchaseInvoiceId, PIB sa prefiksom, broj sa razmacima.
            return [{
                "Id": 777, "NoticeNumber": " rs2604421 ", "PurchaseInvoiceId": None,
                "SendingStatus": "Sent",
                "Recipient": {"VatRegistrationCode": "RS100000104"},
            }]

    nalaz = uskladi_sa_sefom(
        _d(2026, 1, 1), _d(2026, 12, 31), stvarno=True, klijent=LazniSef()
    )

    assert nalaz["po_broju"] == 1
    assert nalaz["nema_obavestenje"] == []
    with session_scope() as session:
        assert doc_id not in [d.id for d in bez_obavestenja(session)]


def test_broj_izvornog_dokumenta_se_ne_skracuje():
    """SEF čuva broj doslovno; skraćen broj daje „Source document not found“.

    KIM-TEC šalje `90002972  ` sa dva razmaka na kraju — provereno 09.10. i u
    SEF-ovom pregledu (`DocumentNumber`) i u UBL-u (`cbc:ID`). Zbog skraćivanja
    su obaveštenja za ta odobrenja padala sa 400 od 02.10.
    """
    from sefsync.services.obavestenje import telo_zahteva

    telo = telo_zahteva(_odobrenje(document_number="90002972  ", billing_reference_count=1))

    assert telo["relatedDocumentNumber"] == "90002972  "
    # Nas broj obavestenja ostaje uredan.
    assert telo["noticeNumber"] == "90002972"
