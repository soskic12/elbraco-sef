"""Faza 2 (nacrt): pretvaranje dokumenta u ulaznu kalkulaciju."""

from __future__ import annotations

from sefsync.erp.calculation import CsvWriter, build_draft
from sefsync.models import (
    BusinessUnit,
    Document,
    DocumentLine,
    DocumentType,
    ItemMapping,
    UnitKind,
)


def _document(session, doc_type=DocumentType.INVOICE) -> int:
    unit = BusinessUnit(code="MP02", name="Maloprodaja Pančevo", kind=UnitKind.RETAIL, erp_code="02")
    session.add(unit)
    session.flush()
    doc = Document(
        sef_invoice_id=1001,
        document_number="2026-114/25",
        document_type=doc_type,
        supplier_name="DOBAVLJAČ TRGOVINA DOO",
        supplier_vat="100200300",
        sum_without_vat=12000.0,
        vat_amount=2400.0,
        amount=14400.0,
        currency="RSD",
        business_unit_id=unit.id,
    )
    doc.lines.append(
        DocumentLine(line_no=1, name="Kabl PPY 3x1.5", sellers_item_id="ART-551",
                     standard_item_id="8600123456789", quantity=10, unit_code="H87",
                     price=1000.0, allowance_amount=1000.0, line_amount=9000.0, vat_percent=20.0)
    )
    doc.lines.append(
        DocumentLine(line_no=2, name="Gibljivo crevo", sellers_item_id="ART-772",
                     buyers_item_id="10045", quantity=50, unit_code="MTR",
                     price=60.0, line_amount=3000.0, vat_percent=20.0)
    )
    session.add(doc)
    session.flush()
    return doc.id


def test_nacrt_racuna_nabavnu_cenu_i_pdv(db):
    with db.session_scope() as session:
        doc_id = _document(session)
        draft = build_draft(session, doc_id)

    assert draft.erp_warehouse_code == "02"
    assert len(draft.lines) == 2

    prva = draft.lines[0]
    assert prva.quantity == 10
    assert prva.net_amount == 9000.0
    assert prva.net_unit_price == 900.0     # nabavna cena posle rabata
    assert prva.discount_amount == 1000.0
    assert prva.vat_amount == 1800.0
    assert not prva.mapped                  # nema mapiranja za ART-551

    druga = draft.lines[1]
    assert druga.mapped                     # BuyersItemIdentification je nasa sifra
    assert druga.erp_item_code == "10045"

    assert len(draft.unmapped_lines) == 1
    assert not draft.ready


def test_mapiranje_po_barkodu(db):
    with db.session_scope() as session:
        doc_id = _document(session)
        session.add(
            ItemMapping(supplier_vat="100200300", barcode="8600123456789", erp_item_code="10012")
        )
        session.flush()
        draft = build_draft(session, doc_id)

    assert draft.lines[0].erp_item_code == "10012"
    assert draft.ready


def test_knjizno_odobrenje_ima_negativne_kolicine(db):
    with db.session_scope() as session:
        doc_id = _document(session, doc_type=DocumentType.CREDIT_NOTE)
        draft = build_draft(session, doc_id)

    assert draft.lines[0].quantity == -10
    assert draft.lines[0].net_amount == -9000.0
    assert draft.lines[0].net_unit_price == 900.0  # cena ostaje pozitivna


def test_csv_izvoz(db, tmp_path):
    with db.session_scope() as session:
        doc_id = _document(session)
        draft = build_draft(session, doc_id)

    path = CsvWriter(tmp_path).write(draft)
    content = open(path, encoding="utf-8-sig").read()
    assert "Kabl PPY 3x1.5" in content
    assert "magacin;02" in content


def test_app_ne_sme_da_upise_zaknjizenu_kalkulaciju():
    """Knjizenje ide iskljucivo kroz ERP - ono povlaci i glavnu knjigu.

    Kalkulacija upisana kao vec zaknjizena izgleda gotovo, a nista se nije
    desilo: roba nije zaduzena, glavna knjiga ne zna za nju, poslovodja nema
    sta da klikne.
    """
    import pytest

    from sefsync.erp.kalkulacija import (PROKNJIZENO_JESTE, PROKNJIZENO_NIJE,
                                         PROKNJIZENO_ZAKLJUCANO, Nacrt, upisi)

    nacrt = Nacrt(
        document_id=1, tabela_zaglavlja="MKALKUL", tabela_stavki="MPRULK",
        magacin="005", zaglavlje={"PROKNJIZENO": PROKNJIZENO_JESTE},
        stavke=[{"ARTIKAL": "1"}],
    )
    with pytest.raises(ValueError, match="zaknjizenu"):
        upisi(nacrt)

    nacrt.zaglavlje["PROKNJIZENO"] = PROKNJIZENO_ZAKLJUCANO
    with pytest.raises(ValueError, match="zaknjizenu"):
        upisi(nacrt)

    assert PROKNJIZENO_NIJE == 0


def test_nacrt_podrazumevano_nije_zaknjizen():
    from sefsync.erp import kalkulacija

    izvor = (kalkulacija.__file__)
    with open(izvor, encoding="utf-8") as f:
        kod = f.read()
    # vrednost se ne sme zakucavati brojem na mestu gradjenja zaglavlja
    assert '"PROKNJIZENO": PROKNJIZENO_NIJE' in kod
    assert '"PROKNJIZENO": 1' not in kod


def test_avansni_racun_ne_ide_kroz_kalkulaciju(db):
    """Mereno: nijedan od 39 avansa u istoriji nema kalkulaciju u ERP-u.

    Da je app napravi, nastao bi dokument koji u knjigama ne postoji.
    """
    from sefsync.erp.kalkulacija import pripremi
    from sefsync.models import BusinessUnit, Document, DocumentType, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777001, document_number="AV-1/26", supplier_name="X",
            supplier_vat="100000000", amount=1000.0, sef_status=SefStatus.SEEN,
            document_type=DocumentType.PREPAYMENT, business_unit_id=pj.id,
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id

    nacrt = pripremi(doc_id, korisnik="test", engine=None)

    assert nacrt.spreman is False
    assert "Avansni" in nacrt.problemi[0]


def test_avans_dobija_stanje_ne_treba(db, monkeypatch):
    """Na ekranu ne sme da stoji kao da ceka nesto - ne treba mu kalkulacija."""
    from sefsync.models import (
        BusinessUnit, Document, DocumentType, NacrtKalkulacije, SefStatus,
        StanjeNacrta, UnitKind,
    )
    from sefsync.services import nacrti

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777002, document_number="AV-2/26", supplier_name="X",
            supplier_vat="100000000", amount=1000.0, sef_status=SefStatus.SEEN,
            document_type=DocumentType.PREPAYMENT, business_unit_id=pj.id,
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id

    def ne_sme_da_se_zove(*a, **k):
        raise AssertionError("za avans se ERP ne dira")

    monkeypatch.setattr(nacrti, "pripremi", ne_sme_da_se_zove)
    red = nacrti.osvezi(doc_id)

    assert red.stanje is StanjeNacrta.NE_TREBA
    with db.session_scope() as session:
        assert session.get(NacrtKalkulacije, doc_id).stanje is StanjeNacrta.NE_TREBA


def test_rbr_nosi_ime_aplikacije_a_ne_coveka():
    """RBR je jedini trag u ERP-u ko je uneo stavku.

    Mereno 21.09.: nijedna od 23.079 stavki nema prazan RBR, ni na nezaknjizenim
    kalkulacijama - znaci upisuje se pri unosu stavke, ne klikom na KNJIZENJE.
    Zato ga app mora upisati sama, i to pod svojim imenom: `RBR LIKE 'sefsync%'`
    odmah kaze sta je app napravila ako nesto krene naopako.
    """
    import datetime as dt

    from sefsync.erp.kalkulacija import KORISNIK, napravi_rbr

    rbr = napravi_rbr(KORISNIK, dt.datetime(2026, 9, 18, 14, 45, 3))

    assert rbr.startswith("82444")          # clarion dan 18.09.2026
    assert rbr.endswith("sefsync")
    assert len(rbr) <= 39                   # duzina kolone u PRULKALK/MPRULK


def test_kalkulacija_nosi_datum_prometa_a_ne_danasnji(db, monkeypatch):
    """Mereno na 3.401 dokumentu gde se datum fakture i prometa razlikuju:
    DATUM je promet u 90 %, DAT_RAC_DOB u 91 %. App je ranije pisala danasnji
    datum, pa je na ERP ekranu ispadao datum slanja umesto prometa.
    """
    import datetime as dt

    from sefsync.erp import kalkulacija as K
    from sefsync.models import BusinessUnit, Document, DocumentLine, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777010, document_number="RN-1/26", supplier_name="X",
            supplier_vat="100000000", supplier_reg_no="12345678",
            amount=1200.0, vat_amount=200.0, sef_status=SefStatus.SEEN,
            business_unit_id=pj.id,
            issue_date=dt.date(2026, 9, 18),
            delivery_date=dt.date(2026, 9, 15),      # promet je ranije od fakture
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id

    class LazniConn:
        def execute(self, *a, **k):
            return self

        def first(self):
            return None

        def scalar(self):
            return None

        def __iter__(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class LazniEngine:
        def connect(self):
            return LazniConn()

    nacrt = K.pripremi(doc_id, engine=LazniEngine(), datum=dt.date(2026, 9, 22))

    promet = K.clarion_dan(dt.date(2026, 9, 15))
    assert nacrt.zaglavlje["DATUM"] == promet
    assert nacrt.zaglavlje["DAT_RAC_DOB"] == promet
    assert nacrt.zaglavlje["DATUM_PRIJEM"] == promet


def test_faktura_sa_vise_poreskih_stopa_deli_pdv(db):
    """ERP drzi stope odvojeno: PDV je opsta (20 %), PRENETIPOREZ posebna (10 %).

    Mereno na 071/00033: PRENETIPOREZ 207,18 je tacno 10 % od osnovice po toj
    stopi. Da app sve sabere u PDV, poreska evidencija bi bila pogresna.
    """
    import datetime as dt

    from sefsync.erp import kalkulacija as K
    from sefsync.models import BusinessUnit, Document, DocumentLine, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777020, document_number="KP-1/26", supplier_name="Komunalno",
            supplier_vat="100495492", supplier_reg_no="12345678",
            amount=5000.0, vat_amount=500.0, sef_status=SefStatus.SEEN,
            business_unit_id=pj.id, issue_date=dt.date(2026, 9, 18),
        )
        # usluzna faktura sa obe stope - struja 20 %, voda 10 %
        doc.lines.append(DocumentLine(line_no=1, name="Električna energija",
                                      quantity=1, line_amount=1000.0, vat_percent=20.0))
        doc.lines.append(DocumentLine(line_no=2, name="Voda",
                                      quantity=1, line_amount=500.0, vat_percent=10.0))
        session.add(doc)
        session.flush()
        doc_id = doc.id

    class Conn:
        def execute(self, *a, **k):
            return self

        def first(self):
            return None

        def scalar(self):
            return None

        def __iter__(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    nacrt = K.pripremi(doc_id, engine=type("E", (), {"connect": lambda self: Conn()})())

    assert nacrt.zaglavlje["PDV"] == 200.0            # 20 % od 1.000
    assert nacrt.zaglavlje["PRENETIPOREZ"] == 50.0    # 10 % od 500


def test_racun_za_struju_deli_evidenciju_na_tip_1_i_23(db):
    """Deo racuna bez PDV-a ide u posebnu evidenciju (PDVEVIDX), a ne u zbir.

    Po pravom EPS racunu KP101016682899: stavka "JMS taksa" 349,00 nosi
    kategoriju OE (nije predmet PDV-a), a osnovica za struju 39.134,26 nosi
    20 %. ERP je to na 088/03039 upisao kao dva reda - TIP 1 sa 46.961,11 i
    TIP 23 sa 349,00. Do sada je app pisala jedan iznos, pa je poreska
    evidencija ispadala veca nego sto jeste.
    """
    import datetime as dt

    from sefsync.erp import kalkulacija as K
    from sefsync.models import BusinessUnit, Document, DocumentLine, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777021, document_number="KP101016682899",
            supplier_name="EPS AD Beograd", supplier_vat="103920327",
            supplier_reg_no="20053658", amount=47310.11, vat_amount=7826.85,
            sef_status=SefStatus.SEEN, business_unit_id=pj.id,
            issue_date=dt.date(2026, 9, 17), delivery_date=dt.date(2026, 9, 1),
        )
        doc.lines.append(DocumentLine(line_no=1, name="JMS taksa", quantity=1,
                                      line_amount=349.0, vat_percent=0.0,
                                      vat_category="OE"))
        doc.lines.append(DocumentLine(line_no=2, name="Osnovica za elektricnu energiju",
                                      quantity=1, line_amount=39134.26,
                                      vat_percent=20.0, vat_category="S"))
        session.add(doc)
        session.flush()
        doc_id = doc.id

    class Conn:
        def execute(self, *a, **k):
            return self

        def first(self):
            return None

        def scalar(self):
            return None

        def __iter__(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    nacrt = K.pripremi(doc_id, engine=type("E", (), {"connect": lambda self: Conn()})())

    oporezivi, neoporezivi = nacrt.pdv_evidencija
    assert oporezivi["TIP"] == K.TIP_ULAZNI_RACUN
    assert oporezivi["IZNOS"] == 46961.11        # 39.134,26 + 7.826,85
    assert oporezivi["OSNOVA2"] == 39134.26
    assert oporezivi["PDV2"] == 7826.85
    assert neoporezivi["TIP"] == K.TIP_BEZ_EPPDV
    assert neoporezivi["IZNOS"] == 349.0
    assert neoporezivi["PDV2"] == 0.0
    # zbir oba reda mora da da ukupan iznos racuna
    assert round(oporezivi["IZNOS"] + neoporezivi["IZNOS"], 2) == 47310.11
    # zaglavlje se ne menja - PDV ostaje samo stvarni porez
    assert nacrt.zaglavlje["PDV"] == 7826.85
    assert nacrt.zaglavlje["IZNOS_ZA_NAP"] == 47310.11


def test_racun_bez_neoporezivog_dela_nema_posebnu_evidenciju(db):
    """Racun koji je ceo oporeziv nema red u PDVEVIDX - ERP ga izvede sam.

    Mereno: 2.988 od 3.075 racuna na 088 nema nijedan red u toj tabeli.
    """
    import datetime as dt

    from sefsync.erp import kalkulacija as K
    from sefsync.models import BusinessUnit, Document, DocumentLine, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777022, document_number="KP-2/26", supplier_name="Telekom",
            supplier_vat="100002887", supplier_reg_no="17162543",
            amount=1200.0, vat_amount=200.0, sef_status=SefStatus.SEEN,
            business_unit_id=pj.id, issue_date=dt.date(2026, 9, 18),
        )
        doc.lines.append(DocumentLine(line_no=1, name="Telefon", quantity=1,
                                      line_amount=1000.0, vat_percent=20.0,
                                      vat_category="S"))
        session.add(doc)
        session.flush()
        doc_id = doc.id

    class Conn:
        def execute(self, *a, **k):
            return self

        def first(self):
            return None

        def scalar(self):
            return None

        def __iter__(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    nacrt = K.pripremi(doc_id, engine=type("E", (), {"connect": lambda self: Conn()})())
    assert nacrt.pdv_evidencija == []
