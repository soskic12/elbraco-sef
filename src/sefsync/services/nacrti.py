"""Stanje kalkulacija koje aplikacija sklapa: sta je spremno, a sta ceka coveka.

Generator (`erp.kalkulacija.pripremi`) svaki put iznova sklopi nacrt iz dokumenta
i ERP-a. To je desetak upita po dokumentu - predugo za ekran koji neko osvezava.
Zato se ovde ishod racunanja upisuje u `nacrt_kalkulacije`, pa ekran cita bazu.

Ono sto covek uradi ne cuva se u nacrtu nego u `item_mapping`: kad Dule kaze koja
je nasa sifra, ta veza vazi i za sve buduce fakture istog dobavljaca.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from ..db import session_scope
from ..erp.connection import get_erp_engine
from ..erp.kalkulacija import VecPostoji, pripremi, upisi
from ..models import (
    AuditLog,
    Document,
    DocumentLine,
    DocumentType,
    ItemMapping,  # noqa: F401 - koristi se u razlozi_cekanja
    NacrtKalkulacije,
    ProcessState,
    SefStatus,
    StanjeNacrta,
    utcnow,
)

log = logging.getLogger(__name__)


# Nazivi koji nisu artikal nego finansijska stavka - po njima se prepoznaje da
# nasa sifra nikad nece ni postojati ("Odobrenje po Saglasnosti broj 277102/2026").
_NIJE_ARTIKAL = re.compile(
    r"odobrenje po saglasnosti|saglasnost broj|troskov|troškov|transport|"
    r"naknad|provizij|rabat|bonus|popust|kamat|uslug",
    re.IGNORECASE,
)


def razlozi_cekanja(engine=None) -> dict:
    """Zasto stavke cekaju sifru, po uzroku - ne po utisku.

    Ekran pokazuje sta ceka coveka, ali ne i zasto. Bez ovoga se ne zna da li je
    posao stvarno rucni ili nesto ne radi kako treba.
    """
    eng = engine or get_erp_engine()
    with eng.connect() as conn:
        barkodovi = {
            r.b
            for r in conn.execute(
                text(
                    "SELECT DISTINCT RTRIM(ISNULL(BARCODE,'')) b FROM dbo.ARTIKLI "
                    "WHERE RTRIM(SIFRAMAG)='099' AND RTRIM(ISNULL(BARCODE,'')) <> ''"
                )
            )
        }

    uzroci: Counter = Counter()
    po_dobavljacu: Counter = Counter()
    primeri: dict[str, list[str]] = {}
    stavki = 0

    with session_scope() as session:
        nacrti = list(
            session.scalars(
                select(NacrtKalkulacije).where(
                    NacrtKalkulacije.stanje == StanjeNacrta.CEKA_SIFRU
                )
            )
        )
        dokumenti = {
            d.id: d
            for d in session.scalars(
                select(Document).where(
                    Document.id.in_([n.document_id for n in nacrti] or [0])
                )
            )
        }
        poznate = {
            (m.supplier_vat, m.supplier_item_id)
            for m in session.scalars(
                select(ItemMapping).where(ItemMapping.active == True)  # noqa: E712
            )
        }

        for nacrt in nacrti:
            doc = dokumenti.get(nacrt.document_id)
            for stavka in nacrt.nemapirane or []:
                stavki += 1
                naziv = stavka.get("naziv") or ""
                ean = (stavka.get("ean") or "").strip()
                sifra_dob = (stavka.get("sifra_dobavljaca") or "").strip()
                pib = doc.supplier_vat if doc is not None else None

                if _NIJE_ARTIKAL.search(naziv):
                    uzrok = "nije artikal (odobrenje, troškovi, naknada)"
                elif ean and ean in barkodovi:
                    uzrok = "barkod POSTOJI u šifarniku — treba da se reši sam"
                elif ean:
                    uzrok = "ima barkod, nema ga u šifarniku — nova šifra"
                elif sifra_dob and (pib, sifra_dob) in poznate:
                    uzrok = "šifra dobavljača je poznata — treba da se reši sama"
                elif sifra_dob:
                    uzrok = "nema barkod, šifra dobavljača nije viđena — čovek"
                else:
                    uzrok = "nema ni barkod ni šifru dobavljača — čovek"

                uzroci[uzrok] += 1
                if doc is not None:
                    po_dobavljacu[(doc.supplier_name or "?")[:34]] += 1
                primeri.setdefault(uzrok, [])
                if len(primeri[uzrok]) < 4:
                    primeri[uzrok].append(
                        f"{naziv[:44]} (EAN {ean or '-'}, šifra {sifra_dob or '-'})"
                    )

    return {
        "nacrta": len(nacrti),
        "stavki": stavki,
        "uzroci": uzroci,
        "primeri": primeri,
        "po_dobavljacu": po_dobavljacu,
    }


RUCNO_U_ERPU = "ručno u ERP-u"


_POZNATI: tuple[float, set[str]] | None = None
TRAJANJE_KESA = 120.0  # sekundi


def _poznati_brojevi(*, osvezi_kes: bool = False) -> set[str]:
    """Normalizovani brojevi svih faktura sa SEF-a.

    Sluzi da se iz predloga po iznosu i datumu izbace kalkulacije koje vec
    pripadaju nekoj drugoj fakturi. Bez toga predlog pogodi bankarsku naknadu
    od 234 dinara koja se istog dana ponovi pod drugim brojem.

    Kesira se nakratko: masovno osvezavanje zove `osvezi` po dokumentu, a bez
    kesa bi svaki od njih ponovo procitao svih 7.727 brojeva.
    """
    global _POZNATI
    from .mapiranje import kljuc_racuna

    if not osvezi_kes and _POZNATI is not None:
        kad, poznati = _POZNATI
        if time.monotonic() - kad < TRAJANJE_KESA:
            return poznati
    with session_scope() as session:
        brojevi = session.execute(select(Document.document_number)).scalars().all()
    poznati = {k for k in (kljuc_racuna(b) for b in brojevi) if k}
    _POZNATI = (time.monotonic(), poznati)
    return poznati


def _vec_u_erpu(nacrt, *, engine=None, i_predlog: bool = False):
    """Kalkulacija u ERP-u koja odgovara ovoj fakturi, ako je ima.

    Sa `i_predlog` vraca i nesiguran pogodak (`Pogodak.siguran == False`) - onaj
    nadjen po iznosu i datumu kad se broj ne poklapa.

    Prelazak na novi nacin rada ide odjednom za sve, pa ce se desavati da
    poslovodja napravi kalkulaciju rukom kao do sada. Upis to hvata i odbija
    duplikat, ali tek kad neko klikne - do tada panel trazi prijem robe za
    fakturu koja je vec zaknjizena.
    """
    from ..erp.kalkulacija import moguca_kalkulacija, postojeca_kalkulacija

    broj = str(nacrt.zaglavlje.get("BROJ_RN_DOB") or "").strip()
    dobavljac = str(nacrt.zaglavlje.get("DOBAVLJAC") or "").strip()
    if not broj or not dobavljac:
        return None
    # Brojevi se citaju PRE otvaranja veze ka ERP-u: to je upit nad bazom panela
    # koji vraca 8.000 redova, a drzati ERP vezu dok traje znaci trositi je bez
    # potrebe. Bazen ka ERP-u je pao prvog radnog dana, pa se svaka zauzeta
    # sekunda racuna.
    poznati = _poznati_brojevi() if i_predlog else set()
    try:
        with (engine or get_erp_engine()).connect() as conn:
            nadjeno = postojeca_kalkulacija(conn, broj, dobavljac)
            if nadjeno is not None or not i_predlog:
                return nadjeno
            # Broj se ne poklapa ni u celosti ni odsecen - mozda je covek
            # pogresio u kucanju.
            return moguca_kalkulacija(
                conn,
                broj,
                dobavljac,
                nacrt.zaglavlje.get("IZNOS_ZA_NAP"),
                nacrt.zaglavlje.get("DATUM"),
                poznati,
            )
    except SQLAlchemyError as exc:
        log.info("Provera postojece kalkulacije nije prosla: %s", exc)
        return None


def _stanje(nacrt) -> StanjeNacrta:
    if nacrt.problemi:
        return StanjeNacrta.PROBLEM
    if nacrt.nemapirane:
        return StanjeNacrta.CEKA_SIFRU
    return StanjeNacrta.SPREMAN


def osvezi(document_id: int, *, engine=None) -> NacrtKalkulacije | None:
    """Ponovo sklopi nacrt za jedan dokument i upise ishod. Ne dira ERP upisom."""
    with session_scope() as session:
        doc = session.get(Document, document_id)
        avans = doc is not None and doc.document_type is DocumentType.PREPAYMENT
    if avans:
        # Ne troši se ni jedan upit ka ERP-u: avans nema sta da trazi u kalkulaciji.
        return _upisi_stanje(document_id, StanjeNacrta.NE_TREBA)

    # RBR nosi ime aplikacije, ne coveka - vidi erp.kalkulacija.KORISNIK
    nacrt = pripremi(document_id, engine=engine)

    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        if red is None:
            red = NacrtKalkulacije(document_id=document_id)
            session.add(red)
        if red.stanje is StanjeNacrta.UPISAN:
            # Vec je u ERP-u - ponovno racunanje ne sme da vrati stanje unazad.
            return red
        stanje = _stanje(nacrt)
        if stanje is StanjeNacrta.SPREMAN:
            rucno = _vec_u_erpu(nacrt, engine=engine, i_predlog=True)
            if rucno is not None and rucno.siguran:
                # Poslovodja je kalkulaciju napravio rukom. Panel to mora da
                # prepozna, inace bi do kraja veka trazio prijem robe za nesto
                # sto je vec zaknjizeno - a operater bi na dugme dobio gresku
                # umesto objasnjenja.
                red.stanje = StanjeNacrta.UPISAN
                red.erp_broj = rucno.oznaka[:20]
                red.erp_napomena = (rucno.razlog or None) and rucno.razlog[:250]
                red.sumnja = None
                red.upisao = RUCNO_U_ERPU
                red.upisano_u = utcnow()
                red.magacin = nacrt.magacin or None
                red.tabela = nacrt.tabela_zaglavlja or None
                red.racunato_u = utcnow()
                session.flush()
                session.expunge(red)
                return red
            # Nesiguran pogodak ceka coveka, vidi `sumnja`.
            predlog = rucno
            odbijen = (red.sumnja or {}).get("oznaka") if red.sumnja_odbio else None
            if predlog is not None and predlog.oznaka != odbijen:
                red.sumnja = {"oznaka": predlog.oznaka, "razlog": predlog.razlog}
                red.sumnja_odbio = None
                red.sumnja_odbijeno_u = None
            elif predlog is None and not red.sumnja_odbio:
                red.sumnja = None

        red.stanje = stanje
        red.magacin = nacrt.magacin or None
        red.tabela = nacrt.tabela_zaglavlja or None
        red.iznos = nacrt.zaglavlje.get("IZNOS_ZA_NAP")
        red.problemi = list(nacrt.problemi) or None
        red.nemapirane = list(nacrt.nemapirane) or None
        red.racunato_u = utcnow()
        session.flush()
        session.expunge(red)

    _zabelezi_popust(document_id, nacrt, engine=engine)
    return red


def _zabelezi_popust(document_id: int, nacrt, *, engine=None) -> None:
    """Knjizno odobrenje na deo vrednosti uredjaja - zabelezi da Ilijana vidi.

    Takvo odobrenje ide na 088 i tamo se gubi, a ona je ta koja popust prenosi
    objektu koji ostecen uredjaj prodaje. Zapis ostaje otvoren dok ga ne prenese.
    """
    from ..erp.kalkulacija import MAGACIN_ULAZNI_RACUN, _analitika_dobavljaca
    from ..models import PopustZaPrenos
    from .odobrenja import popust_za_uredjaj

    if nacrt.magacin != MAGACIN_ULAZNI_RACUN:
        return

    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None or doc.document_type is not DocumentType.CREDIT_NOTE:
            return
        if session.get(PopustZaPrenos, document_id) is not None:
            return  # vec zabelezen, ne dira se trag o prenosu
        podaci = {
            "mb": doc.supplier_reg_no, "pib": doc.supplier_vat,
            "racun": doc.payment_account, "napomena": doc.note,
            "osnovica": doc.sum_without_vat,
        }

    try:
        with (engine or get_erp_engine()).connect() as conn:
            dobavljac = _analitika_dobavljaca(
                conn, podaci["mb"], podaci["pib"], podaci["racun"]
            )
            nalaz = popust_za_uredjaj(
                conn,
                napomena=podaci["napomena"],
                dobavljac=dobavljac,
                osnovica=podaci["osnovica"],
            )
    except SQLAlchemyError as exc:
        log.info("Popust za #%s nije proveren: %s", document_id, exc)
        return
    if nalaz is None:
        return

    with session_scope() as session:
        session.add(
            PopustZaPrenos(
                document_id=document_id,
                erp_item_code=nalaz["sifra"],
                naziv_artikla=nalaz["naziv"],
                iznos=nalaz["iznos"],
                nabavna=nalaz["nabavna"],
                broj_racuna=nalaz["racun"],
                baza_godine=nalaz["baza"],
            )
        )
    log.info(
        "Popust za prenos: #%s %s (%s od %s)",
        document_id, nalaz["sifra"], nalaz["iznos"], nalaz["nabavna"],
    )

    # Mejl kaze da je stiglo, ekran cuva trag sta je uradjeno - jedno bez drugog
    # ne radi posao: mejl se izgubi, a ekran niko ne gleda dok ne zna da ima sta.
    try:
        from ..notify.dispatcher import Dispatcher

        Dispatcher().notify_popust(document_id)
    except Exception as exc:  # noqa: BLE001 - posta ne sme da obori nacrt
        log.warning("Obaveštenje o popustu za #%s nije poslato: %s", document_id, exc)


def _upisi_stanje(document_id: int, stanje: StanjeNacrta) -> NacrtKalkulacije:
    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        if red is None:
            red = NacrtKalkulacije(document_id=document_id)
            session.add(red)
        red.stanje = stanje
        red.problemi = None
        red.nemapirane = None
        red.racunato_u = utcnow()
        session.flush()
        session.expunge(red)
        return red


def osvezi_zapele(*, limit: int = 200, engine=None) -> dict:
    """Preracunava samo nacrte koji cekaju coveka, i to na strani ERP-a.

    Petnaestominutni ciklus je do sada radio samo sinhronizaciju sa SEF-om, pa
    kad Dule otvori sifru u ERP-u aplikacija to ne primeti dok neko rucno ne
    pusti `nacrti --osvezi`. Svima je i dalje pisalo „ceka sifru“ iako je posao
    obavljen (zalba 30.09.).

    Gledaju se samo stanja koja ERP moze da odmrzne - `ceka_sifru` i `problem`.
    Spremni i upisani se ne diraju: njih menja covek u panelu, ne sifarnik.
    """
    with session_scope() as session:
        ids = list(
            session.scalars(
                select(NacrtKalkulacije.document_id)
                .join(Document, Document.id == NacrtKalkulacije.document_id)
                .where(
                    NacrtKalkulacije.stanje.in_(
                        (StanjeNacrta.CEKA_SIFRU, StanjeNacrta.PROBLEM)
                    ),
                    Document.archived == False,  # noqa: E712
                )
                .order_by(NacrtKalkulacije.document_id.desc())
                .limit(limit)
            )
        )

    brojac = {"pregledano": len(ids), "odmrznuto": 0, "greska": 0}
    for doc_id in ids:
        try:
            red = osvezi(doc_id, engine=engine)
        except Exception as exc:  # noqa: BLE001 - jedan dokument ne sme da obori ciklus
            brojac["greska"] += 1
            log.warning("Nacrt za #%s nije osvežen: %s", doc_id, exc)
            continue
        if red is not None and red.stanje not in (
            StanjeNacrta.CEKA_SIFRU, StanjeNacrta.PROBLEM
        ):
            brojac["odmrznuto"] += 1
    return brojac


def osvezi_sve(*, samo_razvrstane: bool = True, limit: int | None = None, engine=None) -> dict:
    """Prolazi kroz dokumente i osvezava nacrte. Vraca brojace po stanju."""
    with session_scope() as session:
        upit = select(Document.id).where(Document.archived == False)  # noqa: E712
        if samo_razvrstane:
            upit = upit.where(Document.business_unit_id.is_not(None))
        upit = upit.where(Document.state != ProcessState.ERROR).order_by(Document.id.desc())
        if limit:
            upit = upit.limit(limit)
        ids = list(session.scalars(upit))

    brojac = {s.value: 0 for s in StanjeNacrta}
    brojac["greska"] = 0
    for doc_id in ids:
        try:
            red = osvezi(doc_id, engine=engine)
        except Exception as exc:  # noqa: BLE001 - jedan dokument ne sme da obori posao
            brojac["greska"] += 1
            log.warning("Nacrt za #%s nije napravljen: %s", doc_id, exc)
            continue
        if red is not None:
            brojac[red.stanje.value] += 1
    return brojac


def oznaci_nije_artikal(
    document_id: int, line_no: int, *, korisnik: str, vrati: bool = False, engine=None
) -> NacrtKalkulacije | None:
    """Stavka koja nikad nece imati nasu sifru (odobrenje po saglasnosti, troskovi).

    Ne otvara se sifra i ne ceka se - stavka prosto izlazi iz kalkulacije. `vrati`
    ponistava oznaku, ako je neko pogresio.
    """
    with session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_id == document_id, DocumentLine.line_no == line_no
            )
        )
        if linija is None:
            raise ValueError(f"Stavka {line_no} ne postoji na dokumentu #{document_id}.")
        linija.nije_artikal = not vrati
        linija.nije_artikal_by = None if vrati else korisnik

    return osvezi(document_id, engine=engine)


def upisi_u_erp(
    document_id: int, *, korisnik: str, produkcija: bool = False, engine=None
) -> tuple[bool, str]:
    """Upisuje kalkulaciju u ERP. Vraca (da li je upisana, poruka za coveka).

    Zove se kad poslovodja potvrdi prijem robe - ranije nema smisla, jer bi app
    pretekla poslovodju koji kalkulaciju i dalje pravi rukom.

    Ne baca izuzetak na ono sto nije greska programa (nije spremna, vec postoji,
    ERP nedostupan) - to su stanja o kojima covek treba da procita recenicu.
    """
    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        stanje = red.stanje if red else None
        vec_upisan = red.erp_broj if red else None
        doc = session.get(Document, document_id)
        status = doc.sef_status if doc is not None else None

    # Faktura koja je povucena sa SEF-a nema sta da trazi u ERP-u. Do sada je
    # ovo bilo bez posledica jer app nije upisivala - mereno 26.09., nijedan od
    # 32 stornirana dokumenta nema potvrdjen prijem. Od 28.09. app upisuje, pa
    # bi storno koji stigne posle potvrde prijema ostavio kalkulaciju za
    # ponistenu fakturu.
    if status is SefStatus.STORNO:
        return False, "Faktura je stornirana na SEF-u — kalkulacija se ne upisuje."
    if status is SefStatus.REJECTED:
        return False, "Faktura je odbijena na SEF-u — kalkulacija se ne upisuje."

    if stanje is StanjeNacrta.UPISAN:
        return False, f"Kalkulacija je već upisana ({vec_upisan})."
    if stanje is StanjeNacrta.NE_TREBA:
        return False, "Ovaj dokument ne ide kroz kalkulaciju."

    try:
        nacrt = pripremi(document_id, engine=engine)
    except Exception as exc:  # noqa: BLE001 - ERP ume da bude nedostupan
        log.warning("Nacrt za #%s nije sklopljen: %s", document_id, exc)
        _zabelezi_neuspeh(document_id, f"kalkulacija nije sklopljena: {exc}")
        return False, f"Kalkulacija nije sklopljena: {exc}"

    if not nacrt.spreman:
        # Jedini put koji nije ostavljao nikakav trag: ni u dnevniku ni na
        # dokumentu. Faktura 26-15240-238911 je tako stajala - prijem potvrdjen,
        # upis odbijen jer stavka nije imala sifru, i nigde se to nije videlo
        # (30.09.).
        razlog = "; ".join(
            [f"stavka {x['line_no']}: {x['naziv']}" for x in nacrt.nemapirane[:2]]
            + nacrt.problemi[:2]
        )
        log.info("Kalkulacija za #%s nije spremna: %s", document_id, razlog[:160])
        osvezi(document_id, engine=engine)
        return False, "Kalkulacija još nije spremna — pogledaj tab „Šifre“."

    try:
        opis = upisi(nacrt, produkcija=produkcija)
    except VecPostoji as exc:
        _zabelezi_upis(document_id, str(exc).split(": ")[-1], "ručno u ERP-u", opis=str(exc))
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - poruka ide coveku, trag u log
        log.exception("Kalkulacija za #%s nije upisana", document_id)
        _zabelezi_neuspeh(document_id, f"upis nije prošao: {exc}")
        return False, f"Upis nije prošao: {exc}"

    # U kolonu ide samo broj kalkulacije (staje u 20 znakova) - ceo opis, sa
    # imenom baze, ostaje u dnevniku.
    broj = f"{nacrt.magacin}/{nacrt.zaglavlje.get('BROJ', '')}"
    _zabelezi_upis(document_id, broj, korisnik, opis=opis)
    _zabelezi_neuspeh(document_id, None)   # uspelo je, staro upozorenje se skida
    poruka = f"Kalkulacija je upisana u ERP ({opis})."
    return True, poruka + prihvati_ako_je_kompletna(document_id, korisnik)


def kompletna(session, doc) -> tuple[bool, str]:
    """Da li je fakturi sve na broju, pa sme da se prihvati na SEF-u.

    Nekompletna je ona kojoj nesto fali ili se o njoj jos odlucuje. Prihvatanje
    je nepovratno - kad se prihvati, odbijanje vise nije moguce, ostaje samo
    knjizno odobrenje. Zato faktura sa otvorenom prijavom manjka ceka: mozda
    ostatak nikad ne stigne.
    """
    from ..models import PrijavaManjka, SefStatus

    if doc.sef_status in (SefStatus.APPROVED, SefStatus.REJECTED, SefStatus.STORNO):
        return False, f"već je {doc.sef_status.value} na SEF-u"
    manjak = session.get(PrijavaManjka, doc.id)
    if manjak is not None and manjak.otvoren:
        return False, "prijavljen je manjak koji još nije rešen"
    return True, ""


def prihvati_ako_je_kompletna(document_id: int, korisnik: str) -> str:
    """Prihvata fakturu na SEF-u posle upisa, ako joj nista ne fali.

    Bane, 29.09.: „automatski prihvati svaku koja je kompletna, kojoj ne fali
    roba i slicno; ove nekompletne kad postanu kompletne ili se vrate operateru
    na odlucivanje”.

    Vraca dodatak za poruku coveku, ili prazno kad nema sta da se kaze.
    Neuspeh prihvatanja ne ponistava upis - kalkulacija jeste u ERP-u.
    """
    from ..config import get_settings

    if not get_settings().prihvati_posle_upisa:
        return ""

    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None:
            return ""
        sme, zasto = kompletna(session, doc)
    if not sme:
        if "manjak" in zasto:
            return f" Na SEF-u nije prihvaćena — {zasto}."
        return ""

    from .acceptance import AcceptanceService

    try:
        ishod = AcceptanceService().accept(
            document_id, actor=korisnik, comment="Prihvaćeno uz upis kalkulacije"
        )
    except Exception as exc:  # noqa: BLE001 - SEF ume da bude nedostupan
        log.exception("Prihvatanje dokumenta %s posle upisa nije proslo", document_id)
        return f" Na SEF-u NIJE prihvaćena ({exc}) — uradi to ručno."
    if ishod.ok:
        return " Prihvaćena je i na SEF-u."
    return f" Na SEF-u nije prihvaćena: {ishod.message}"


def _zabelezi_neuspeh(document_id: int, razlog: str | None) -> None:
    """Zasto upis nije prosao - da ostane na dokumentu, a ne samo u poruci.

    Potvrda prijema pokusava upis, i kad upis padne poruka se pojavi jednom na
    ekranu pa nestane. Poslovodja je vidi u prolazu, operater nikad. Mereno
    30.09.: faktura 26-15240-238911 imala je nacrt „spreman“ i potvrdjen prijem,
    a kalkulacije u ERP-u nije bilo - i nigde se nije videlo zasto.
    """
    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        if red is None:
            return
        red.upozorenje = f"Upis nije prošao: {razlog}"[:250] if razlog else None


def _zabelezi_upis(document_id: int, broj: str, korisnik: str, *, opis: str = "") -> None:
    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        if red is None:
            red = NacrtKalkulacije(document_id=document_id)
            session.add(red)
        red.stanje = StanjeNacrta.UPISAN
        red.erp_broj = broj[:20]
        red.upisano_u = utcnow()
        red.upisao = korisnik
        session.add(
            AuditLog(
                actor=korisnik,
                action="kalkulacija.upisana",
                document_id=document_id,
                detail=opis or broj,
            )
        )


def povezi_sifru(
    document_id: int,
    line_no: int,
    erp_item_code: str,
    *,
    korisnik: str,
    pamti: bool = True,
    engine=None,
) -> NacrtKalkulacije | None:
    """Covek je rekao koja je nasa sifra za jednu stavku.

    Veza se pamti u `item_mapping` (po dobavljacu i njegovoj sifri, pa i po
    barkodu kad ga ima), dakle vazi i za sve naredne fakture. Zato je ovo
    najvredniji klik u celom toku - ne resava jedan dokument nego sve buduce.

    Ali pamti se samo ono sto je jednoznacno. Neki dobavljaci jednom sifrom
    pokrivaju vise nasih artikala - CSM salje "ponye 20 litium" bez oznake boje,
    a kod nas je svaka boja svoja sifra. Kad se ista dobavljaceva sifra jednom
    veze za jedan artikal, a drugi put za drugi, veza se **gasi** umesto da se
    prepise: sledeci put mora opet da odluci covek. Prepisivanje bi znacilo da
    naredna faktura tiho dobije boju od proslog puta.

    `pamti=False` je za slucaj kad covek unapred zna da je sifra dvosmislena.
    """
    sifra = (erp_item_code or "").strip()
    if not sifra:
        raise ValueError("Šifra artikla je prazna.")

    with session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_id == document_id, DocumentLine.line_no == line_no
            )
        )
        if linija is None:
            raise ValueError(f"Stavka {line_no} ne postoji na dokumentu #{document_id}.")
        doc = session.get(Document, document_id)
        pib = (doc.supplier_vat or "").strip()
        if not pib:
            raise ValueError("Dokument nema PIB dobavljača — veza se ne bi imala za šta zakačiti.")

        linija.erp_item_code = sifra
        linija.erp_mapping_source = "rucno"

        # Kad stavka nema ni sifru dobavljaca ni barkod, identitet joj je NAZIV.
        # Ranije je kljuc bio (dobavljac, prazno, prazno) - isti za svaki takav
        # artikal tog dobavljaca - pa je svako novo povezivanje gasilo prethodno
        # kao "kolebljivo". Dule je povezao sve LEXA zvucnike i sve su mu ostale
        # na spisku, jer je poslednji klik ubio sve pre njega (30.09.).
        ima_kljuc = bool((linija.sellers_item_id or "").strip()
                         or (linija.standard_item_id or "").strip())
        if ima_kljuc:
            postoji = session.scalar(
                select(ItemMapping).where(
                    ItemMapping.supplier_vat == pib,
                    ItemMapping.supplier_item_id == (linija.sellers_item_id or None),
                    ItemMapping.barcode == (linija.standard_item_id or None),
                )
            )
        else:
            from ..textutil import normalize

            ime = normalize(linija.name or "")
            postoji = next(
                (
                    m
                    for m in session.scalars(
                        select(ItemMapping).where(
                            ItemMapping.supplier_vat == pib,
                            ItemMapping.supplier_item_id.is_(None),
                            ItemMapping.barcode.is_(None),
                        )
                    )
                    if normalize(m.supplier_item_name or "") == ime
                ),
                None,
            )
        if postoji is not None and postoji.erp_item_code.strip() != sifra:
            # Ista dobavljaceva sifra, drugi artikal - dakle ne znaci jedno.
            postoji.active = False
            postoji.source = "kolebljivo"
            postoji.misses += 1
            session.add(
                AuditLog(
                    actor=korisnik,
                    action="mapiranje.kolebljivo",
                    document_id=document_id,
                    detail=f"{pib}/{linija.sellers_item_id or '-'}: "
                           f"{postoji.erp_item_code} -> {sifra}; veza ugašena",
                )
            )
        elif postoji is not None:
            # Covek je rekao istu sifru kao i pre - to je potvrda, pa ugasena
            # veza ozivljava. Bez ovoga je mogao da klikce koliko hoce i nista
            # se ne bi promenilo: veza ostane mrtva a stavka i dalje ceka
            # (naslo se 30.09. uz LEXA zvucnike).
            if not postoji.active:
                postoji.active = True
                postoji.source = "rucno"
                session.add(
                    AuditLog(
                        actor=korisnik,
                        action="mapiranje.ozivljeno",
                        document_id=document_id,
                        detail=f"{pib}/{linija.name or '-'}: {sifra}",
                    )
                )
            postoji.confirmed_at = utcnow()
            postoji.confirmed_by = korisnik
            postoji.hits += 1
        elif pamti:
            session.add(
                ItemMapping(
                    supplier_vat=pib,
                    supplier_item_id=linija.sellers_item_id or None,
                    barcode=linija.standard_item_id or None,
                    supplier_item_name=(linija.name or "")[:500] or None,
                    erp_item_code=sifra,
                    source="rucno",
                    confirmed_at=utcnow(),
                    confirmed_by=korisnik,
                )
            )

    osvezeni = osvezi(document_id, engine=engine)

    # Isti artikal stize na vise objekata. Kad se sifra jednom kaze, sve ostale
    # fakture koje ga cekaju moraju da prodju same - inace poslovodja u drugom
    # objektu i dalje gleda "ceka sifru" za nesto sto je vec reseno.
    for drugi in _cekaju_isti_artikal(document_id, line_no):
        try:
            osvezi(drugi, engine=engine)
        except Exception as exc:  # noqa: BLE001 - jedan dokument ne sme da obori ostale
            log.warning("Nacrt za #%s nije osvezen: %s", drugi, exc)
    return osvezeni


def _cekaju_isti_artikal(document_id: int, line_no: int) -> list[int]:
    """Dokumenti koji cekaju sifru za isti artikal (po barkodu ili sifri dobavljaca)."""
    with session_scope() as session:
        linija = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_id == document_id, DocumentLine.line_no == line_no
            )
        )
        if linija is None:
            return []
        doc = session.get(Document, document_id)
        uslovi = []
        if linija.standard_item_id:
            uslovi.append(DocumentLine.standard_item_id == linija.standard_item_id)
        if linija.sellers_item_id and doc is not None and doc.supplier_vat:
            uslovi.append(
                and_(
                    DocumentLine.sellers_item_id == linija.sellers_item_id,
                    Document.supplier_vat == doc.supplier_vat,
                )
            )
        # Kad nema ni barkoda ni sifre dobavljaca, jedino preostaje naziv - a bez
        # toga se veza ne bi prenela ni na jednu drugu fakturu.
        if not uslovi and linija.name and doc is not None and doc.supplier_vat:
            uslovi.append(
                and_(
                    DocumentLine.name == linija.name,
                    Document.supplier_vat == doc.supplier_vat,
                )
            )
        if not uslovi:
            return []
        return list(
            session.scalars(
                select(Document.id)
                .join(DocumentLine, DocumentLine.document_id == Document.id)
                .join(NacrtKalkulacije, NacrtKalkulacije.document_id == Document.id)
                .where(
                    Document.id != document_id,
                    Document.archived == False,  # noqa: E712
                    NacrtKalkulacije.stanje == StanjeNacrta.CEKA_SIFRU,
                    or_(*uslovi),
                )
                .distinct()
            )
        )
