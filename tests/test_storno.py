"""Storno: panel ga sazna iz SEF-ovih dogadjaja, a obavestenje ide dugmetom."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from sefsync.models import (
    AuditLog,
    Document,
    DocumentType,
    ObavestenjeOPrethodnomPorezu,
    SefStatus,
    Setting,
)


class LazniSef:
    """SEF koji za zadate dane vraca zadate dogadjaje i pamti sta je poslato."""

    def __init__(self, dogadjaji=None, zapis_fakture=None, pregled=None):
        self.dogadjaji = dogadjaji or {}
        self.zapis_fakture = zapis_fakture or {}
        self.pregled = pregled or []
        self.citani_dani: list[date] = []
        self.poslato: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def purchase_changes(self, dan):
        self.citani_dani.append(dan)
        return self.dogadjaji.get(dan, [])

    def purchase_invoice(self, invoice_id):
        return self.zapis_fakture

    def purchase_overview(self, od, do):
        return self.pregled

    def posalji_obavestenje_o_prethodnom_porezu(self, telo):
        self.poslato.append(telo)
        return {"Id": 999, "SendingStatus": "Sent"}


def _faktura(db, **polja) -> int:
    from datetime import datetime

    osnovno = dict(
        sef_invoice_id=453705773,
        document_number="R-26061900",
        document_type=DocumentType.INVOICE,
        supplier_name="Comtrade Distribution",
        supplier_vat="100000104",
        supplier_reg_no="17172140",
        issue_date=date(2026, 7, 23),
        sent_date=datetime(2026, 7, 23, 12, 56, 51, 490891),
        vat_amount=40553.0,
        sef_status=SefStatus.APPROVED,
    )
    osnovno.update(polja)
    with db.session_scope() as session:
        doc = Document(**osnovno)
        session.add(doc)
        session.flush()
        return doc.id


STORNO_24_07 = {
    "EventId": 1,
    "Date": "2026-07-24T13:23:14.9212074Z",
    "NewInvoiceStatus": "Storno",
    "PurchaseInvoiceId": 453705773,
    "StornoNumber": "ST-1",
}


def test_storno_iz_dogadjaja_se_belezi_sa_trenutkom(db):
    """Pregled storno starije fakture ne vraca - mereno 13.10. na R-26061900."""
    from sefsync.services.storno import procitaj_promene, trenutak_storna

    doc_id = _faktura(db)
    sef = LazniSef({date(2026, 7, 24): [STORNO_24_07, {"NewInvoiceStatus": "Seen"}]})

    ishod = procitaj_promene(klijent=sef, danas=date(2026, 7, 25))

    assert (ishod["storna"], ishod["novih"], ishod["zatecenih"]) == (1, 1, 0)
    with db.session_scope() as session:
        assert session.get(Document, doc_id).sef_status is SefStatus.STORNO
        assert trenutak_storna(session, doc_id) == "2026-07-24T13:23:14.9212074Z"


def test_dan_se_cita_jednom(db):
    from sefsync.services.storno import procitaj_promene

    sef = LazniSef()
    procitaj_promene(klijent=sef, danas=date(2026, 10, 14))
    assert sef.citani_dani[0] == date(2026, 9, 14), "prvi put svih 30 dana"
    assert sef.citani_dani[-1] == date(2026, 10, 13)

    sef.citani_dani.clear()
    procitaj_promene(klijent=sef, danas=date(2026, 10, 14))
    procitaj_promene(klijent=sef, danas=date(2026, 10, 16))

    assert sef.citani_dani == [date(2026, 10, 14), date(2026, 10, 15)]
    with db.session_scope() as session:
        assert session.get(Setting, "storno_promene_do").value == "2026-10-15"


def test_zateceno_storno_ispravlja_status_ali_se_ne_nudi(db):
    """Mereno 13.10.: 2026-6919 stornirana 17.09. stajala je kod nas kao Approved.

    Status mora da se ispravi - inace bi se za stornirani dokument upisala
    kalkulacija - ali obavestenje se za zaostala storna ne nudi (Bane, 13.10.).
    """
    from sefsync.services.obavestenje import bez_obavestenja
    from sefsync.services.storno import procitaj_promene

    staro = _faktura(db, sef_invoice_id=471935557, document_number="2026-6919")
    novo = _faktura(db)
    sef = LazniSef({
        date(2026, 9, 17): [{"Date": "2026-09-17T08:07:56.7682175Z",
                             "NewInvoiceStatus": "Storno", "PurchaseInvoiceId": 471935557}],
        date(2026, 10, 13): [dict(STORNO_24_07, Date="2026-10-13T10:00:00Z")],
    })

    ishod = procitaj_promene(klijent=sef, danas=date(2026, 10, 14))

    assert (ishod["novih"], ishod["zatecenih"]) == (1, 1)
    with db.session_scope() as session:
        assert session.get(Document, staro).sef_status is SefStatus.STORNO
        ids = [d.id for d in bez_obavestenja(session)]
        assert novo in ids and staro not in ids
        assert session.scalars(
            select(AuditLog).where(AuditLog.document_id == staro,
                                   AuditLog.action == "sef.storno.zatecen")
        ).one().detail.startswith("2026-09-17T08:07:56")


def test_isti_storno_dvaput_je_jedan_zapis(db):
    from sefsync.services.storno import procitaj_promene

    doc_id = _faktura(db)
    sef = LazniSef({date(2026, 7, 24): [STORNO_24_07]})
    procitaj_promene(klijent=sef, danas=date(2026, 7, 25))
    drugi = procitaj_promene(klijent=sef, danas=date(2026, 7, 25), od=date(2026, 7, 24))

    assert drugi["novih"] == 0
    with db.session_scope() as session:
        zapisi = session.scalars(
            select(AuditLog).where(AuditLog.document_id == doc_id, AuditLog.action == "sef.storno")
        ).all()
        assert len(zapisi) == 1


def test_telo_storna_kao_bogdanovo_sa_portala(db):
    """Oblik preuzet sa obavestenja koje je Bogdan poslao 27.07. za R-26061900."""
    from sefsync.services.obavestenje import telo_zahteva

    with db.session_scope() as session:
        doc = session.get(Document, _faktura(db, sef_status=SefStatus.STORNO))
        telo = telo_zahteva(
            doc,
            {"DocumentNumber": "R-26061900", "SentDate": "2026-07-23T12:56:51.4908917+00:00"},
            storno_u="2026-07-24T13:23:14.9212074Z",
            napomena="faktura nije knjižena i nije korišten prethodni pdv",
        )

    assert telo["basisOfNotice"] == "Storno"
    assert telo["documentReferenceType"] == "StornoInvoice"
    assert telo["relatedDocumentNumber"] == "R-26061900"
    assert telo["relatedDocumentIssueDate"] == "2026-07-23T12:56:51.4908917+00:00"
    assert telo["relatedDocumentStornoDate"] == "2026-07-24T13:23:14.9212074Z"
    assert telo["totalVatAmount"] == 40553.0
    assert telo["notes"] == "faktura nije knjižena i nije korišten prethodni pdv"
    assert telo["recipient"] == {"vatRegistrationCode": "100000104", "registrationCode": "17172140"}


def test_storno_bez_datuma_storna_se_ne_salje(db):
    """Tabela polja (str. 177): datum storna je obavezan kad je osnov Storno."""
    from sefsync.services.obavestenje import NeMozeObavestenje, telo_zahteva

    with db.session_scope() as session:
        doc = session.get(Document, _faktura(db, sef_status=SefStatus.STORNO))
        with pytest.raises(NeMozeObavestenje, match="storniran"):
            telo_zahteva(doc)


@pytest.mark.parametrize(
    "vrsta, kod, ocekivano",
    [
        (DocumentType.DEBIT_NOTE, None, "StornoDebitNote"),
        (DocumentType.PREPAYMENT, None, "StornoPrepayment"),
        (DocumentType.INVOICE, "386", "StornoPrepayment"),
    ],
)
def test_vrsta_veze_storna(vrsta, kod, ocekivano):
    from sefsync.services.obavestenje import vrsta_veze

    doc = Document(document_type=vrsta, invoice_type_code=kod, sef_status=SefStatus.STORNO)
    assert vrsta_veze(doc) == ocekivano


def test_stornirano_odobrenje_ne_trazi_obavestenje():
    """Pravilo 2: obavestenje ide za odobrenje „које није сторнирано“."""
    from sefsync.services.obavestenje import NeMozeObavestenje, telo_zahteva

    doc = Document(
        document_number="IFKO-6/26", document_type=DocumentType.CREDIT_NOTE,
        supplier_vat="1", issue_date=date(2026, 4, 24), vat_amount=-27745.0,
        sef_status=SefStatus.STORNO,
    )
    with pytest.raises(NeMozeObavestenje, match="stornirano"):
        telo_zahteva(doc)


def test_spisak_nudi_samo_storna_uocena_posle_uvodjenja(db):
    from sefsync.services.obavestenje import bez_obavestenja
    from sefsync.services.storno import procitaj_promene

    novo = _faktura(db)
    staro = _faktura(db, sef_invoice_id=1, document_number="IFK-26-628", sef_status=SefStatus.STORNO)
    procitaj_promene(klijent=LazniSef({date(2026, 7, 24): [STORNO_24_07]}), danas=date(2026, 7, 25))

    with db.session_scope() as session:
        ids = [d.id for d in bez_obavestenja(session)]
    assert novo in ids
    assert staro not in ids


def test_slanje_storna_uzima_trenutak_iz_dogadjaja_i_napomenu(db):
    from sefsync.services.obavestenje import bez_obavestenja, posalji
    from sefsync.services.storno import procitaj_promene

    doc_id = _faktura(db)
    sef = LazniSef(
        {date(2026, 7, 24): [STORNO_24_07]},
        pregled=[{"InvoiceId": 453705773, "DocumentNumber": "R-26061900",
                  "SentDate": "2026-07-23T12:56:51.4908917+00:00"}],
    )
    procitaj_promene(klijent=sef, danas=date(2026, 7, 25))

    ok, poruka = posalji(doc_id, korisnik="bogdan", klijent=sef, napomena="nije knjižena")

    assert ok, poruka
    telo = sef.poslato[0]
    assert telo["basisOfNotice"] == "Storno"
    assert telo["relatedDocumentStornoDate"] == "2026-07-24T13:23:14.9212074Z"
    assert telo["notes"] == "nije knjižena"
    with db.session_scope() as session:
        assert session.get(ObavestenjeOPrethodnomPorezu, doc_id).uspelo
        assert doc_id not in [d.id for d in bez_obavestenja(session)]


def test_bez_dogadjaja_datum_storna_iz_zapisa_fakture(db):
    """`LastModifiedUtc` stornirane fakture = trenutak storna (14 od 14, 13.10.)."""
    from sefsync.services.obavestenje import datum_storna

    with db.session_scope() as session:
        doc = session.get(Document, _faktura(db, sef_status=SefStatus.STORNO))
        sef = LazniSef(zapis_fakture={"Status": "Storno",
                                      "LastModifiedUtc": "2026-07-24T13:23:14.9212074+00:00"})
        assert datum_storna(doc, sef) == "2026-07-24T13:23:14.9212074+00:00"
        sef = LazniSef(zapis_fakture={"Status": "Approved", "LastModifiedUtc": "2026-07-24"})
        assert datum_storna(doc, sef) is None


def test_napomena_zavisi_od_toga_da_li_je_kalkulacija_u_erpu():
    from sefsync.services.obavestenje import predlog_napomene

    assert "nije knjižena" in predlog_napomene(None)
    assert "ispravljen" in predlog_napomene("088/01234")


def _sync(records, faktura_xml):
    import httpx

    from sefsync.config import get_settings
    from sefsync.sef import SefClient
    from sefsync.services.ingest import IngestService

    def handler(request):
        if request.url.path.endswith("/purchase-invoice/overview"):
            return httpx.Response(200, json=records)
        if request.url.path.endswith("/purchase-invoice/xml"):
            return httpx.Response(200, content=faktura_xml)
        return httpx.Response(404)

    class Nista:
        def notify_document(self, doc_id):
            return False

        def maybe_auto_accept(self, doc_id):
            return False

    settings = get_settings()
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.base_url)
    IngestService(
        client=SefClient(settings, client=http), settings=settings,
        notifier=Nista(), acceptor=Nista(),
    ).sync("2026-07-01", "2026-07-31")


def _zapis(status, sent="2026-07-23T12:56:51.4908917+00:00"):
    return {
        "InvoiceId": 453705773, "DocumentNumber": "R-26061900", "DocumentType": "Invoice",
        "Status": status, "SupplierName": "Comtrade", "SupplierVatRegistrationNumber": "100000104",
        "Amount": 243318.0, "SumWithoutVat": 202765.0, "VatAmount": 40553.0,
        "Currency": "RSD", "SentDate": sent,
    }


def test_prelaz_u_storno_kroz_pregled_se_belezi(db, faktura_xml):
    from sefsync.services.storno import je_zabelezen

    _sync([_zapis("Approved")], faktura_xml)
    _sync([_zapis("Storno")], faktura_xml)
    _sync([_zapis("Storno")], faktura_xml)

    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 453705773))
        assert doc.sef_status is SefStatus.STORNO
        assert je_zabelezen(session, doc.id)
        assert len(session.scalars(select(AuditLog).where(AuditLog.action == "sef.storno")).all()) == 1


def test_stari_storno_pri_preuzimanju_istorije_nije_nov(db, faktura_xml):
    """Ponovno preuzimanje istorije ne sme sva stara storna da proglasi novim."""
    from sefsync.services.storno import je_zabelezen

    _sync([_zapis("Storno", sent="2026-01-20T10:00:00+00:00")], faktura_xml)

    with db.session_scope() as session:
        doc = session.scalar(select(Document).where(Document.sef_invoice_id == 453705773))
        assert doc.sef_status is SefStatus.STORNO
        assert not je_zabelezen(session, doc.id)


def test_granica_je_stalna_i_ispravlja_pogresno_nova_storna(db):
    """14.10. 08:35: drugi radnik je granicu izveo iz toga dokle je prvi stigao,
    pa su četiri stara storna (17.09.-09.10.) upisana kao nova. Granica se sada
    zapisuje jednom, a pri zapisivanju se takvi zapisi prevode u zatečene.
    """
    from sefsync.services.obavestenje import bez_obavestenja
    from sefsync.services.storno import procitaj_promene

    staro = _faktura(db, sef_invoice_id=471935557, document_number="2026-6919",
                     sef_status=SefStatus.STORNO)
    with db.session_scope() as session:
        session.add(Setting(key="storno_promene_do", value="2026-10-13"))
        session.add(AuditLog(actor="sef", action="sef.storno", document_id=staro,
                             detail="2026-09-17T08:07:56.7682175Z storno ST2026-344"))

    procitaj_promene(klijent=LazniSef(), danas=date(2026, 10, 15))

    with db.session_scope() as session:
        assert session.get(Setting, "storno_uvedeno").value == "2026-10-13"
        akcije = [a.action for a in session.scalars(
            select(AuditLog).where(AuditLog.document_id == staro))]
        assert akcije == ["sef.storno.zatecen"]
        assert staro not in [d.id for d in bez_obavestenja(session)]

    # Granica se posle ne pomera, ma dokle se citalo.
    procitaj_promene(klijent=LazniSef(), danas=date(2026, 10, 20))
    with db.session_scope() as session:
        assert session.get(Setting, "storno_uvedeno").value == "2026-10-13"


def test_drugi_radnik_ne_proglasava_staro_novim(db):
    """Prvi radnik zapiše granicu i dan, pa padne; drugi nastavlja - staro ostaje staro."""
    from sefsync.services.storno import procitaj_promene

    staro = _faktura(db, sef_invoice_id=471935557, document_number="2026-6919")
    dogadjaj = {date(2026, 9, 17): [{"Date": "2026-09-17T08:07:56Z", "NewInvoiceStatus": "Storno",
                                     "PurchaseInvoiceId": 471935557}]}

    class PadaPosleDana(LazniSef):
        def purchase_changes(self, dan):
            if dan > date(2026, 9, 15):
                raise RuntimeError("SEF ne odgovara")
            return super().purchase_changes(dan)

    with pytest.raises(RuntimeError):
        procitaj_promene(klijent=PadaPosleDana(dogadjaj), danas=date(2026, 10, 14))
    ishod = procitaj_promene(klijent=LazniSef(dogadjaj), danas=date(2026, 10, 14))

    assert (ishod["novih"], ishod["zatecenih"]) == (0, 1)
    with db.session_scope() as session:
        assert [a.action for a in session.scalars(
            select(AuditLog).where(AuditLog.document_id == staro))] == ["sef.storno.zatecen"]
