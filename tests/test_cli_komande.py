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


def test_nacrti_spisak_radi_i_bez_magacina(db):
    """Zaustavljene kalkulacije se traže po stanju, a magacin se ne zna unapred."""
    from sefsync.models import Document, NacrtKalkulacije, StanjeNacrta

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=7, document_number="FA-1/26",
                       supplier_name="NEKI DOBAVLJAČ")
        session.add(doc)
        session.flush()
        session.add(NacrtKalkulacije(
            document_id=doc.id, stanje=StanjeNacrta.PROBLEM, magacin="021",
            problemi=["dobavljač nije otvoren u šifarniku"],
        ))

    ishod = _pokreni("nacrti", "--spisak", "--stanje", "problem")
    assert "Svi magacini" in ishod.stdout
    assert "FA-1/26" in ishod.stdout
    # razlog mora da se vidi - sam broj ne govori ništa
    assert "nije otvoren u šifarniku" in ishod.stdout


def test_analiza_razvrstavanja_kaze_kad_nema_sta_da_se_oceni(db):
    """Bez ijedne ljudske potvrde ocena ne postoji — i to mora da se kaže."""
    ishod = _pokreni("analiza-razvrstavanja")
    assert "nema šta da se oceni" in ishod.stdout


def test_analiza_razvrstavanja_racuna_tacnost(db):
    from sefsync.models import Document, RoutingSource, utcnow

    with db.session_scope() as session:
        for i in range(3):
            session.add(Document(
                sef_invoice_id=100 + i, document_number=f"F{i}",
                routing_source=RoutingSource.RULE, routing_confirmed_at=utcnow(),
                routing_corrected=False,
            ))
        session.add(Document(
            sef_invoice_id=200, document_number="F-lose",
            routing_source=RoutingSource.MANUAL, routing_confirmed_at=utcnow(),
            routing_corrected=True,
        ))

    ishod = _pokreni("analiza-razvrstavanja")
    assert "Predloga koje je čovek video: 4" in ishod.stdout
    assert "75.0%" in ishod.stdout      # 3 od 4 ostalo kako je app predložila
    assert "25.0%" in ishod.stdout      # 1 ispravka


def test_pravila_sukobi_nalazi_isti_obrazac_na_vise_jedinica(db):
    """Mereno 29.09.: ista adresa vodila je na deset različitih jedinica."""
    from sefsync.models import BusinessUnit, MatchField, MatchOp, RoutingRule, UnitKind

    with db.session_scope() as session:
        a = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        b = BusinessUnit(code="MP001", name="SOMBOR", kind=UnitKind.RETAIL, erp_code="001")
        session.add_all([a, b])
        session.flush()
        for jedinica, pogodaka in ((a, 40), (b, 3)):
            session.add(RoutingRule(
                field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
                pattern="Srpskih vladara 46", business_unit_id=jedinica.id,
                hits=pogodaka,
            ))

    suvo = _pokreni("pravila-sukobi")
    assert "Srpskih vladara 46".upper() in suvo.stdout.upper()
    # oba su nešto odlučivala — to je čovekova odluka, ne komandina
    assert "TVOJA ODLUKA" in suvo.stdout

    _pokreni("pravila-sukobi", "--stvarno")

    with db.session_scope() as session:
        from sqlalchemy import select

        aktivna = list(session.scalars(
            select(RoutingRule).where(RoutingRule.active == True)  # noqa: E712
        ))
        assert len(aktivna) == 2, "ništa što je radilo ne sme da se ugasi samo"


def test_pravila_sukobi_gasi_samo_ono_sto_nista_nije_odlucilo(db):
    from sqlalchemy import select

    from sefsync.models import BusinessUnit, MatchField, MatchOp, RoutingRule, UnitKind

    with db.session_scope() as session:
        a = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        b = BusinessUnit(code="MAG010", name="POVRAT", kind=UnitKind.WAREHOUSE, erp_code="010")
        session.add_all([a, b])
        session.flush()
        session.add(RoutingRule(
            field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
            pattern="Srpskih vladara 46", business_unit_id=a.id, hits=40,
        ))
        session.add(RoutingRule(       # nastalo greškom, nikad ništa nije odlučilo
            field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
            pattern="Srpskih vladara 46", business_unit_id=b.id,
        ))

    _pokreni("pravila-sukobi", "--stvarno")

    with db.session_scope() as session:
        aktivna = list(session.scalars(
            select(RoutingRule).where(RoutingRule.active == True)  # noqa: E712
        ))
        assert len(aktivna) == 1
        assert aktivna[0].hits == 40


def test_pravilo_gasi_i_vraca_bez_gubitka_brojaca(db):
    """Gasi se, ne briše — brojači su jedini podatak koliko je pravilo vredelo."""
    from sefsync.models import BusinessUnit, MatchField, MatchOp, RoutingRule, UnitKind

    with db.session_scope() as session:
        u = BusinessUnit(code="MP003", name="ODŽACI", kind=UnitKind.RETAIL, erp_code="003")
        session.add(u)
        session.flush()
        p = RoutingRule(
            field=MatchField.SUPPLIER_VAT, op=MatchOp.CONTAINS, pattern="103699024",
            business_unit_id=u.id, hits=5, misses=5,
        )
        session.add(p)
        session.flush()
        broj = p.id

    ishod = _pokreni("pravilo", str(broj))
    assert "103699024" in ishod.stdout
    assert "tačnost 50%" in ishod.stdout

    _pokreni("pravilo", str(broj), "--ugasi")
    with db.session_scope() as session:
        p = session.get(RoutingRule, broj)
        assert p.active is False
        assert p.hits == 5 and p.misses == 5      # istorija ostaje

    _pokreni("pravilo", str(broj), "--upali")
    with db.session_scope() as session:
        assert session.get(RoutingRule, broj).active is True


def test_pravila_prioritet_daje_prednost_tacnom_pogotku(db):
    """Motor uzima prvo pravilo koje pogodi — prioritet je jedini način da se kaže šta je važnije."""
    from sqlalchemy import select

    from sefsync.models import BusinessUnit, MatchField, MatchOp, RoutingRule, UnitKind

    with db.session_scope() as session:
        office = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        mp = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        session.add_all([office, mp])
        session.flush()
        session.add(RoutingRule(              # tačan pogodak po dobavljaču
            priority=60, field=MatchField.SUPPLIER_VAT, op=MatchOp.EQUALS,
            pattern="100001159", business_unit_id=office.id, hits=322,
        ))
        session.add(RoutingRule(              # opšta adresa
            priority=50, field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
            pattern="Srpskih vladara 46", business_unit_id=mp.id,
        ))

    _pokreni("pravila-prioritet", "30", "--polje", "supplier_vat",
             "--op", "equals", "--jedinica", "OFFICE", "--stvarno")

    with db.session_scope() as session:
        po_pibu = session.scalar(
            select(RoutingRule).where(RoutingRule.field == MatchField.SUPPLIER_VAT)
        )
        adresno = session.scalar(
            select(RoutingRule).where(RoutingRule.field == MatchField.DELIVERY_ADDRESS)
        )
        assert po_pibu.priority == 30
        assert adresno.priority == 50
        assert po_pibu.priority < adresno.priority, "PIB mora da se proveri pre adrese"


def test_objasni_pokazuje_koje_pravilo_odlucuje(db):
    """Iz spiska pravila se ne vidi ko se otima oko dokumenta — ovde se vidi."""
    from sefsync.models import (
        BusinessUnit, Document, MatchField, MatchOp, RoutingRule, UnitKind,
    )

    with db.session_scope() as session:
        office = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        mp = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        session.add_all([office, mp])
        session.flush()
        session.add(RoutingRule(
            priority=40, field=MatchField.ANY_TEXT, op=MatchOp.CONTAINS,
            pattern="rabat", business_unit_id=office.id,
        ))
        session.add(RoutingRule(
            priority=50, field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
            pattern="Srpskih vladara 46", business_unit_id=mp.id,
        ))
        doc = Document(
            sef_invoice_id=900, document_number="F-1",
            supplier_name="NEKO", supplier_vat="100000000",
            delivery_address="Srpskih vladara 46, 25260, Apatin",
            note="odobravamo rabat",
        )
        session.add(doc)
        session.flush()
        broj = doc.id

    ishod = _pokreni("objasni", str(broj))
    assert "ODLUČUJE" in ishod.stdout
    # opšte tekstualno pravilo na 40 pretiče adresu na 50
    prvi = ishod.stdout.index("prioritet 40")
    drugi = ishod.stdout.index("prioritet 50")
    assert prvi < drugi


def test_objasni_ne_prikazuje_pravila_za_drugu_vrstu_dokumenta(db):
    """Prva verzija je imala svoju kopiju uslova i ispustila proveru vrste.

    Zbog toga je prikazivala pravila koja motor nikad ne bi pogledao — i na
    osnovu toga je izveden pogrešan zaključak o razvrstavanju.
    """
    from sefsync.models import (
        BusinessUnit, Document, DocumentType, MatchField, MatchOp, RoutingRule, UnitKind,
    )

    with db.session_scope() as session:
        office = BusinessUnit(code="OFFICE", name="UPRAVA", kind=UnitKind.HQ)
        mp = BusinessUnit(code="MP002", name="APATIN", kind=UnitKind.RETAIL, erp_code="002")
        session.add_all([office, mp])
        session.flush()
        session.add(RoutingRule(          # važi samo za knjižna odobrenja
            priority=40, field=MatchField.ANY_TEXT, op=MatchOp.CONTAINS,
            pattern="rabat", business_unit_id=office.id,
            document_type=DocumentType.CREDIT_NOTE,
        ))
        session.add(RoutingRule(
            priority=50, field=MatchField.DELIVERY_ADDRESS, op=MatchOp.CONTAINS,
            pattern="Srpskih vladara 46", business_unit_id=mp.id,
        ))
        doc = Document(
            sef_invoice_id=901, document_number="F-2", document_type=DocumentType.INVOICE,
            supplier_name="NEKO", supplier_vat="100000000",
            delivery_address="Srpskih vladara 46, 25260, Apatin",
            note="odobravamo rabat",
        )
        session.add(doc)
        session.flush()
        broj = doc.id

    ishod = _pokreni("objasni", str(broj))
    assert "rabat" not in ishod.stdout, "pravilo za odobrenja ne važi za fakturu"
    assert "Srpskih vladara 46" in ishod.stdout
    assert "MP002" in ishod.stdout


def test_dokument_kaze_sta_drzi_kalkulaciju(db):
    """Odgovor na „zašto ovo nije prošlo" sklapao se iz tri-četiri komande."""
    from sefsync.models import (
        BusinessUnit, Document, NacrtKalkulacije, StanjeNacrta, UnitKind, utcnow,
    )

    with db.session_scope() as session:
        u = BusinessUnit(code="MAG021", name="CMO SOMBOR", kind=UnitKind.WAREHOUSE,
                         erp_code="021")
        session.add(u)
        session.flush()
        doc = Document(
            sef_invoice_id=555, document_number="26-15240-238911",
            supplier_name="MISON DOO BEOGRAD", amount=303937.35,
            business_unit_id=u.id, received_at=utcnow(), received_by="MikicNemanja",
        )
        session.add(doc)
        session.flush()
        session.add(NacrtKalkulacije(
            document_id=doc.id, stanje=StanjeNacrta.CEKA_SIFRU, magacin="021",
            nemapirane=[{"line_no": 3, "naziv": "FRIŽIDER X", "ean": "123", "sifra_dobavljaca": "A9"}],
        ))

    ishod = _pokreni("dokument", "26-15240-238911")
    assert "MISON DOO" in ishod.stdout
    assert "MikicNemanja" in ishod.stdout
    assert "ceka_sifru" in ishod.stdout
    assert "FRIŽIDER X" in ishod.stdout       # tačno ono što je drži


def test_dokument_prima_i_id_iz_panela(db):
    from sefsync.models import Document

    with db.session_scope() as session:
        doc = Document(sef_invoice_id=556, document_number="F-77", supplier_name="X")
        session.add(doc)
        session.flush()
        broj = doc.id

    assert "F-77" in _pokreni("dokument", f"#{broj}").stdout


def test_dovrsi_upise_nalazi_primljeno_a_neupisano(db, monkeypatch):
    """Roba primljena, nacrt u međuvremenu spreman, a upis niko ne pokreće."""
    import datetime as dt

    from sefsync.models import (
        Document, NacrtKalkulacije, OdlukaManjka, PrijavaManjka, StanjeNacrta, utcnow,
    )
    from sefsync.services import nacrti as N

    monkeypatch.setattr(get_settings(), "erp_upis_kalkulacije", True, raising=False)
    monkeypatch.setattr(get_settings(), "novi_tok_od", dt.date(2026, 9, 28), raising=False)

    with db.session_scope() as session:
        def dodaj(broj, stanje, prijem=True, manjak=False, kada=None):
            doc = Document(
                sef_invoice_id=abs(hash(broj)) % 10**6, document_number=broj,
                supplier_name="MISON DOO",
                created_at=kada or dt.datetime(2026, 9, 29, 9, 0),
                received_at=utcnow() if prijem else None,
                received_by="MikicNemanja" if prijem else None,
            )
            session.add(doc)
            session.flush()
            session.add(NacrtKalkulacije(document_id=doc.id, stanje=stanje, magacin="004"))
            if manjak:
                session.add(PrijavaManjka(
                    document_id=doc.id, odluka=OdlukaManjka.KASNIJE, prijavio="predrag",
                    stavke=[{"line_no": 1, "naziv": "X", "fakturisano": 2, "stiglo": 1}],
                ))

        dodaj("TREBA-1", StanjeNacrta.SPREMAN)
        dodaj("NIJE-PRIMLJENO", StanjeNacrta.SPREMAN, prijem=False)
        dodaj("CEKA-SIFRU", StanjeNacrta.CEKA_SIFRU)
        dodaj("SA-MANJKOM", StanjeNacrta.SPREMAN, manjak=True)
        dodaj("PO-STAROM", StanjeNacrta.SPREMAN, kada=dt.datetime(2026, 9, 20, 9, 0))

    suvo = _pokreni("dovrsi-upise")
    assert "TREBA-1" in suvo.stdout
    for ne_sme in ("NIJE-PRIMLJENO", "CEKA-SIFRU", "SA-MANJKOM", "PO-STAROM"):
        assert ne_sme not in suvo.stdout, f"{ne_sme} ne sme da se dira"

    pozvano = []
    monkeypatch.setattr(N, "upisi_u_erp",
                        lambda doc_id, **k: (pozvano.append(doc_id), (True, "Upisana."))[1])
    import sefsync.cli as C
    monkeypatch.setattr(C, "upisi_u_erp", N.upisi_u_erp, raising=False)

    _pokreni("dovrsi-upise", "--stvarno")
    assert len(pozvano) == 1


def test_ozivi_veze_vraca_samo_jednoznacne(db):
    """Dulet su sve LEXA veze ostale ugašene jer im je ključ bio prazan."""
    from sqlalchemy import select

    from sefsync.models import ItemMapping

    with db.session_scope() as session:
        # dva različita zvučnika istog dobavljača — ugašeni greškom
        for ime, sifra in (("LEXA BT ZVUCNIK LX-201 SPARK", "98101"),
                           ("LEXA BT ZVUCNIK LX-302 NITRO", "98102")):
            session.add(ItemMapping(
                supplier_vat="100123456", supplier_item_name=ime,
                erp_item_code=sifra, source="kolebljivo", active=False,
            ))
        # isti naziv, dve šifre — tu je gašenje bilo ispravno
        for sifra in ("15064", "84154"):
            session.add(ItemMapping(
                supplier_vat="107351003", supplier_item_name="MASINA ZA PRANJE VESA",
                erp_item_code=sifra, source="kolebljivo", active=False,
            ))

    suvo = _pokreni("ozivi-veze")
    assert "oživljava se (naziv vodi na jednu šifru): 2" in suvo.stdout
    assert "ostaje ugašeno (naziv stvarno dvosmislen): 2" in suvo.stdout

    _pokreni("ozivi-veze", "--stvarno")

    with db.session_scope() as session:
        zive = sorted(
            v.erp_item_code
            for v in session.scalars(
                select(ItemMapping).where(ItemMapping.active == True)  # noqa: E712
            )
        )
    assert zive == ["98101", "98102"]
