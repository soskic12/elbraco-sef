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
    # Dokument je usluzni (OFFICE -> 088), a tamo prijema robe nema:
    # vidi test_usluga_nema_datum_prijema_robe.
    assert nacrt.zaglavlje["DATUM_PRIJEM"] == 0


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


def test_usluga_nema_datum_prijema_robe(db):
    """Uslugu niko ne primi, pa DATUM_PRIJEM ostaje nula.

    Mereno: na svih 3.076 zaglavlja na 088 polje je nula, dok je na 010, 021 i
    u maloprodaji popunjeno bez izuzetka. Prvi probni upis (088/02530) ga je
    popunio i time se razlikovao od zapisa koji ERP sam napravi.
    """
    import datetime as dt

    from sefsync.erp import kalkulacija as K
    from sefsync.models import BusinessUnit, Document, DocumentLine, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777023, document_number="KP-3/26", supplier_name="EPS",
            supplier_vat="103920327", supplier_reg_no="20053658",
            amount=1200.0, vat_amount=200.0, sef_status=SefStatus.SEEN,
            business_unit_id=pj.id, issue_date=dt.date(2026, 9, 18),
            delivery_date=dt.date(2026, 9, 1),
        )
        doc.lines.append(DocumentLine(line_no=1, name="Struja", quantity=1,
                                      line_amount=1000.0, vat_percent=20.0))
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
    assert nacrt.zaglavlje["DATUM_PRIJEM"] == 0
    assert nacrt.zaglavlje["MENJA_CENU"] == ""
    # datum prometa i dalje stoji tamo gde treba
    assert nacrt.zaglavlje["DATUM"] == K.clarion_dan(dt.date(2026, 9, 1))

    # a kod robe se prijem upisuje - inace bi polje ostalo prazno tamo gde ERP
    # ocekuje datum (010 i 021: popunjeno na svih 872 zaglavlja)
    with db.session_scope() as session:
        mag = BusinessUnit(code="MAG010", name="POVRAĆAJ", kind=UnitKind.WAREHOUSE,
                           erp_code="010")
        session.add(mag)
        session.flush()
        doc = Document(
            sef_invoice_id=777024, document_number="RN-9/26", supplier_name="Dobavljač",
            supplier_vat="100000000", supplier_reg_no="12345678",
            amount=1200.0, vat_amount=200.0, sef_status=SefStatus.SEEN,
            business_unit_id=mag.id, issue_date=dt.date(2026, 9, 18),
            delivery_date=dt.date(2026, 9, 1),
        )
        doc.lines.append(DocumentLine(line_no=1, name="Roba", quantity=1,
                                      line_amount=1000.0, vat_percent=20.0))
        session.add(doc)
        session.flush()
        doc_roba = doc.id

    nacrt = K.pripremi(doc_roba, engine=type("E", (), {"connect": lambda self: Conn()})())
    assert nacrt.zaglavlje["DATUM_PRIJEM"] == K.clarion_dan(dt.date(2026, 9, 1))
    # Isto pravilo vazi i za MENJA_CENU: kalkulacija robe formira cenu, usluga ne.
    assert nacrt.zaglavlje["MENJA_CENU"] == "Da"


def test_dopuna_praznih_kolona():
    """Kolone koje app ne racuna dobijaju nulu ili prazan string, ne NULL.

    ERP je Clarion aplikacija i nigde ne ostavlja NULL: u uzorku od 200 zapisa
    na 021 svih 78 kolona koje app ne dira ima 0 odnosno prazan string.
    """
    from sefsync.erp import kalkulacija as K

    class Conn:
        def execute(self, *a, **k):
            return [
                type("R", (), {"COLUMN_NAME": "MAGACIN", "DATA_TYPE": "varchar"}),
                type("R", (), {"COLUMN_NAME": "IZNOS", "DATA_TYPE": "float"}),
                type("R", (), {"COLUMN_NAME": "CARINA", "DATA_TYPE": "float"}),
                type("R", (), {"COLUMN_NAME": "VAL", "DATA_TYPE": "varchar"}),
                type("R", (), {"COLUMN_NAME": "STATUSID", "DATA_TYPE": "int"}),
                type("R", (), {"COLUMN_NAME": "NESTO", "DATA_TYPE": "datetime"}),
            ]

    K._KOLONE.pop("PROBA", None)
    red = K.dopuni_prazna(Conn(), "PROBA", {"MAGACIN": "021", "IZNOS": 1200.0})

    assert red["MAGACIN"] == "021"      # nase vrednosti ostaju
    assert red["IZNOS"] == 1200.0
    assert red["CARINA"] == 0           # brojevi dobijaju nulu
    assert red["STATUSID"] == 0
    assert red["VAL"] == ""             # tekst prazan string
    assert "NESTO" not in red           # ostale tipove ne diramo
    K._KOLONE.pop("PROBA", None)


def test_knjizno_odobrenje_nosi_tip_povrat_robe(db):
    """Stavka knjiznog odobrenja ide u evidenciju kao povrat robe (TIPEVID 35).

    Mereno: na 010 je IDPDV = 35 na 690 od 692 stavki i svih 690 su negativne,
    dok je na 021 i u maloprodaji 1 bez izuzetka.
    """
    import datetime as dt

    from sefsync.erp import kalkulacija as K
    from sefsync.models import (
        BusinessUnit, Document, DocumentLine, DocumentType, ItemMapping, SefStatus,
        UnitKind,
    )

    with db.session_scope() as session:
        mag = BusinessUnit(code="MAG010", name="POVRAĆAJ", kind=UnitKind.WAREHOUSE,
                           erp_code="010")
        session.add(mag)
        session.flush()
        doc = Document(
            sef_invoice_id=777025, document_number="KO-1/26", supplier_name="Dobavljač",
            supplier_vat="100000000", supplier_reg_no="12345678",
            document_type=DocumentType.CREDIT_NOTE,
            amount=1200.0, vat_amount=200.0, sef_status=SefStatus.SEEN,
            business_unit_id=mag.id, issue_date=dt.date(2026, 9, 18),
        )
        doc.lines.append(DocumentLine(line_no=1, name="Vraćena roba", quantity=1,
                                      line_amount=1000.0, vat_percent=20.0,
                                      sellers_item_id="DOB-77"))
        session.add(doc)
        session.add(ItemMapping(supplier_vat="100000000", supplier_item_id="DOB-77",
                                erp_item_code="12345", active=True))
        session.flush()
        doc_id = doc.id

    class Red:
        """Red iz ARTIKLI onakav kakav ocekuje `_artikal`."""

        _mapping = {"artikal": "12345", "naziv": "Vraćena roba", "jm": "kom",
                    "tarifa": "002"}

        def __getitem__(self, i):
            return 20.0          # PTARIFE.STOPA

    class Conn:
        def execute(self, *a, **k):
            self.upit = str(a[0]) if a else ""
            return self

        def first(self):
            return Red() if "ARTIKLI" in self.upit or "PTARIFE" in self.upit else None

        def scalar(self):
            return None

        def __iter__(self):
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    nacrt = K.pripremi(doc_id, engine=type("E", (), {"connect": lambda self: Conn()})())
    assert nacrt.stavke, "nacrt nema nijednu stavku - test ne bi proveravao nista"
    for stavka in nacrt.stavke:
        assert stavka["IDPDV"] == K.TIP_POVRAT_ROBE
        assert stavka["KOLICINA"] < 0      # odobrenje ulazi minusom


def test_broj_racuna_iz_napomene():
    """Iz napomene knjižnog odobrenja se vadi broj fakture koju ispravlja.

    Pravi primer (Eurotehna, 615FN-2026): „Odobreno za ves masinu WPNA14A3PWIFI,
    po racunu broj 851 VP-2026 od 16.03.2026.“ Broj je u ERP-u zapisan spojeno
    („851VP-2026“), pa se razmaci uklanjaju.
    """
    from sefsync.services.odobrenja import broj_originalnog_racuna as broj

    assert broj("Odobreno za ves masinu WPNA14A3PWIFI, po racunu broj 851 VP-2026 "
                "od 16.03.2026.") == "851VP-2026"
    assert broj("Odobrenje po računu br. 1234/26 od 01.01.2026") == "1234/26"
    assert broj("po fakturi 77VP-2026, reklamacija") == "77VP-2026"
    assert broj("Nema broja fakture ovde") is None
    assert broj(None) is None


def test_izbor_stavke_sa_originalne_fakture():
    """Kad faktura ima više redova, iznos odobrenja bira pravi.

    Mereno: to je samo pretvorilo 49 slučajeva iz „više stavki“ u razrešeno, a
    predlog se poklopio sa čovekovim izborom u 50 od 52 provere.
    """
    from sefsync.services.odobrenja import izaberi_stavku

    veš = {"artikal": "84678", "naziv": "Gorenje WPNA 14 A3PWIFI", "nabavna": 43470.70}
    frižider = {"artikal": "92815", "naziv": "Gorenje RB 413 EPW4", "nabavna": 19610.0}

    assert izaberi_stavku([veš], None)[0] is veš            # jedina stavka, iznos nepoznat
    assert izaberi_stavku([veš], 43470.70)[0] is veš       # jedina stavka, iznos se slaže

    # Jedina stavka ali iznos ne odgovara: 568FN-2026 je odobrenje na 25.066,44
    # za uređaj čija je nabavna 18.108,40 - to nije taj uređaj.
    stavka, kako = izaberi_stavku([veš], 20000.0)
    assert stavka is None and "popust" in kako          # odobren deo vrednosti
    stavka, kako = izaberi_stavku([frižider], 25066.44)
    assert stavka is None and "vise od nabavne" in kako
    assert izaberi_stavku([veš, frižider], 43470.70)[0] is veš
    assert izaberi_stavku([veš, frižider], 19610.0)[0] is frižider

    # ništa ne odgovara iznosu - bolje bez predloga nego pogrešan
    stavka, kako = izaberi_stavku([veš, frižider], 999.0)
    assert stavka is None and "iznosu" in kako

    # dva različita artikla sa istim iznosom se ne razrešavaju sama
    isti = dict(frižider, artikal="99999", naziv="Drugi uređaj")
    stavka, kako = izaberi_stavku([frižider, isti], 19610.0)
    assert stavka is None

    # isti artikal u dva reda jeste jedan odgovor
    stavka, _ = izaberi_stavku([frižider, dict(frižider)], 19610.0)
    assert stavka is not None and stavka["artikal"] == "92815"

    assert izaberi_stavku([], 100.0)[0] is None


def test_popust_se_prepoznaje_po_tome_sto_je_manji_od_nabavne(db):
    """Odobren deo vrednosti je popust; ceo iznos je povrat i ne beleži se ovde.

    Mereno na odobrenjima čija napomena imenuje fakturu: 61 je na ceo iznos
    uređaja, 5 na deo. Bane: deo znači da je uređaj stigao oštećen, roba ostaje
    kod nas i prodaje se jeftinije.
    """
    from sefsync.services.odobrenja import popust_za_uredjaj

    class Red:
        def __init__(self, nab):
            self._mapping = {
                "magacin": "008", "broj": "00153", "artikal": "93568",
                "naziv": "Beko RS 9152WN", "kolicina": 1.0, "nabavna": nab,
            }

    class Conn:
        def __init__(self, nab):
            self.nab = nab

        def execute(self, upit, *a, **k):
            tekst = str(upit)
            if "sys.databases" in tekst:
                return [("ELBS_2026",)]
            if "ARTIKLI" in tekst:
                return self          # sifra postoji danas
            if "KALKUL" in tekst:
                return [Red(self.nab)]
            return self

        def first(self):
            return (1,)

        def __iter__(self):
            return iter(())

    import sefsync.services.odobrenja as O
    O._BAZE = None

    napomena = "Odobreno za frizider, po racunu broj 851 VP-2026 od 16.03.2026."

    # odobreno 4.671,57 od 12.630,98 - popust zbog oštećenja
    nalaz = popust_za_uredjaj(Conn(12630.98), napomena=napomena,
                              dobavljac="00142", osnovica=4671.57)
    assert nalaz is not None
    assert nalaz["sifra"] == "93568"
    assert nalaz["nabavna"] == 12630.98
    O._BAZE = None

    # odobren ceo iznos - to je povrat, ne popust
    assert popust_za_uredjaj(Conn(12630.98), napomena=napomena,
                             dobavljac="00142", osnovica=12630.98) is None
    O._BAZE = None

    # bez napomene nema šta da se traži
    assert popust_za_uredjaj(Conn(12630.98), napomena=None,
                             dobavljac="00142", osnovica=100.0) is None
    O._BAZE = None


def test_udeo_popusta():
    """Koliki deo vrednosti uređaja je odobren - stoji uz zapis da se vidi."""
    from sefsync.models import PopustZaPrenos

    p = PopustZaPrenos(document_id=1, iznos=4671.57, nabavna=12630.98)
    assert 0.36 < p.udeo < 0.38
    assert p.otvoren
    assert PopustZaPrenos(document_id=2, iznos=100.0, nabavna=None).udeo is None


def test_prepoznaje_kalkulaciju_koju_je_covek_napravio_rucno(monkeypatch):
    """Ako je poslovođa kalkulaciju napravio rukom, panel to mora da vidi.

    Prelazak ide odjednom za sve, pa će se to dešavati. Upis bi duplikat odbio,
    ali tek kad neko klikne — do tada bi panel tražio prijem robe za fakturu koja
    je već zaknjižena.
    """
    from sefsync.erp.kalkulacija import Nacrt
    from sefsync.services import nacrti as N

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Engine:
        def connect(self):
            return Conn()

    nacrt = Nacrt(
        document_id=1, tabela_zaglavlja="ULKALKUL", tabela_stavki="PRULKALK",
        magacin="021",
        zaglavlje={"BROJ_RN_DOB": "FA-5543-03/26", "DOBAVLJAC": "00905"},
    )

    monkeypatch.setattr(
        "sefsync.erp.kalkulacija.postojeca_kalkulacija",
        lambda conn, broj, dob: "ULKALKUL 021/00286" if broj and dob else None,
    )
    assert N._vec_u_erpu(nacrt, engine=Engine()) == "ULKALKUL 021/00286"

    # bez broja računa ili dobavljača nema po čemu da se traži
    prazan = Nacrt(document_id=2, tabela_zaglavlja="ULKALKUL", tabela_stavki="PRULKALK",
                   magacin="021", zaglavlje={})
    assert N._vec_u_erpu(prazan, engine=Engine()) is None

    # ERP ume da bude nedostupan - to ne sme da obori nacrt
    def puca(*a, **k):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("veza", None, Exception("nema ERP-a"))

    monkeypatch.setattr("sefsync.erp.kalkulacija.postojeca_kalkulacija", puca)
    assert N._vec_u_erpu(nacrt, engine=Engine()) is None


def test_vrsta_ekrana_po_jedinici():
    """Ilijana i Bogdan dobijaju svoje ekrane, objekat svoj.

    Bane, 24.09.: „ilijana treba da ima svoj a ne operatorski ekran; 088 je u
    stvari Bogdanovo, i po meni treba da ima prilagođen ekran".
    """
    from sefsync.models import BusinessUnit, UnitKind
    from sefsync.web.app import (
        EKRAN_OBJEKAT, EKRAN_POVRACAJ, EKRAN_USLUGE, _vrsta_ekrana,
    )

    povracaj = BusinessUnit(code="MAG010", name="Povrat robe",
                            kind=UnitKind.WAREHOUSE, erp_code="010")
    usluge = BusinessUnit(code="MAG088", name="BEZ EVIDENCIJE",
                          kind=UnitKind.WAREHOUSE, erp_code="088")
    objekat = BusinessUnit(code="MP002", name="APATIN",
                           kind=UnitKind.RETAIL, erp_code="002")

    assert _vrsta_ekrana(povracaj) == EKRAN_POVRACAJ
    assert _vrsta_ekrana(usluge) == EKRAN_USLUGE
    assert _vrsta_ekrana(objekat) == EKRAN_OBJEKAT

    # OFFICE je mesto gde uslužne fakture stvarno završe (1.953 dokumenta),
    # dok MAG088 nema nijedan - 088 se dobija tek pri pravljenju kalkulacije.
    uprava = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
    assert _vrsta_ekrana(uprava) == EKRAN_USLUGE


def test_valuta_nikad_nije_nula(db):
    """Kad faktura nema rok plaćanja, u VALUTA ide datum prometa.

    Mereno: od 8.863 kalkulacija u ERP-u VALUTA je nula — nijednom. Knjižno
    odobrenje rok po pravilu nema, pa je app pisala nulu; na 010 je ERP-ova
    VALUTA jednaka datumu u 584 od 587 zapisa.
    """
    import datetime as dt

    from sefsync.erp import kalkulacija as K
    from sefsync.models import BusinessUnit, Document, DocumentLine, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        session.add(pj)
        session.flush()
        doc = Document(
            sef_invoice_id=777030, document_number="KO-9/26", supplier_name="Dobavljač",
            supplier_vat="100000000", supplier_reg_no="12345678",
            amount=1200.0, vat_amount=200.0, sef_status=SefStatus.SEEN,
            business_unit_id=pj.id, issue_date=dt.date(2026, 9, 20),
            delivery_date=dt.date(2026, 9, 18), due_date=None,
        )
        doc.lines.append(DocumentLine(line_no=1, name="Usluga", quantity=1,
                                      line_amount=1000.0, vat_percent=20.0))
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
    promet = K.clarion_dan(dt.date(2026, 9, 18))
    assert nacrt.zaglavlje["VALUTA"] == promet
    assert nacrt.zaglavlje["VALUTA"] != 0


def test_sifra_objekta_088_vodi_na_upravu(db):
    """Ko u šifarniku osoblja ima objekat 088, radi ulazne račune — a oni stoje
    na upravi.

    Mereno 25.09.: OFFICE ima 1.953 dokumenta, MAG088 nijedan. Magacin 088
    nastaje tek pri pravljenju kalkulacije, pa bi Bogdanov ekran bio prazan.
    """
    from sefsync.models import BusinessUnit, UnitKind
    from sefsync.web.app import _unit_for
    from sefsync.web.auth import Role, User

    with db.session_scope() as session:
        session.add_all([
            BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ),
            BusinessUnit(code="MAG088", name="BEZ EVIDENCIJE", kind=UnitKind.WAREHOUSE,
                         erp_code="088"),
            BusinessUnit(code="MP004", name="KULA", kind=UnitKind.RETAIL, erp_code="004"),
        ])
        session.flush()

        bogdan = User("bogdan", "Sunajko Bogdan", Role.POSLOVODJA, "088", "FINANSIJE")
        assert _unit_for(session, bogdan).code == "OFFICE"

        # ostali i dalje idu na svoj objekat
        nina = User("ninar", "Vignević Nina", Role.POSLOVODJA, "004", "PODRSKA")
        assert _unit_for(session, nina).code == "MP004"


def test_dokument_pre_prelaska_ide_po_starom(monkeypatch):
    """Dokument koji je stigao pre dana prelaska ne nudi upis ni prijem.

    Bane, 25.09.: „nek napravi ručno ali mora dobiti jasno uputstvo od kog
    momenta je krenulo ovo novo". Na dan prelaska je u panelu stajalo 46
    spremnih kalkulacija za fakture koje niko nije zaknjižio — jedan pogrešan
    klik i ERP bi dobio kalkulaciju za robu koju je poslovođa odavno primio.
    """
    import datetime as dt

    from sefsync.config import Settings
    from sefsync.models import Document

    stari = Document(sef_invoice_id=1, created_at=dt.datetime(2026, 9, 20, 10, 0))
    novi = Document(sef_invoice_id=2, created_at=dt.datetime(2026, 10, 1, 8, 0))
    na_sam_dan = Document(sef_invoice_id=3, created_at=dt.datetime(2026, 10, 1, 0, 5))

    # bez zadatog dana nista nije po starom - zatecena instalacija radi kao i pre
    monkeypatch.setattr("sefsync.config.get_settings", lambda: Settings(_env_file=None))
    assert stari.po_starom is False

    monkeypatch.setattr(
        "sefsync.config.get_settings",
        lambda: Settings(_env_file=None, novi_tok_od="2026-10-01"),
    )
    assert stari.po_starom is True
    assert novi.po_starom is False
    assert na_sam_dan.po_starom is False      # dan prelaska je vec novi tok


# --------------------------------------------------------------------------- #
# provera duplikata: odsecen broj i dobavljac otvoren dvaput
# --------------------------------------------------------------------------- #


class _LaznaVeza:
    """Minimalna zamena za Connection - vraca unapred pripremljene redove."""

    def __init__(self, komitent=None, kalkulacije=(), grupa=()):
        self.komitent = komitent          # (MATBROJ, ZIRORACUN) trazene sifre
        self.kalkulacije = list(kalkulacije)  # (tabela, mag, broj, dobavljac, BROJ_RN_DOB)
        self.grupa = list(grupa)          # analitike istog pravnog lica
        self.pitanja = []

    def execute(self, upit, parametri=None):
        from types import SimpleNamespace

        tekst = str(upit)
        self.pitanja.append(tekst)
        if "MATBROJ,'')) mb" in tekst:
            if self.komitent is None:
                return iter([])
            mb, pib = self.komitent
            return _Prvi(SimpleNamespace(mb=mb, pib=pib))
        if "SELECT RTRIM(ANALITIKA) a FROM dbo.NAZIVI" in tekst:
            return iter([SimpleNamespace(a=s) for s in self.grupa])
        tabela = "ULKALKUL" if "dbo.ULKALKUL" in tekst else "MKALKUL"
        trazene = set((parametri or {}).values())
        return iter([
            SimpleNamespace(br=broj, mag=mag, dob=dob, rn=rn)
            for tab, mag, broj, dob, rn in self.kalkulacije
            if tab == tabela and dob in trazene
        ])


class _Prvi(list):
    def __init__(self, red):
        super().__init__([red])

    def first(self):
        return self[0]


def test_postojeca_kalkulacija_hvata_odsecen_broj():
    """ERP-ova forma pusta 15 znakova, pa duzi broj stoji odsecen."""
    from sefsync.erp.kalkulacija import postojeca_kalkulacija

    veza = _LaznaVeza(
        komitent=("08734054", "100273149"),
        grupa=["10107"],
        kalkulacije=[("ULKALKUL", "088", "00449", "10107", "k110/2026-83524")],
    )
    nadjeno = postojeca_kalkulacija(veza, "K110/2026-835249", "10107")
    assert nadjeno is not None
    assert "088/00449" in nadjeno and "odsečen" in nadjeno

    # kad ih je vise, ne zna se koji je - cutanje je ispravnije od pogotka,
    # jer bi lazan pogodak sklonio fakturu sa ekrana
    veza.kalkulacije.append(("MKALKUL", "010", "00131", "10107", "k110/2026-83524"))
    assert postojeca_kalkulacija(veza, "K110/2026-835249", "10107") is None


def test_postojeca_kalkulacija_ne_gata_po_kratkom_pocetku():
    """Broj koji nije na granici od 15 znakova nije odsecen, pa se ne poredi po pocetku."""
    from sefsync.erp.kalkulacija import postojeca_kalkulacija

    # 7016088538 je stvaran broj u ERP-u i pocetak je od 70160885380120 -
    # to su dve razlicite fakture istog dobavljaca
    veza = _LaznaVeza(
        komitent=("", ""),
        kalkulacije=[("ULKALKUL", "088", "00200", "12469", "7016088538")],
    )
    assert postojeca_kalkulacija(veza, "7016088538_0120", "12469") is None


def test_postojeca_kalkulacija_gleda_i_sestrinsku_sifru():
    """Sulex stoji dvaput u komitentima - kalkulacija je pod drugom analitikom."""
    from sefsync.erp.kalkulacija import postojeca_kalkulacija

    veza = _LaznaVeza(
        komitent=("08734054", "100273149"),
        grupa=["15582", "04808"],
        kalkulacije=[("ULKALKUL", "088", "00512", "04808", "FA-5543-03/26")],
    )
    nadjeno = postojeca_kalkulacija(veza, "FA55430326", "15582")
    assert nadjeno is not None
    assert "088/00512" in nadjeno and "04808" in nadjeno


def test_sestrinske_sifre_preskace_popunu():
    """MATBROJ '123456789' i same nule ne identifikuju nikoga."""
    from sefsync.erp.kalkulacija import sestrinske_sifre

    veza = _LaznaVeza(komitent=("123456789", "000000000"), grupa=["99999"])
    assert sestrinske_sifre(veza, "10107") == {"10107"}
    assert not any("SELECT RTRIM(ANALITIKA) a FROM dbo.NAZIVI" in p for p in veza.pitanja)
