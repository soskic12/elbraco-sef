"""Petlja ucenja: operater potvrdjuje ili ispravlja predlog, aplikacija pamti.

Bez ovoga se ne razlikuje "razvrstano tacno" od "niko jos nije pogledao", pa
se ni tacnost ne moze meriti ni losa pravila prepoznati.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sefsync.models import (
    BusinessUnit,
    Document,
    MatchField,
    MatchOp,
    RoutingRule,
    RoutingSource,
    SefStatus,
    UnitKind,
)
from sefsync.routing.stats import sazetak
from sefsync.services.workflow import Workflow


class LazniNotifier:
    def notify_document(self, doc_id: int) -> bool:
        return True

    def notify_receipt(self, doc_id: int, confirmed_by: str) -> bool:
        return True


@pytest.fixture
def svet(db):
    with db.session_scope() as session:
        apatin = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        sombor = BusinessUnit(code="MP001", name="SOMBOR", kind=UnitKind.RETAIL, erp_code="001")
        session.add_all([apatin, sombor])
        session.flush()
        pravilo = RoutingRule(priority=20, field=MatchField.DELIVERY_ADDRESS,
                              op=MatchOp.CONTAINS, pattern="Srpskih vladara 46",
                              business_unit_id=apatin.id)
        session.add(pravilo)
        session.flush()
        doc = Document(
            sef_invoice_id=900, document_number="1/26", supplier_name="EWE COMP",
            supplier_vat="100042618", supplier_reg_no="17333127",
            amount=1000.0, sef_status=SefStatus.SEEN,
            business_unit_id=apatin.id, routing_source=RoutingSource.RULE,
            routing_rule_id=pravilo.id,
            delivery_address="Srpskih vladara 46, 25260, Apatin",
        )
        session.add(doc)
        session.flush()
        return {"apatin": apatin.id, "sombor": sombor.id,
                "pravilo": pravilo.id, "doc": doc.id}


def tok() -> Workflow:
    return Workflow(notifier=LazniNotifier())


# --------------------------------------------------------------------------- #
# potvrda
# --------------------------------------------------------------------------- #


def test_potvrda_belezi_ko_je_i_broji_pogodak(db, svet):
    tok().confirm_routing([svet["doc"]], actor="NinaR")

    with db.session_scope() as session:
        doc = session.get(Document, svet["doc"])
        assert doc.routing_confirmed_by == "NinaR"
        assert doc.routing_corrected is False
        assert session.get(RoutingRule, svet["pravilo"]).hits == 1


def test_dvostruka_potvrda_ne_broji_dvaput(db, svet):
    tok().confirm_routing([svet["doc"]], actor="NinaR")
    tok().confirm_routing([svet["doc"]], actor="DusanSelak")

    with db.session_scope() as session:
        assert session.get(RoutingRule, svet["pravilo"]).hits == 1
        assert session.get(Document, svet["doc"]).routing_confirmed_by == "NinaR"


def test_prosledjivanje_vazi_kao_potvrda(db, svet):
    """Niko ne salje dokument objektu za koji misli da nije njegov."""
    tok().forward([svet["doc"]], actor="NinaR")

    with db.session_scope() as session:
        assert session.get(Document, svet["doc"]).routing_confirmed_at is not None
        assert session.get(RoutingRule, svet["pravilo"]).hits == 1


# --------------------------------------------------------------------------- #
# ispravka
# --------------------------------------------------------------------------- #


def test_ispravka_belezi_promasaj_pravilu(db, svet):
    tok().assign_unit(svet["doc"], svet["sombor"], actor="NinaR")

    with db.session_scope() as session:
        doc = session.get(Document, svet["doc"])
        assert doc.business_unit_id == svet["sombor"]
        assert doc.routing_corrected is True
        assert doc.routing_source is RoutingSource.MANUAL
        pravilo = session.get(RoutingRule, svet["pravilo"])
        assert pravilo.misses == 1
        assert pravilo.hits == 0


def test_dodela_nerazvrstanog_nije_promasaj(db, svet):
    """Kad aplikacija nije imala odgovor, nema ni sta da se oceni kao gresка."""
    with db.session_scope() as session:
        doc = session.get(Document, svet["doc"])
        doc.business_unit_id = None
        doc.routing_source = RoutingSource.NONE
        doc.routing_rule_id = None

    tok().assign_unit(svet["doc"], svet["apatin"], actor="NinaR")

    with db.session_scope() as session:
        assert session.get(Document, svet["doc"]).routing_corrected is False
        assert session.get(RoutingRule, svet["pravilo"]).misses == 0


def test_ispravka_na_istu_pj_nije_promasaj(db, svet):
    tok().assign_unit(svet["doc"], svet["apatin"], actor="NinaR")

    with db.session_scope() as session:
        assert session.get(RoutingRule, svet["pravilo"]).misses == 0


# --------------------------------------------------------------------------- #
# ucenje novog pravila
# --------------------------------------------------------------------------- #


def test_pamcenje_bira_najjaci_dostupan_podatak(db, svet):
    """Bez izricitog polja uzima se adresa isporuke, pa nadalje po pouzdanosti."""
    with db.session_scope() as session:
        doc = session.get(Document, svet["doc"])
        doc.business_unit_id = None
        doc.delivery_address = None
        doc.order_reference = "NAR-771"
        doc.supplier_vat = "100042618"

    tok().assign_unit(svet["doc"], svet["sombor"], actor="NinaR", remember=True)

    with db.session_scope() as session:
        novo = session.scalar(
            select(RoutingRule).where(RoutingRule.field == MatchField.ORDER_REFERENCE)
        )
        assert novo.pattern == "NAR-771"       # ne PIB, jer je narudzbenica uza
        assert novo.business_unit_id == svet["sombor"]


def test_dokument_bez_ijednog_traga_ne_dobija_pravilo(db, svet):
    with db.session_scope() as session:
        doc = session.get(Document, svet["doc"])
        doc.business_unit_id = None
        for polje in ("delivery_address", "delivery_name", "order_reference",
                      "buyer_reference", "contract_reference", "supplier_vat"):
            setattr(doc, polje, None)

    ishod = tok().assign_unit(svet["doc"], svet["sombor"], actor="NinaR", remember=True)

    assert "nema nijedan podatak" in ishod.message
    with db.session_scope() as session:
        assert len(list(session.scalars(select(RoutingRule)))) == 1  # samo pocetno


# --------------------------------------------------------------------------- #
# merenje
# --------------------------------------------------------------------------- #


def test_tacnost_meri_samo_ono_sto_je_covek_video(db, svet):
    with db.session_scope() as session:
        apatin, sombor = svet["apatin"], svet["sombor"]
        pravilo = svet["pravilo"]
        session.add_all([
            Document(sef_invoice_id=901, business_unit_id=apatin,
                     routing_source=RoutingSource.RULE, routing_rule_id=pravilo),
            Document(sef_invoice_id=902, business_unit_id=apatin,
                     routing_source=RoutingSource.RULE, routing_rule_id=pravilo),
            Document(sef_invoice_id=903),  # nerazvrstan
        ])
        session.flush()
        drugi = session.scalar(select(Document).where(Document.sef_invoice_id == 901)).id
        treci = session.scalar(select(Document).where(Document.sef_invoice_id == 902)).id

    tok().confirm_routing([svet["doc"], drugi], actor="NinaR")   # dva tacna
    tok().assign_unit(treci, svet["sombor"], actor="NinaR")      # jedan ispravljen

    with db.session_scope() as session:
        podaci = sazetak(session)

    assert podaci["tacnost"].tacno == 2
    assert podaci["tacnost"].ispravljeno == 1
    assert podaci["tacnost"].pregledano == 3
    assert round(podaci["tacnost"].procenat) == 67
    assert podaci["nerazvrstano"] == 1
    assert [r.id for r in podaci["sumnjiva"]] == [svet["pravilo"]]


def test_bez_ijedne_provere_tacnost_nije_nula_nego_nepoznata(db, svet):
    """Prazna mera ne sme da izgleda kao los rezultat."""
    with db.session_scope() as session:
        podaci = sazetak(session)

    assert podaci["tacnost"].pregledano == 0
    assert podaci["tacnost"].procenat is None


def test_faktura_bez_piba_i_maticnog_ne_ide_dalje(db, svet):
    """Bez oba identifikatora dobavljac se ne moze prepoznati - ni u sifarniku
    NAZIVI, ni u mapiranju artikala, ni u poreskoj evidenciji. Takav dokument
    staje kod operatera."""
    with db.session_scope() as session:
        loša = Document(
            sef_invoice_id=901, document_number="X-1", supplier_name="Nepoznat",
            amount=100.0, sef_status=SefStatus.SEEN,
            business_unit_id=svet["apatin"],
        )
        session.add(loša)
        session.flush()
        loš_id = loša.id
        assert loša.bez_identifikacije is True
        # prazan string je isto sto i None
        loša.supplier_vat = "  "
        loša.supplier_reg_no = ""
        assert loša.bez_identifikacije is True

    ishod = tok().forward([loš_id], actor="NinaR")
    assert ishod.done == 0
    assert any("neispravna" in p for p in ishod.problems)

    with db.session_scope() as session:
        assert session.get(Document, loš_id).forwarded_at is None
        # ispravan dokument iz fiksture i dalje prolazi
        assert session.get(Document, svet["doc"]).bez_identifikacije is False


# --------------------------------------------------------------------------- #
# dvosmislena sifra dobavljaca
# --------------------------------------------------------------------------- #


def _dokument_sa_stavkom(db, broj: str, sifra_dob: str, naziv: str) -> int:
    from sefsync.models import BusinessUnit, Document, DocumentLine, SefStatus, UnitKind

    with db.session_scope() as session:
        pj = session.scalar(select(BusinessUnit).where(BusinessUnit.code == "MP002"))
        if pj is None:
            pj = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
            session.add(pj)
            session.flush()
        doc = Document(
            sef_invoice_id=hash(broj) % 10**6, document_number=broj,
            supplier_name="CSM 2017", supplier_vat="110130228",
            amount=1000.0, sef_status=SefStatus.SEEN, business_unit_id=pj.id,
        )
        doc.lines.append(
            DocumentLine(line_no=1, name=naziv, sellers_item_id=sifra_dob,
                         quantity=1.0, price=1000.0)
        )
        session.add(doc)
        session.flush()
        return doc.id


def test_ista_sifra_dobavljaca_za_drugi_artikal_gasi_vezu(db, monkeypatch):
    """CSM salje "ponye 20 litium" bez boje, a kod nas je svaka boja svoja sifra.

    Da se veza prepisala, sledeca faktura bi tiho dobila boju od proslog puta.
    """
    from sefsync.models import ItemMapping
    from sefsync.services import nacrti

    class Nacrt:
        magacin, tabela_zaglavlja, zaglavlje = "002", "MKALKUL", {}
        problemi, nemapirane, napomene = [], [], []
        spreman = True

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: Nacrt())
    prvi = _dokument_sa_stavkom(db, "CSM-1", "PONYE20", "CSM ponye 20 litium")
    drugi = _dokument_sa_stavkom(db, "CSM-2", "PONYE20", "CSM ponye 20 litium")

    nacrti.povezi_sifru(prvi, 1, "50001", korisnik="dusan")     # bela
    nacrti.povezi_sifru(drugi, 1, "50002", korisnik="dusan")    # crvena

    with db.session_scope() as session:
        veze = list(session.scalars(select(ItemMapping)))
        assert len(veze) == 1, "ne pravi se druga veza za istu sifru"
        assert veze[0].active is False, "kolebljiva veza se gasi"
        assert veze[0].source == "kolebljivo"
        # obe stavke ipak imaju svoju sifru
        from sefsync.models import DocumentLine

        sifre = {
            l.document_id: l.erp_item_code
            for l in session.scalars(select(DocumentLine))
        }
        assert sifre[prvi] == "50001" and sifre[drugi] == "50002"


def test_ne_pamti_ostavlja_sifarnik_netaknut(db, monkeypatch):
    """Kad covek unapred zna da je sifra dvosmislena."""
    from sefsync.models import ItemMapping
    from sefsync.services import nacrti

    class Nacrt:
        magacin, tabela_zaglavlja, zaglavlje = "002", "MKALKUL", {}
        problemi, nemapirane, napomene = [], [], []
        spreman = True

    monkeypatch.setattr(nacrti, "pripremi", lambda *a, **k: Nacrt())
    doc = _dokument_sa_stavkom(db, "CSM-3", "PONYE24", "CSM ponye 24 litium")

    nacrti.povezi_sifru(doc, 1, "50003", korisnik="dusan", pamti=False)

    with db.session_scope() as session:
        assert session.scalar(select(ItemMapping)) is None


def _odobrenje_sa_stavkom(db, broj: str, sifra_dob: str, naziv: str) -> int:
    """Knjižno odobrenje istog dobavljača, sa zadatom „šifrom“ i nazivom."""
    from sefsync.models import (BusinessUnit, Document, DocumentLine, DocumentType,
                                SefStatus, UnitKind)

    with db.session_scope() as session:
        pj = session.scalar(select(BusinessUnit).where(BusinessUnit.code == "MAG010"))
        if pj is None:
            pj = BusinessUnit(code="MAG010", name="POVRAĆAJ", kind=UnitKind.WAREHOUSE,
                              erp_code="010")
            session.add(pj)
            session.flush()
        doc = Document(
            sef_invoice_id=abs(hash(broj)) % 10**6, document_number=broj,
            supplier_name="Eurotehna", supplier_vat="100000142",
            document_type=DocumentType.CREDIT_NOTE, amount=-1000.0,
            sef_status=SefStatus.SEEN, business_unit_id=pj.id,
        )
        doc.lines.append(DocumentLine(line_no=1, name=naziv, sellers_item_id=sifra_dob,
                                      quantity=-1.0, price=1000.0))
        session.add(doc)
        session.flush()
        return doc.id


def test_sifra_pod_kojom_stoji_mnogo_naziva_je_oznaka(db):
    """„Porez 20%“ nije šifra artikla nego nalepnica za vrstu dokumenta.

    Mereno: Eurotehna šalje `Porez 20%` na svih 107 odobrenja i pod njom stoji
    100 različitih naziva; EWE `USL00027` na 109, Comtrade `R0001` na 90.
    """
    from sefsync.erp.kalkulacija import _sifra_je_oznaka

    for i, naziv in enumerate(("Odobrenje po Saglasnosti broj 1/2026",
                               "Odobrenje po Saglasnosti broj 2/2026",
                               "Kvartalni rabat Q3")):
        _odobrenje_sa_stavkom(db, f"KO-{i}", "Porez 20%", naziv)

    with db.session_scope() as session:
        assert _sifra_je_oznaka(session, "100000142", "Porez 20%")


def test_sifra_sa_jednim_nazivom_ostaje_sifra(db):
    """Prava šifra artikla nosi uvek isti naziv — nju ne diramo."""
    from sefsync.erp.kalkulacija import _sifra_je_oznaka

    for i in range(4):
        _odobrenje_sa_stavkom(db, f"KO-R{i}", "Y30847", "Philips LED TV 55PUS7810")

    with db.session_scope() as session:
        assert not _sifra_je_oznaka(session, "100000142", "Y30847")


def test_oznaka_ne_razresava_stavku_odobrenja(db, monkeypatch):
    """Veza za „Porez 20%“ ne sme da štampa isti artikal na svako odobrenje.

    661FN-2026 je tako dobilo frižider sa 613FN-2026, iako napomena kaže drugi
    model — a predlog iz napomene nije ni računat, jer se računa tek kad stavka
    ostane bez šifre (02.10.).
    """
    from sefsync.erp.kalkulacija import _nasa_sifra
    from sefsync.models import Document, DocumentLine, ItemMapping

    for i, naziv in enumerate(("Odobrenje po Saglasnosti broj 1/2026",
                               "Odobrenje po Saglasnosti broj 2/2026",
                               "Naknadno odobren rabat")):
        doc_id = _odobrenje_sa_stavkom(db, f"KO-X{i}", "Porez 20%", naziv)

    with db.session_scope() as session:
        session.add(ItemMapping(supplier_vat="100000142", supplier_item_id="Porez 20%",
                                erp_item_code="92846", active=True))

    with db.session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(DocumentLine.document_id == doc_id)
        )
        sifra, _ = _nasa_sifra(session, None, "100000142", linija, odobrenje=True)
        assert sifra is None, "odobrenje je dobilo artikal sa tuđeg dokumenta"

        # Na običnoj fakturi se ništa ne menja — tamo je šifra ipak šifra.
        sifra, odakle = _nasa_sifra(session, None, "100000142", linija, odobrenje=False)
        assert sifra == "92846" and odakle == "mapiranje"
