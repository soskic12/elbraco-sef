"""Faza 2: gradjenje ulazne kalkulacije u ERP-u iz e-fakture.

Nista se ne izmislja - svako polje je prepisano sa stvarnih kalkulacija koje
su ljudi napravili (vidi `docs/FAZA-2.md`). Nacrt se prvo ispisuje i poredi sa
onim sto je covek uneo za isti dokument, pa tek onda upisuje, i to podrazumevano
u test bazu.

Odrediste bira poslovna jedinica iz faze 1:

    prodavnica -> MKALKUL  + MPRULK
    magacin    -> ULKALKUL + PRULKALK
    uprava     -> ULKALKUL, magacin 088, bez stavki (ulazni racun)
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.engine import Connection, Engine

from ..db import session_scope
from ..models import Document, DocumentLine, DocumentType, ItemMapping, UnitKind
from ..routing.engine import MAGACIN_POVRACAJ
from ..textutil import normalize
from .connection import erp_engine_za_upis, get_erp_engine

log = logging.getLogger(__name__)

# Clarion broji dane od ovog datuma.
CLARION_NULA = dt.date(1800, 12, 28)

MAGACIN_ULAZNI_RACUN = "088"
IZVOR = "Elektronske fakture"

# Podela racuna po tipu PDV evidencije (tab POPDV na formi ulaznog racuna).
# Redovi se pisu u PDVEVIDX; knjizenje ih razvije u PDVEVID. Mereno: podelu ima
# 87 od 3.075 racuna na 088 i kod svih se broj redova poklapa red za red.
TABELA_PDV_EVIDENCIJE = "PDVEVIDX"
TIP_ULAZNI_RACUN = 1     # sifarnik TIPEVID: "ULAZNI RACUNI"
TIP_BEZ_EPPDV = 23       # "ULAZNI RACUNI (BEZ EPPDV)" - racun koji ne nosi PDV
TIP_POVRAT_ROBE = 35           # "Povrat robe" - magacin 010, sve negativno
TIP_FINANSIJSKO_ODOBRENJE = 34  # "Finansijska odobrenja" - negativno, bez robe

# Clarion ne ostavlja NULL: u kolonu koja nije popunjena upisuje nulu ili prazan
# string. Nas INSERT je nabrajao samo polja koja racunamo, pa je ostalo 78 kolona
# sa NULL-om kojih u ERP-ovim zapisima nema nijedna.
TIPOVI_BROJEVI = {"tinyint", "smallint", "int", "bigint", "float", "real",
                  "decimal", "numeric", "money", "smallmoney"}
TIPOVI_TEKST = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
_KOLONE: dict[str, dict[str, str]] = {}

# Izmereno na 4.652 kalkulacije: stopa -> sintetski konto poreza.
SINTPOR_PO_STOPI = {20.0: "270", 10.0: "271", 0.0: "272"}

# PROKNJIZENO: aplikacija SME da upise samo "nije zaknjizeno".
#
# Knjizenje se radi iskljucivo u ERP-u, dugmetom "KNJIZENJE". Taj postupak ne
# menja samo kalkulaciju - on nastavlja u glavnu knjigu i ostale evidencije.
# Ako aplikacija upise kalkulaciju kao vec zaknjizenu, ona izgleda gotovo a
# nista od toga se nije desilo: roba nije zaduzena, glavna knjiga ne zna za nju,
# a poslovodja nema sta da klikne. Zato je vrednost zakucana i proverava se
# pri upisu.
PROKNJIZENO_NIJE = 0    # nije proknjizena - jedina vrednost koju app sme da upise
PROKNJIZENO_JESTE = 1   # proknjizio poslovodja - NIKAD iz aplikacije
# Proknjizena pa je administrator zakljucao da ne moze unazad da se menja, zbog
# obracuna PDV-a. Dakle i ovo je zaknjizeno, ne samo "zakljucano" (Bane, 26.09.).
PROKNJIZENO_ZAKLJUCANO = 2

# ID PDV evidencije stoji u koloni koja se zove `LASTUSER` - ime vara, ali je
# tako (provereno 30.09. na celoj bazi: 088/00011 OTP banka ima LASTUSER 75, i
# u PDVEVID mu odgovara red TIP 75 "Provizije, takse"). Sifarnik je TIPEVID.
#
# Nasi dosadasnji upisi imali su 0, sto je prazna vrsta - `dopuni_prazna` je
# popunjavala nulom jer joj nista nismo zadali. Bogdan je to rucno ispravljao.
KOLONA_PDV_EVIDENCIJE = "LASTUSER"

# Koliko puta dobavljac mora da se pojavi sa istom vrstom da bi se preuzela, i
# koliki deo njegove istorije ta vrsta mora da nosi.
#
# "Uvek ista vrsta" je previse krto: Banca intesa ima 323 puta 75 i po jednom 1
# i 23 - dve omaske u 325 zapisa blokirale bi ucenje za sve tri stotine. Mereno
# na celoj 2026: prag 100 % pogadja 688 od 921 provizije, a 90 % pogadja 887.
# Ispod 90 % se vise ne dobija (85 % daje 888).
DOVOLJNO_ZA_UCENJE = 3
UDEO_ZA_UCENJE = 0.90

# Vrsta koju app NIKAD ne bira sama: pravo na odbitak zavisi od toga sta je
# kupljeno i za sta se koristi, a to se iz fakture ne vidi. Mereno: Euro petrol
# je 59 puta 21 i 40 puta 1, Sinagoga 18:15 - isti dobavljac, ista vrsta racuna.
TIP_BEZ_ODBITKA = 21

# Da nule nema u produkciji nije znak da je pogresna: ELBS_2026 ima samo 1 (211,
# poslednjih dvadesetak dana) i 2 (8.630, cela godina), jer ljudi knjize odmah po
# unosu. Kalkulacija koju app ostavi na nuli ceka poslovodju, kako i treba.

# Koliko puta se pokusava sa sledecim brojem kad ga neko preotme.
POKUSAJA_ZA_BROJ = 5


def clarion_dan(d: dt.date) -> int:
    return (d - CLARION_NULA).days


def clarion_datum(n: int) -> dt.date:
    return CLARION_NULA + dt.timedelta(days=n)


# Ime pod kojim app upisuje stavke. RBR je jedini trag u ERP-u ko je stavku uneo,
# pa stoji ime aplikacije, ne coveka (odluka: Bane, 21.09.). Kad nesto krene
# naopako, `RBR LIKE 'sefsync%'` odmah kaze sta je app napravila. Ko je pokrenuo
# upis pise kod nas, u `nacrt_kalkulacije.upisao`.
KORISNIK = "sefsync"


def napravi_rbr(korisnik: str, trenutak: dt.datetime | None = None) -> str:
    """RBR = datum(5) + vreme u stotinkama(7) + 4 cifre + korisnicko ime.

    Primer sa stvarne stavke: 8240145806807721dane
                              82401 4580680 7721 dane
    """
    t = trenutak or dt.datetime.now()
    stotinke = (t.hour * 3600 + t.minute * 60 + t.second) * 100 + t.microsecond // 10000
    return f"{clarion_dan(t.date()):05d}{stotinke:07d}{random.randint(0, 9999):04d}{korisnik[:10]}"


def broj_racuna_za_erp(broj: str | None) -> str:
    """BROJ_RN_DOB je varchar(20).

    Mereno: 4.185 od 4.652 brojeva zadrzava crtice i kose crte, a nijedan nije
    duzine 20. Znaci broj se prepisuje kakav jeste; znakovi se izbacuju tek ako
    ne stane, kako ljudi i rade.
    """
    broj = (broj or "").strip()
    if len(broj) <= 20:
        return broj
    zbijen = re.sub(r"[^A-Za-z0-9]", "", broj)
    return zbijen[:20]


class VecPostoji(Exception):
    """Za tu fakturu kalkulacija vec postoji - app ne pravi drugu."""


@dataclass
class Nacrt:
    """Sta bi aplikacija upisala. Ne dira bazu dok se ne pozove `upisi`."""

    document_id: int
    tabela_zaglavlja: str
    tabela_stavki: str
    magacin: str
    zaglavlje: dict
    stavke: list[dict] = field(default_factory=list)
    problemi: list[str] = field(default_factory=list)
    nemapirane: list[dict] = field(default_factory=list)  # stavke bez nase sifre
    # Ono sto ne zaustavlja upis, ali covek treba da pogleda.
    napomene: list[str] = field(default_factory=list)
    pdv_evidencija: list[dict] = field(default_factory=list)  # redovi za PDVEVIDX

    @property
    def spreman(self) -> bool:
        return not self.problemi and not self.nemapirane


# --------------------------------------------------------------------------- #
# citanje iz ERP-a
# --------------------------------------------------------------------------- #


def _analitika_dobavljaca(
    conn: Connection,
    maticni: str | None,
    pib: str | None,
    racun: str | None = None,
) -> str | None:
    """NAZIVI.ANALITIKA - sifra dobavljaca u ERP-u.

    Redosled je izmeren na 198 stvarnih dobavljaca, ne pretpostavljen:

    1. maticni broj (`MATBROJ`) - tacan kad ga ima, ali popunjen je na samo
       3.351 od 44.949 redova, a kod javnih preduzeca SEF ionako salje drugi
       identifikator (Srbijagas: 86132 umesto 20084600). Sam nalazi 66%.
    2. PIB - i to u koloni `ZIRORACUN`, koja uprkos imenu drzi PIB, ne racun
       (Banca Intesa: ZIRORACUN = 100001159). Popunjena je na 37.411 redova i
       podize pokrivenost sa 66% na 97%.
    3. `BPG` - popunjen na svega 22 reda, pa je poslednja nada.

    Preostalih 3% su dobavljaci koji stvarno jos nisu otvoreni u sifarniku.
    """
    if maticni:
        red = conn.execute(
            text("SELECT TOP 1 RTRIM(ANALITIKA) a FROM dbo.NAZIVI WHERE RTRIM(MATBROJ)=:m"),
            {"m": maticni.strip()},
        ).first()
        if red:
            return red.a

    if pib:
        red = conn.execute(
            text(
                "SELECT TOP 1 RTRIM(ANALITIKA) a FROM dbo.NAZIVI "
                "WHERE RTRIM(ISNULL(ZIRORACUN,''))=:p OR RTRIM(ISNULL(BPG,''))=:p"
            ),
            {"p": pib.strip()},
        ).first()
        if red:
            return red.a
    return None


# Reci koje ne razlikuju jednog komitenta od drugog - pravni oblik i opsti deo
# imena javnih preduzeca. Bez njih "JAVNO KOMUNALNO PREDUZECE KOMUNALAC, KULA"
# i "Komunalac JKP" iz Kule postaju isti skup reci.
_PRAVNI_OBLIK = {
    "DOO", "OOD", "AD", "KD", "JKP", "JP", "PR", "SZR", "SUR", "STR", "SZTR",
    "DRUSTVO", "OGRANICENOM", "ODGOVORNOSCU", "JAVNO", "KOMUNALNO", "PREDUZECE",
    "PREDUZETNIK", "ORTACKO", "AKCIONARSKO", "RADNJA",
}


def _reci(tekst: str | None) -> frozenset[str]:
    """Reci po kojima se komitent prepoznaje - bez pravnog oblika i kratkih."""
    return frozenset(
        w for w in normalize(tekst or "").split()
        if len(w) > 2 and w not in _PRAVNI_OBLIK
    )


def predlog_dobavljaca(
    conn: Connection,
    naziv: str | None,
    maticni: str | None,
    pib: str | None,
) -> str | None:
    """Kad se dobavljac ne nadje po brojevima - koji komitent mu najvise lici.

    Ne odlucuje nista i ne upisuje nista: vraca recenicu koja ide uz razlog
    zaustavljanja, da covek vidi gde da gleda.

    Zasto uopste treba: dobavljac ume da **postoji** u sifarniku a da se ne
    nadje. JKP Komunalac Kula je analitika 00709 i ima osam kalkulacija, ali
    SEF za javna preduzeca salje JBKJS (82080) umesto maticnog (08004790), a
    PIB mu je u sifarniku zapisan sa nulom viska (1000662387 umesto
    100662387) - pa oba puta otkazu (01.10.).

    Kljuc je skup reci iz imena, a SEF u ime po pravilu stavi i mesto
    ("...KOMUNALAC, KULA", "BCE GROUP DOO CACAK"), pa se poredi sa imenom i
    sedistem komitenta. Mereno na 45.105 komitenata: 96 % skupova reci vodi na
    tacno jednog. Predlog se daje samo kad je kandidat jedan - inace nista.
    """
    trazene = _reci(naziv)
    if not trazene:
        return None

    # Najduza rec je najprepoznatljivija; suzava pretragu na nekoliko redova
    # umesto citanja celog sifarnika. Poredjenje u ERP-u ne razlikuje ni velika
    # slova ni kvacice, pa "CACAK" nalazi "Čačak".
    najduza = max(trazene, key=len)
    try:
        redovi = conn.execute(
            text(
                "SELECT TOP 200 RTRIM(ANALITIKA) a, RTRIM(NAZIV1) nz, "
                "RTRIM(ISNULL(SEDISTE,'')) mesto, RTRIM(ISNULL(MATBROJ,'')) mb, "
                "RTRIM(ISNULL(ZIRORACUN,'')) zr FROM dbo.NAZIVI "
                "WHERE NAZIV1 LIKE :u"
            ),
            {"u": f"%{najduza}%"},
        ).all()
    except SQLAlchemyError as exc:  # pretraga je pomoc, ne sme da obori nacrt
        log.info("Predlog dobavljača nije izračunat: %s", exc)
        return None

    kandidati = [
        r for r in redovi
        if _reci(r.nz) and _reci(r.nz) <= trazene and _reci(r.mesto) <= trazene
    ]
    if len(kandidati) != 1:
        return None

    k = kandidati[0]
    razlike = []
    if (maticni or "").strip() and k.mb and k.mb != maticni.strip():
        razlike.append(f"matični {k.mb}, a na fakturi {maticni.strip()}")
    if (pib or "").strip() and k.zr and k.zr != pib.strip():
        razlike.append(f"PIB {k.zr}, a na fakturi {pib.strip()}")
    rep = f" — u šifarniku mu stoji {'; '.join(razlike)}" if razlike else ""
    mesto = f", {k.mesto}" if k.mesto else ""
    return f"Možda je {k.a} {k.nz}{mesto}{rep}."


def dinarski_obracun(doc: Document) -> tuple[float | None, float | None]:
    """Osnovica i PDV u dinarima za deviznu fakturu. Cita i iz sacuvanog UBL-a.

    Polja se pune pri uvozu, pa ih dokumenti koji su stigli pre te izmene nemaju
    - a oni su na disku vec od prvog dana. Bez ovoga bi svaka starija devizna
    faktura stala sa "nema dinarskog obracuna", iako obracun stoji u XML-u
    (Banca Intesa KR1384362/26, 06.10.).
    """
    if doc.sum_without_vat_rsd is not None:
        return doc.sum_without_vat_rsd, doc.vat_amount_rsd

    putanja = (doc.ubl_path or "").strip()
    if not putanja:
        return None, None
    try:
        from pathlib import Path

        from ..ubl.parser import parse_ubl

        datoteka = Path(putanja)
        if not datoteka.exists():
            return None, None
        ubl = parse_ubl(datoteka.read_bytes())
    except Exception as exc:  # noqa: BLE001 - citanje fajla ne sme da obori nacrt
        log.info("UBL za #%s nije procitan: %s", doc.id, exc)
        return None, None

    if ubl.taxable_amount_rsd is None:
        return None, None
    osnovica = float(ubl.taxable_amount_rsd)
    pdv = float(ubl.tax_amount_rsd) if ubl.tax_amount_rsd is not None else None
    # Zapise se da se sledeci put ne cita fajl.
    doc.sum_without_vat_rsd = osnovica
    doc.vat_amount_rsd = pdv
    return osnovica, pdv


def vrsta_pdv_evidencije(
    conn: Connection,
    dobavljac: str | None,
    magacin: str,
    *,
    predznak: float,
    ima_pdv: bool,
) -> tuple[int, str | None]:
    """ID PDV evidencije za kalkulaciju. Vraca (vrsta, napomena za coveka).

    Do sada je app u svaku kalkulaciju pisala 1 ("ULAZNI RACUNI"), pa je Bogdan
    rucno ispravljao odobrenja, provizije i racune bez PDV-a.

    Mereno 30.09. na ELBS_2026, 8.939 kalkulacija sa oznakom:

    * **35 Povrat robe** - magacin 010. Na 010 nema nijedne pozitivne
      kalkulacije, a 600 od 603 nosi 35.
    * **34 Finansijska odobrenja** - negativan dokument na 088; svih 368 je
      negativno i bez stavki.
    * racun **sa PDV-om** je 1 ili 21, nikad 75 ni 23 (1.255 : 408 : 14);
    * racun **bez PDV-a** je 75 ili 23, nikad 1 ni 21 (921 : 168 : 2).

    Kod racuna bez PDV-a se bira po dobavljacu, iz onoga sto je covek ranije
    birao. Spisak se tako odrzava sam - sedam banaka (OTP 418, Raiffeisen, NLB,
    Dunav, AIK, PTT, Addiko) nosi vecinu 75.

    **21 se nikad ne bira sam.** Pravo na odbitak zavisi od toga sta je kupljeno
    i za sta se koristi, sto se iz fakture ne vidi: Euro petrol je 59 puta 21 i
    40 puta 1. Kad je dobavljac ranije imao 21, upisuje se 1 uz napomenu.

    Postena simulacija (svaka kalkulacija pogodjena samo iz onih pre nje):
    94,8 % umesto 71,9 % koliko daje danasnje "uvek 1".
    """
    if magacin == MAGACIN_POVRACAJ:
        return TIP_POVRAT_ROBE, None
    # Negativno van 088 se ne javlja: roba se vraca preko 010, a u maloprodaji i
    # na 021 nema nijedne negativne kalkulacije.
    if predznak < 0 and magacin == MAGACIN_ULAZNI_RACUN:
        return TIP_FINANSIJSKO_ODOBRENJE, None

    istorija = (
        conn.execute(
            text(
                f"SELECT {KOLONA_PDV_EVIDENCIJE} vrsta, COUNT(*) n, "
                "  CASE WHEN (ISNULL(PDV,0)+ISNULL(PRENETIPOREZ,0)) = 0 THEN 1 ELSE 0 END bez_pdv "
                "FROM dbo.ULKALKUL "
                f"WHERE RTRIM(DOBAVLJAC) = :d AND {KOLONA_PDV_EVIDENCIJE} <> 0 "
                f"  AND RTRIM(MAGACIN) = '{MAGACIN_ULAZNI_RACUN}' AND IZNOS_ZA_NAP >= 0 "
                f"GROUP BY {KOLONA_PDV_EVIDENCIJE}, "
                "  CASE WHEN (ISNULL(PDV,0)+ISNULL(PRENETIPOREZ,0)) = 0 THEN 1 ELSE 0 END"
            ),
            {"d": dobavljac.strip()},
        ).all()
        if dobavljac
        else []
    )

    if ima_pdv:
        koliko_21 = sum(int(r.n) for r in istorija if int(r.vrsta) == TIP_BEZ_ODBITKA)
        if koliko_21:
            koliko_1 = sum(int(r.n) for r in istorija if int(r.vrsta) == TIP_ULAZNI_RACUN)
            return TIP_ULAZNI_RACUN, (
                f"kod ovog dobavljača je ranije {koliko_21} puta biran 21 "
                f"(bez prava na odbitak PDV-a), a {koliko_1} puta 1 — "
                "proveri ID PDV evidencije pre knjiženja"
            )
        return TIP_ULAZNI_RACUN, None

    # Bez PDV-a: provizija/taksa (75) ili racun bez EPPDV (23). Uzima se ono sto
    # je dobavljac ubedljivo najcesce bio, i tek kad se ponovilo dovoljno puta.
    bez_pdv = {int(r.vrsta): int(r.n) for r in istorija if int(r.bez_pdv) == 1}
    ukupno = sum(bez_pdv.values())
    if ukupno:
        vrsta, koliko = max(bez_pdv.items(), key=lambda x: x[1])
        if koliko >= DOVOLJNO_ZA_UCENJE and koliko / ukupno >= UDEO_ZA_UCENJE:
            return vrsta, None
    return TIP_BEZ_EPPDV, None


def stanje_kalkulacije(conn: Connection, tabela: str, magacin: str, broj: str) -> dict | None:
    """PROKNJIZENO i stavke kalkulacije koja je vec u ERP-u. Samo cita.

    Treba kad se sifra artikla ispravi POSLE upisa: app u ERP-u ne sme nista da
    menja (ima samo INSERT), pa coveku mora da kaze i sta da ispravi i koliko ga
    to kosta - a to zavisi od toga da li je kalkulacija zaknjizena.
    """
    red = conn.execute(
        text(f"SELECT TOP 1 PROKNJIZENO p FROM dbo.{tabela} "
             "WHERE RTRIM(MAGACIN)=:m AND RTRIM(BROJ)=:b"),
        {"m": magacin, "b": broj},
    ).first()
    if red is None:
        return None
    stavke = "MPRULK" if tabela == "MKALKUL" else "PRULKALK"
    return {
        "proknjizeno": int(red.p or 0),
        "stavke": [
            dict(r._mapping)
            for r in conn.execute(
                text(f"SELECT RTRIM(ARTIKAL) artikal, RTRIM(NAZIV) naziv, KOLICINA kolicina "
                     f"FROM dbo.{stavke} WHERE RTRIM(MAGACIN)=:m AND RTRIM(BROJ)=:b"),
                {"m": magacin, "b": broj},
            )
        ],
    }


def _artikal(conn: Connection, sifra: str) -> dict | None:
    red = conn.execute(
        text(
            "SELECT TOP 1 RTRIM(ARTIKAL) artikal, RTRIM(NAZIV) naziv, RTRIM(ISNULL(JEDMERE,'')) jm, "
            "RTRIM(ISNULL(TARIFA,'002')) tarifa FROM dbo.ARTIKLI "
            "WHERE RTRIM(ARTIKAL)=:a AND RTRIM(SIFRAMAG)='099'"
        ),
        {"a": sifra},
    ).first()
    return dict(red._mapping) if red else None


def _stopa(conn: Connection, tarifa: str) -> float:
    red = conn.execute(
        text("SELECT TOP 1 STOPA FROM dbo.PTARIFE WHERE RTRIM(TARIFA)=:t"), {"t": tarifa}
    ).first()
    return float(red[0]) if red and red[0] is not None else 20.0


def _maloprodajna_cena(conn: Connection, artikal: str, magacin: str) -> float | None:
    """Mereno: MALACENA se u 80% poklapa sa tekucom cenom objekta u ARTPROD."""
    red = conn.execute(
        text(
            "SELECT TOP 1 CENAMALO FROM dbo.ARTPROD "
            "WHERE RTRIM(ARTIKAL)=:a AND RTRIM(SIFRAPRO)=:m"
        ),
        {"a": artikal, "m": magacin},
    ).first()
    return float(red[0]) if red and red[0] is not None else None


def sledeci_broj(conn: Connection, tabela: str, magacin: str) -> str:
    red = conn.execute(
        text(f"SELECT MAX(CAST(RTRIM(BROJ) AS INT)) FROM dbo.{tabela} WHERE RTRIM(MAGACIN)=:m"),
        {"m": magacin},
    ).scalar()
    return f"{(int(red or 0) + 1):05d}"


# --------------------------------------------------------------------------- #
# mapiranje artikala
# --------------------------------------------------------------------------- #


# Koliko razlicitih naziva mora da stoji pod istom "sifrom dobavljaca" da bi se
# ona proglasila oznakom, a ne sifrom artikla.
#
# Mereno 02.10. na 28.310 stavki: od 553 para (dobavljac + sifra) koji se
# javljaju na odobrenjima, prag "vise od dva naziva" izdvaja 13 sifara i 425
# stavki - a nijedna od njih danas nema vezu, pa se nicemu sto radi ne staje na
# put. Prag "vise od jednog" bi dirao 35 postojecih veza i to bez potrebe.
OZNAKA_OD_NAZIVA = 3


def _sifra_je_oznaka(session, pib: str, sifra: str) -> bool:
    """Da li dobavljac tom "sifrom" oznacava vise razlicitih stvari.

    Na knjiznom odobrenju sifra dobavljaca cesto nije sifra artikla nego
    nalepnica za vrstu dokumenta: Eurotehna salje `Porez 20%` na svih 107
    odobrenja i pod njom stoji 100 razlicitih naziva, EWE `USL00027` na 109
    (13 naziva), Comtrade `R0001` na 90 (27 naziva).

    Jedna jedina veza za takvu "sifru" stampa isti artikal na svako sledece
    odobrenje tog dobavljaca. Tako je 661FN-2026 dobilo frizider sa 613FN-2026,
    iako u napomeni pise drugi model - a predlog iz napomene, koji pogadja
    9 od 9, nije ni racunat, jer se racuna tek kad stavka ostane bez sifre.

    Obrnuto ne vazi: naziv koji se ponavlja je sasvim normalan ("LED TV
    55PUS7810 PHILIPS" stoji na 45 dokumenata i to je jedan artikal), pa se
    grana po nazivu ne dira.
    """
    nazivi = {
        normalize(naziv or "")
        for (naziv,) in session.execute(
            select(DocumentLine.name)
            .join(Document, Document.id == DocumentLine.document_id)
            .where(Document.supplier_vat == pib,
                   DocumentLine.sellers_item_id == sifra)
            .distinct()
        )
        if naziv
    }
    return len(nazivi) >= OZNAKA_OD_NAZIVA


@dataclass
class Veze:
    """Sifarnik veza, procitan jednom. Vidi `veze_dobavljaca`."""

    po_sifri: dict[tuple[str, str], str] = field(default_factory=dict)
    po_barkodu: dict[str, set[str]] = field(default_factory=dict)
    po_imenu: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    # (pib, sifra) -> da li je to oznaka vrste dokumenta, a ne sifra artikla
    oznake: dict[tuple[str, str], bool] = field(default_factory=dict)


_KES_VEZA: tuple[float, Veze] | None = None
KES_VEZA_S = 120.0


def zaboravi_veze() -> None:
    """Posle svake izmene u sifarniku veza - da se promena odmah vidi."""
    global _KES_VEZA
    _KES_VEZA = None


def veze_dobavljaca(session, *, osvezi_kes: bool = False) -> Veze:
    """Sve aktivne veze, procitane jednom i drzane u pameti.

    Ranije je `_nasa_sifra` za **svaku stavku** radila do cetiri upita, a jedan
    od njih je ucitavao sve veze tog dobavljaca. Na `nacrti --osvezi` to je
    bilo desetine hiljada upita i MS SQL je prijavljivao upite od sedam sekundi
    (Bane, 06.10.: "vrti ga bukvalno pola sata"). Veza ima oko 2.100 - sve
    staju u pamet i citaju se jednom.

    Kes se gasi pri svakoj izmeni veze (`zaboravi_veze`), pa povezivanje i
    dalje vazi odmah.
    """
    global _KES_VEZA
    sada = time.monotonic()
    if not osvezi_kes and _KES_VEZA and sada - _KES_VEZA[0] < KES_VEZA_S:
        return _KES_VEZA[1]

    veze = Veze()
    for pib, sifra, barkod, naziv, erp in session.execute(
        select(
            ItemMapping.supplier_vat,
            ItemMapping.supplier_item_id,
            ItemMapping.barcode,
            ItemMapping.supplier_item_name,
            ItemMapping.erp_item_code,
        ).where(ItemMapping.active == True)  # noqa: E712 - MS SQL nema IS TRUE
    ):
        kod = (erp or "").strip()
        if not kod:
            continue
        if pib and (sifra or "").strip():
            veze.po_sifri[(pib, sifra.strip())] = kod
        if (barkod or "").strip():
            veze.po_barkodu.setdefault(barkod.strip(), set()).add(kod)
        if pib and (naziv or "").strip():
            veze.po_imenu.setdefault((pib, normalize(naziv)), set()).add(kod)

    _KES_VEZA = (sada, veze)
    return veze


def _nasa_sifra(
    session,
    conn: Connection,
    pib: str | None,
    linija: DocumentLine,
    *,
    odobrenje: bool = False,
    veze: Veze | None = None,
) -> tuple[str | None, str]:
    """Vraca (sifra, odakle). Redosled po pouzdanosti - vidi docs/FAZA-2.md."""
    veze = veze if veze is not None else veze_dobavljaca(session)

    # Covek je sifru rekao bas za OVU stavku. To je pouzdanije od svake veze i
    # vredi i kad se veza ne pamti za ubuduce.
    #
    # Bez ovoga je povezivanje na knjiznom odobrenju bilo bez ikakvog dejstva:
    # tamo se veza pamti samo kad covek cekira "zapamti vezu", a kad ne cekira
    # nije nastajalo nista - sifra se upisivala na stavku koju niko nije citao
    # (Ilijana, Vivax 90911 na KIM-TEC odobrenju, 01.10.).
    if (linija.erp_mapping_source or "") == "rucno" and (linija.erp_item_code or "").strip():
        return linija.erp_item_code.strip(), "ručno"

    sifra_dob = (linija.sellers_item_id or "").strip()
    if pib and sifra_dob:
        oznaka = False
        if odobrenje:
            kljuc = (pib, sifra_dob)
            if kljuc not in veze.oznake:
                veze.oznake[kljuc] = _sifra_je_oznaka(session, pib, sifra_dob)
            oznaka = veze.oznake[kljuc]
        if not oznaka:
            kod = veze.po_sifri.get((pib, sifra_dob))
            if kod:
                return kod, "mapiranje"

    # Veza zapamcena po barkodu vazi i kad dobavljac promeni svoju sifru, i kad
    # isti artikal stigne od drugog dobavljaca. Mereno na 2.110 aktivnih veza:
    # 1.872 ima barkod i nijedan ne vodi na dve sifre - pa je trazenje
    # jednoznacno. Ako se ikad pojavi dvosmislen, ne odlucuje se.
    barkod = (linija.standard_item_id or "").strip()
    if barkod:
        kodovi = veze.po_barkodu.get(barkod, set())
        if len(kodovi) == 1:
            return next(iter(kodovi)), "mapiranje po barkodu"
        if len(kodovi) > 1:
            return None, "barkod je ranije povezan sa više šifara"

        redovi = conn.execute(
            text(
                "SELECT DISTINCT RTRIM(ARTIKAL) a FROM dbo.ARTIKLI "
                "WHERE RTRIM(ISNULL(BARCODE,''))=:b AND RTRIM(SIFRAMAG)='099'"
            ),
            {"b": barkod},
        ).all()
        if len(redovi) == 1:
            return redovi[0].a, "barkod"
        if len(redovi) > 1:
            return None, "barkod vodi na vise sifara"

    # Poslednja nada: isti dobavljac, isti naziv. Neki dobavljaci ne salju ni
    # svoju sifru ni barkod (Vitel: „LEXA BT ZVUCNIK LX-302 NITRO“), pa veza
    # nema za sta drugo da se zakaci.
    #
    # Mereno na 2.110 veza: 2.057 razlicitih parova (dobavljac + naziv), od
    # kojih 24 vode na vise sifara - svi od dobavljaca koji salje opste nazive
    # tipa „MASINA ZA PRANJE VESA“. Zato se odlucuje samo kad je pogodak jedan.
    if pib and (linija.name or "").strip():
        kodovi = veze.po_imenu.get((pib, normalize(linija.name)), set())
        if len(kodovi) == 1:
            return next(iter(kodovi)), "mapiranje po nazivu"
        if len(kodovi) > 1:
            return None, "isti naziv je ranije povezan sa više šifara"

    return None, "nepoznat"


# --------------------------------------------------------------------------- #
# gradjenje nacrta
# --------------------------------------------------------------------------- #


def pripremi(
    document_id: int,
    korisnik: str = KORISNIK,
    *,
    datum: dt.date | None = None,
    engine: Engine | None = None,
) -> Nacrt:
    # Avans se odbija pre nego sto se ERP uopste dodirne. Mereno 18.09.: nijedan
    # od 39 avansa u istoriji nema kalkulaciju - da je app napravi, nastao bi
    # dokument koji u knjigama ne postoji.
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is not None and doc.document_type is DocumentType.PREPAYMENT:
            return Nacrt(
                document_id, "", "", "", {},
                problemi=["Avansni račun ne ide kroz kalkulaciju."],
            )

    eng = engine or get_erp_engine()
    danas = datum or dt.date.today()
    dan = clarion_dan(danas)

    with session_scope() as session, eng.connect() as conn:
        doc = session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"Dokument #{document_id} ne postoji.")
        jedinica = doc.business_unit
        if jedinica is None:
            return Nacrt(document_id, "", "", "", {}, problemi=["Dokument nije razvrstan."])

        maloprodaja = jedinica.kind is UnitKind.RETAIL
        usluga = jedinica.kind not in (UnitKind.RETAIL, UnitKind.WAREHOUSE)
        magacin = (jedinica.erp_code or jedinica.code).strip()
        if usluga:
            magacin = MAGACIN_ULAZNI_RACUN

        tabela_z = "MKALKUL" if maloprodaja else "ULKALKUL"
        tabela_s = "MPRULK" if maloprodaja else "PRULKALK"

        nacrt = Nacrt(document_id, tabela_z, tabela_s, magacin, {})

        dobavljac = _analitika_dobavljaca(
            conn, doc.supplier_reg_no, doc.supplier_vat, doc.payment_account
        )
        if dobavljac is None:
            razlog = (
                f"Dobavljač {doc.supplier_name} (MB {doc.supplier_reg_no}, "
                f"PIB {doc.supplier_vat or '-'}) ne postoji u NAZIVI."
            )
            predlog_dob = predlog_dobavljaca(
                conn, doc.supplier_name, doc.supplier_reg_no, doc.supplier_vat
            )
            if predlog_dob:
                razlog = f"{razlog} {predlog_dob}"
            nacrt.problemi.append(razlog)

        broj_racuna = broj_racuna_za_erp(doc.document_number)
        broj = sledeci_broj(conn, tabela_z, magacin)
        # Knjizno odobrenje ulazi minusom - kolicine i iznosi su negativni.
        predznak = -1.0 if (doc.document_type and "credit" in str(doc.document_type.value).lower()) else 1.0

        # Faktura u stranoj valuti: u ERP ide u dinarima.
        #
        # Mereno 05.10.: ERP sve takve drzi preracunate (DEVIZNO=0, KURS=0), a
        # svih 15 deviznih faktura od 01.07. nosi dinarski obracun u sebi -
        # drugi blok `TaxTotal` sa `currencyID="RSD"`. Bez ovoga je 1.350,60 EUR
        # odlazilo u ERP kao 1.350,60 dinara (Raiffeisen K110/2026-3087535).
        kurs = 1.0
        pdv_rsd = None
        valuta = (doc.currency or "RSD").strip().upper()
        if valuta and valuta != "RSD":
            osnovica_rsd, pdv_rsd = dinarski_obracun(doc)
            osnovica_val = doc.sum_without_vat or doc.amount
            if osnovica_rsd and osnovica_val:
                kurs = float(osnovica_rsd) / float(osnovica_val)
                nacrt.napomene.append(
                    f"Faktura je u {valuta} — preračunato u dinare po kursu "
                    f"{kurs:.4f} sa same fakture."
                )
            else:
                nacrt.problemi.append(
                    f"Faktura je u {valuta}, a nema dinarski obračun — "
                    "iznos bi otišao u ERP kao da su dinari. Unesi je ručno."
                )

        # Kalkulacija na magacinu 088 nema stavke. Nije stvar izbora nego pravilo
        # ERP-a: mereno 16.09., svih 3.016 zaglavlja na 088 nema nijedan red u
        # PRULKALK, a ROBNO_ZADUZ je u svima 0. Bez ovoga generator trazi sifru
        # artikla za "Naknada za transakcije na POS terminalu" - a takva sifra
        # nikad nece ni postojati, pa bi kalkulacija zauvek cekala coveka.
        bez_stavki = magacin == MAGACIN_ULAZNI_RACUN
        veze = veze_dobavljaca(session)

        linije = (
            []
            if bez_stavki
            else session.query(DocumentLine)
            .filter(DocumentLine.document_id == document_id)
            .order_by(DocumentLine.line_no)
            .all()
        )

        # Datum prometa je ono sto ERP ocekuje. Mereno 22.09. na 3.401 dokumentu
        # gde se datum fakture i datum prometa razlikuju: DATUM je promet u 90 %,
        # DAT_RAC_DOB u 91 %, DATUM_PRIJEM u 51 % (u 43 % nula). Stavka nosi isti
        # datum kao zaglavlje (2.066 od 2.068, odnosno 20.991 od 21.032).
        # Ranije je app pisala danasnji datum, pa je na ekranu ispadao datum
        # slanja umesto prometa.
        promet = doc.delivery_date or doc.issue_date
        dan_prometa = clarion_dan(promet) if promet else dan

        osnovica = pdv_ukupno = mp_vrednost = mp_pdv = 0.0
        # Zbirovi po poreskoj stopi. ERP ih drzi u odvojenim poljima: PDV je
        # opsta stopa (20 %), PRENETIPOREZ posebna (10 %), a u maloprodaji jos i
        # MPVRED1/UKAL_PDV1 za 10 % naspram MPVRED2/UKAL_PDV2 za 20 %. Mereno na
        # 071/00033: PRENETIPOREZ 207,18 je tacno 10 % od osnovice po toj stopi.
        # Mesanih kalkulacija je malo (4 u istoriji, sve u pabu), ali faktura sa
        # 0/10/20 % postoji, pa zbir po jednoj stopi ne bi bio tacan.
        po_stopi: dict[float, dict[str, float]] = {}

        def _dodaj(stopa_: float, osn: float, porez: float, mp_vr: float = 0.0, mp_pd: float = 0.0):
            red = po_stopi.setdefault(stopa_, {"osnovica": 0.0, "pdv": 0.0, "mp": 0.0, "mp_pdv": 0.0})
            red["osnovica"] += osn
            red["pdv"] += porez
            red["mp"] += mp_vr
            red["mp_pdv"] += mp_pd

        trenutak = dt.datetime.now()

        # Knjizno odobrenje koje ne navodi artikal nego vezu ka saglasnosti:
        # artikal se trazi po broju fakture iz napomene. Racuna se jednom za ceo
        # dokument, jer se napomena odnosi na dokument a ne na pojedinu stavku.
        predlog = None
        if doc.document_type is DocumentType.CREDIT_NOTE:
            from ..services.odobrenja import predlog_iz_napomene

            try:
                predlog = predlog_iz_napomene(
                    conn,
                    napomena=doc.note,
                    dobavljac=dobavljac,
                    osnovica=doc.sum_without_vat,
                )
            except SQLAlchemyError as exc:  # stare godine ume da nema
                log.info("Predlog iz napomene za #%s nije izracunat: %s", document_id, exc)

        for linija in linije:
            if linija.nije_artikal:
                # Covek je rekao da to nije artikal - ne trazi se sifra i ne ulazi
                # u kalkulaciju. Vidi se na strani dokumenta, uz stavku.
                continue
            sifra, odakle = _nasa_sifra(
                session, conn, doc.supplier_vat, linija,
                odobrenje=predznak < 0, veze=veze,
            )
            if sifra is None:
                # Ne recenica nego podaci: ekran na kom se otvara sifra treba
                # tacno ovo - naziv, barkod, sifru dobavljaca, kolicinu i cenu.
                nacrt.nemapirane.append(
                    {
                        "line_no": linija.line_no,
                        "naziv": linija.name or "",
                        "sifra_dobavljaca": linija.sellers_item_id or "",
                        "ean": linija.standard_item_id or "",
                        "kolicina": linija.quantity,
                        "cena": linija.price,
                        "jedinica": linija.unit_code or "",
                        "predlog": predlog,
                        "razlog": odakle if odakle != "nepoznat" else "",
                    }
                )
                continue
            art = _artikal(conn, sifra)
            if art is None:
                # Sifra postoji na stavci, ali je u sifarniku nema - omaska u
                # kucanju (87622 umesto 87621) ili artikal koji nikad nije
                # otvoren. To nije zastoj nego stavka bez upotrebljive sifre:
                # pod "zaustavljeno" se forma za povezivanje ne nudi, pa se
                # faktura nije imala kako odglaviti (3753VP-2026, 03.10.).
                nacrt.nemapirane.append(
                    {
                        "line_no": linija.line_no,
                        "naziv": linija.name or "",
                        "sifra_dobavljaca": linija.sellers_item_id or "",
                        "ean": linija.standard_item_id or "",
                        "kolicina": linija.quantity,
                        "cena": linija.price,
                        "jedinica": linija.unit_code or "",
                        "predlog": predlog,
                        "razlog": f"šifra {sifra} ne postoji u šifarniku",
                    }
                )
                continue

            tarifa = (art["tarifa"] or "002").strip()
            stopa = _stopa(conn, tarifa)
            kolicina = float(linija.quantity or 0) * predznak
            kol_abs = abs(kolicina) or 1.0

            # Mereno na 4.425 stavki sa rabatom: FAK_CENA je bruto cena,
            # NAB_CENA neto, IZN_RABATA rabat po komadu, VREDNOST neto x kolicina.
            bruto = float(linija.price or 0) * kurs
            neto = (
                round(float(linija.line_amount) * kurs / kol_abs, 4)
                if linija.line_amount is not None and kol_abs
                else bruto
            )
            rabat_po_komadu = round(bruto - neto, 4)
            rabat_proc = round(rabat_po_komadu / bruto * 100, 4) if bruto else 0.0
            vrednost = round(neto * kolicina, 2)
            pdv = round(vrednost * stopa / 100.0, 3)

            stavka = {
                "RBR": napravi_rbr(korisnik, trenutak + dt.timedelta(milliseconds=10 * len(nacrt.stavke))),
                "MAGACIN": magacin,
                "VRSTAKNJIZ": "U",
                "BROJ": broj,
                "ARTIKAL": art["artikal"],
                "NAZIV": art["naziv"],
                "DATUM": dan_prometa,
                "OPIS": broj_racuna[:15],
                "TARIFA": tarifa.zfill(5),
                "STOPA": stopa,
                "SINTPOR": SINTPOR_PO_STOPI.get(stopa, "270"),
                "PDV": pdv,
                "KOLICINA": kolicina,
                "JMR": (art["jm"] or "kom")[:3],
                "FAK_CENA": bruto,
                "RABAT_PROC": rabat_proc,
                "IZN_RABATA": rabat_po_komadu,
                "VREDNOST": vrednost,
                "NAB_CENA": neto,
                "ROB_CENA": neto,
                "IZNOS": vrednost,
                "DOBAV": dobavljac or "",
                # Tip poreske evidencije stavke. Popunjava se posle petlje,
                # kad se zna ima li racun PDV-a: zaglavlje i stavke u ERP-u
                # uvek nose istu vrstu.
                "IDPDV": TIP_ULAZNI_RACUN,
                "_izvor_sifre": odakle,
            }

            if maloprodaja:
                mp = _maloprodajna_cena(conn, art["artikal"], magacin)
                if mp:
                    stavka["MALACENA"] = mp
                    mp_bez_pdv = mp / (1 + stopa / 100.0)
                    marza = mp_bez_pdv - neto
                    stavka["RUCM_PROC"] = round(marza / neto * 100, 6) if neto else 0.0
                    stavka["IZN_RUCM"] = round(marza * abs(kolicina), 2)
                    stavka["IZN_MPP"] = round((mp - mp_bez_pdv) * abs(kolicina), 2)
                    mp_vrednost += round(mp * abs(kolicina), 2)
                    mp_pdv += stavka["IZN_MPP"]
                else:
                    nacrt.problemi.append(
                        f"stavka {linija.line_no}: artikal {art['artikal']} nema cenu u objektu {magacin}"
                    )

            osnovica += vrednost
            pdv_ukupno += pdv
            _dodaj(
                stopa,
                vrednost,
                pdv,
                round(mp * abs(kolicina), 2) if maloprodaja and mp else 0.0,
                stavka.get("IZN_MPP", 0.0) if maloprodaja else 0.0,
            )
            nacrt.stavke.append(stavka)

        if bez_stavki:
            # Iznosi dolaze iz zaglavlja fakture, jer stavaka nema. Podela po
            # stopama se ipak racuna iz stavki dokumenta - usluzna faktura ume da
            # nosi i 10 % i 20 % (struja, komunalije), a ERP to drzi u odvojenim
            # poljima.
            pdv_ukupno = float(
                pdv_rsd if kurs != 1.0 and pdv_rsd is not None
                else (doc.vat_amount or 0)
            ) * predznak
            za_naplatu = round(float(doc.amount or 0) * kurs * predznak, 2)
            for linija in doc.lines:
                if linija.vat_percent is None or linija.line_amount is None:
                    continue
                osn = round(float(linija.line_amount) * kurs * predznak, 2)
                _dodaj(float(linija.vat_percent), osn,
                       round(osn * float(linija.vat_percent) / 100.0, 2))
            if not po_stopi:
                _dodaj(20.0, 0.0, pdv_ukupno)
        else:
            za_naplatu = round(osnovica + pdv_ukupno, 2)

        # ERP razdvaja stope: PDV je opsta (20 %), PRENETIPOREZ posebna (10 %).
        def _po(stopa_: float, kljuc: str) -> float:
            return round(po_stopi.get(stopa_, {}).get(kljuc, 0.0), 2)

        pdv_opsta = _po(20.0, "pdv")
        pdv_posebna = _po(10.0, "pdv")
        if bez_stavki and not pdv_opsta and not pdv_posebna:
            pdv_opsta = round(pdv_ukupno, 2)

        # Deo racuna koji ne nosi PDV ide u posebnu evidenciju. Mereno na EPS
        # racunu KP101016682899: stavka "JMS taksa" 349,00 nosi kategoriju OE
        # (nije predmet PDV-a), a ERP ju je na 088/03039 upisao kao red TIP 23,
        # dok je ostatak 46.961,11 = 39.134,26 + 7.826,85 upisan kao TIP 1.
        #
        # Pise se samo prava podela - racun koji je ceo oporeziv (2.988 od
        # 3.075) nema red u PDVEVIDX i ERP mu evidenciju izvede iz zaglavlja.
        # Racun koji bi bio ceo neoporeziv se ne dira: takav u istoriji ne
        # postoji, pa nema po cemu da se proveri sta bi ERP ocekivao.
        if bez_stavki:
            neoporezivo = _po(0.0, "osnovica")
            oporezivo = round(_po(10.0, "osnovica") + _po(20.0, "osnovica"), 2)
            if neoporezivo and oporezivo:
                nacrt.pdv_evidencija = [
                    {
                        "REDBR": 1,
                        "TIP": TIP_ULAZNI_RACUN,
                        "IZNOS": round(oporezivo + pdv_posebna + pdv_opsta, 2),
                        "OSNOVA1": _po(10.0, "osnovica"),
                        "PDV1": pdv_posebna,
                        "OSNOVA2": _po(20.0, "osnovica"),
                        "PDV2": pdv_opsta,
                    },
                    {
                        "REDBR": 2,
                        "TIP": TIP_BEZ_EPPDV,
                        "IZNOS": neoporezivo,
                        "OSNOVA1": 0.0,
                        "PDV1": 0.0,
                        "OSNOVA2": 0.0,
                        "PDV2": 0.0,
                    },
                ]

        # Rok placanja. Knjizno odobrenje ga po pravilu nema, pa je app pisala
        # nulu - a takvog zapisa u ERP-u nema nijednog: od 8.863 kalkulacija
        # VALUTA je nula nula puta. Na 010 je jednaka datumu u 584 od 587, na
        # 021 i u maloprodaji je kasnije. Kad roka nema, stoji datum prometa.
        rok = clarion_dan(doc.due_date) if doc.due_date else dan_prometa

        # ID PDV evidencije. Kolona se zove LASTUSER - ime vara, ali je tako.
        vrsta, napomena = vrsta_pdv_evidencije(
            conn,
            dobavljac,
            magacin,
            predznak=predznak,
            ima_pdv=abs(round(pdv_opsta + pdv_posebna, 2)) > 0,
        )
        if napomena:
            nacrt.napomene.append(napomena)
        for stavka in nacrt.stavke:
            stavka["IDPDV"] = vrsta

        nacrt.zaglavlje = {
            KOLONA_PDV_EVIDENCIJE: vrsta,
            "MAGACIN": magacin,
            "VRSTAKNJIZ": "U",
            "BROJ": broj,
            "DATUM": dan_prometa,
            "KONTO": "435",
            "DOBAVLJAC": dobavljac or "",
            "BROJ_RN_DOB": broj_racuna,
            "DAT_RAC_DOB": dan_prometa,
            # Datum prijema robe. Kod usluga prijema nema - na svih 3.076
            # zaglavlja na 088 je nula, dok je na 010, 021 i u maloprodaji
            # popunjen bez izuzetka (i skoro uvek jednak DATUM-u).
            "DATUM_PRIJEM": 0 if bez_stavki else dan_prometa,
            "ROBNO_ZADUZ": round(osnovica, 2),
            "PDV": pdv_opsta,
            "PRENETIPOREZ": pdv_posebna,
            "IZNOS_ZA_NAP": za_naplatu,
            # "Da" znaci da kalkulacija formira cenu. Mereno: tako je svuda gde
            # ima robe - maloprodaja 4.809, 010 587, 021 285 zapisa - a prazno
            # je samo na uslugama (088, 3.076). Ranije je app gledala da li je
            # maloprodaja, pa je veleprodaji pisala prazno.
            "MENJA_CENU": "" if bez_stavki else "Da",
            "OBRACUN_MALO": "",
            "MODEL": 0,
            "VALUTA": rok,
            "PROKNJIZENO": PROKNJIZENO_NIJE,
            "OZNAKA": "EUR",
            "TM": "000000",
            "IZVOREPP": IZVOR,
            # Mereno: u MKALKUL je polje uvek prazno, u ULKALKUL je "Da" kad ima
            # stavki (010, 021) i "Ne" na 088. Prazno se u ULKALKUL ne javlja.
            "STAVKE": "" if maloprodaja else ("Da" if nacrt.stavke else "Ne"),
        }
        if maloprodaja:
            nacrt.zaglavlje["IZN_RUCM"] = round(mp_vrednost - mp_pdv - osnovica, 2)
            # 1 = posebna stopa, 2 = opsta. Mereno na 071/00033: MPVRED1 2.000,00
            # i UKAL_PDV1 181,82 (= 2.000/11) idu uz 10 %, a MPVRED2/UKAL_PDV2 uz 20 %.
            nacrt.zaglavlje["UKAL_PDV1"] = _po(10.0, "mp_pdv")
            nacrt.zaglavlje["MPVRED1"] = _po(10.0, "mp")
            nacrt.zaglavlje["UKAL_PDV2"] = _po(20.0, "mp_pdv")
            nacrt.zaglavlje["MPVRED2"] = _po(20.0, "mp")

        return nacrt


# --------------------------------------------------------------------------- #
# ispis i upis
# --------------------------------------------------------------------------- #


def dopuni_prazna(conn: Connection, tabela: str, red: dict) -> dict:
    """Popunjava kolone koje nismo racunali - nulom ili praznim stringom.

    ERP je Clarion aplikacija i nigde ne ostavlja NULL: u uzorku od 200 zapisa
    na 021 svih 78 kolona koje app ne dira ima 0 odnosno prazan string. Nas
    INSERT je nabrajao samo svoja polja, pa je ostatak ostajao NULL - zapis koji
    se po tome razlikuje od svih ostalih u bazi.
    """
    if tabela not in _KOLONE:
        _KOLONE[tabela] = {
            r.COLUMN_NAME: r.DATA_TYPE
            for r in conn.execute(
                text(
                    "select COLUMN_NAME, DATA_TYPE from INFORMATION_SCHEMA.COLUMNS "
                    "where TABLE_NAME = :t"
                ),
                {"t": tabela},
            )
        }
    dopunjen = dict(red)
    for kolona, tip in _KOLONE[tabela].items():
        if kolona in dopunjen:
            continue
        if tip in TIPOVI_BROJEVI:
            dopunjen[kolona] = 0
        elif tip in TIPOVI_TEKST:
            dopunjen[kolona] = ""
    return dopunjen


def _vrednost(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


def kao_sql(nacrt: Nacrt) -> str:
    """Tacno ono sto bi se izvrsilo - za pregled pre upisa."""
    if not nacrt.zaglavlje:
        return "-- nema šta da se upiše"
    redovi = []
    kolone = list(nacrt.zaglavlje)
    redovi.append(
        f"INSERT INTO dbo.{nacrt.tabela_zaglavlja} ({', '.join(kolone)})\n"
        f"VALUES ({', '.join(_vrednost(nacrt.zaglavlje[k]) for k in kolone)});"
    )
    for s in nacrt.stavke:
        vidljive = {k: v for k, v in s.items() if not k.startswith("_")}
        kolone = list(vidljive)
        redovi.append(
            f"INSERT INTO dbo.{nacrt.tabela_stavki} ({', '.join(kolone)})\n"
            f"VALUES ({', '.join(_vrednost(vidljive[k]) for k in kolone)});"
        )
    return "\n".join(redovi)


@dataclass(frozen=True)
class Pogodak:
    """Kalkulacija u ERP-u za koju mislimo da je ista faktura.

    `oznaka` je kratka jer ide u `nacrt_kalkulacije.erp_broj`, koji je
    `varchar(20)`; objasnjenje stoji odvojeno u `razlog`, inace bi na MS SQL-u
    upis pukao na duzini.

    `siguran` deli dve vrste nalaza. Siguran je onaj gde se broj racuna poklapa
    (u celosti, ili je u ERP-u odsecen na 15 znakova) - njega app sme sama da
    skine sa ekrana. Nesiguran je predlog po iznosu i datumu: njega odlucuje
    covek, jer lazan pogodak tiho skloni fakturu koju onda niko ne zaknjizi.
    """

    oznaka: str
    razlog: str = ""
    siguran: bool = True

    def __str__(self) -> str:
        return f"{self.oznaka} ({self.razlog})" if self.razlog else self.oznaka


def _rastojanje(a: str, b: str) -> int:
    """Levenstajn - koliko znakova treba promeniti da jedan broj postane drugi."""
    prethodni = list(range(len(b) + 1))
    for i, za in enumerate(a, 1):
        tekuci = [i]
        for j, zb in enumerate(b, 1):
            tekuci.append(
                min(prethodni[j] + 1, tekuci[j - 1] + 1, prethodni[j - 1] + (za != zb))
            )
        prethodni = tekuci
    return prethodni[-1]


def _najduzi_zajednicki(a: str, b: str) -> int:
    """Najduzi niz znakova koji stoji u oba broja."""
    if not a or not b:
        return 0
    prosli = [0] * (len(b) + 1)
    najduzi = 0
    for za in a:
        tekuci = [0] * (len(b) + 1)
        for j, zb in enumerate(b, 1):
            if za == zb:
                tekuci[j] = prosli[j - 1] + 1
                najduzi = max(najduzi, tekuci[j])
        prosli = tekuci
    return najduzi


def ocena_broja(nas: str, u_erpu: str) -> str:
    """Koliko su dva broja racuna slicna, recima kojima covek moze da veruje.

    Mereno na 47 predloga sa ekrana: razlike skoro nikad nisu greske u kucanju
    nego skraceni zapis istog broja - METRO salje `0/0(044)0002/143059/26`, a u
    ERP-u stoji `143059/26`. Zato prvo ide provera sadrzanosti, pa tek onda
    rastojanje. Ocena nista ne odlucuje, samo pomaze coveku da brze vidi.
    """
    from ..services.mapiranje import kljuc_racuna

    a, b = kljuc_racuna(nas), kljuc_racuna(u_erpu)
    if not a or not b:
        return "broj se ne može uporediti"
    if a == b:
        return "isti broj"
    if a in b or b in a:
        return "isti broj, kraće zapisan"
    razlika = _rastojanje(a, b)
    if razlika <= 2:
        return f"razlika u {'jednom znaku' if razlika == 1 else 'dva znaka'}"
    zajednicko = _najduzi_zajednicki(a, b)
    if zajednicko >= 5 and zajednicko >= min(len(a), len(b)) / 2:
        return f"poklapa se {zajednicko} znakova"
    return "broj se ne poklapa"


# Vrednosti koje u sifarniku stoje kao popuna, pa ne identifikuju nikoga.
PRAZNI_IDENTIFIKATORI = {"123456789", "000000000", "99999999", "11111111"}


def sestrinske_sifre(conn: Connection, dobavljac: str) -> set[str]:
    """Sve analitike koje pripadaju istom pravnom licu kao data sifra.

    Isti dobavljac ume da stoji dvaput u komitentima - Sulex je otvoren kao
    `04808 Sulex d.o.o` (bez maticnog broja) i `15582 Sulex DOO za trgovinu i
    usluge` (MB 08734054), oba sa PIB-om 100273149. App po maticnom broju
    izabere 15582, a sve cetiri postojece kalkulacije stoje pod 04808, pa
    provera duplikata koja gleda samo jednu sifru promasi.

    Mereno na ELBS_2026: 1.339 PIB-ova deli 3.075 sifara, 139 maticnih brojeva
    deli 295 sifara. Sifarnik se ne dira - definicija tabele u ERP-u nije nasa -
    nego se pri proveri gleda cela grupa.
    """
    if not dobavljac or not dobavljac.strip():
        return set()
    svoja = dobavljac.strip()
    red = conn.execute(
        text(
            "SELECT TOP 1 RTRIM(ISNULL(MATBROJ,'')) mb, RTRIM(ISNULL(ZIRORACUN,'')) pib "
            "FROM dbo.NAZIVI WHERE RTRIM(ANALITIKA) = :a"
        ),
        {"a": svoja},
    ).first()
    sifre = {svoja}
    if not red:
        return sifre
    for vrednost in (red.mb, red.pib):
        v = (vrednost or "").strip()
        if not v or not v.strip("0") or v in PRAZNI_IDENTIFIKATORI:
            continue
        for r in conn.execute(
            text(
                "SELECT RTRIM(ANALITIKA) a FROM dbo.NAZIVI "
                "WHERE RTRIM(ISNULL(MATBROJ,'')) = :v OR RTRIM(ISNULL(ZIRORACUN,'')) = :v"
            ),
            {"v": v},
        ):
            if r.a:
                sifre.add(r.a.strip())
    return sifre


def postojeca_kalkulacija(
    conn: Connection, broj_racuna: str, dobavljac: str
) -> Pogodak | None:
    """Da li za taj racun tog dobavljaca vec postoji kalkulacija u ERP-u.

    Poslovodje i dalje prave kalkulacije rukom, pa app mora da proveri pre upisa -
    dve kalkulacije za istu fakturu su gore nego nijedna. Poredi se po
    normalizovanom broju (`FA-5543-03/26` == `FA55430326`), jer covek crtice i
    kose crte kuca kako mu dodje.

    Dva su nacina na koja se isti racun ne prepozna, oba nadjena kod Bogdana:

    * ERP-ova forma pusta 15 znakova, pa duzi broj stoji odsecen. Zato se, kad
      tacnog poklapanja nema, prihvata i zapis koji je tacno na toj granici i
      predstavlja pocetak trazenog broja - ali samo ako je kandidat jedan.
      Mereno na ELBS_2026: svih 567 zapisa na granici ima jedinstven par
      (dobavljac, broj), a pravilo vraca 51 dokument koji sad stoji na ekranu.
      Neograniceno poredjenje po pocetku bi napravilo 9 laznih pogodaka, zato
      granica od 15 znakova mora da ostane uslov.
    * dobavljac je u sifarniku otvoren dvaput, pa kalkulacija stoji pod drugom
      analitikom - trazi se po celoj grupi, vidi `sestrinske_sifre`.
    """
    from ..services.mapiranje import DUZINA_U_ERPU, NAJKRACI_POCETAK, kljuc_racuna

    trazeni = kljuc_racuna(broj_racuna)
    if not trazeni or not dobavljac:
        return None

    sifre = sestrinske_sifre(conn, dobavljac)
    imenovani = {f"d{i}": s for i, s in enumerate(sorted(sifre))}
    uslov = ", ".join(f":{k}" for k in imenovani)

    odseceni: list[str] = []
    for tabela in ("ULKALKUL", "MKALKUL"):
        for red in conn.execute(
            text(
                f"SELECT RTRIM(BROJ) br, RTRIM(MAGACIN) mag, RTRIM(DOBAVLJAC) dob, "
                f"RTRIM(ISNULL(BROJ_RN_DOB,'')) rn "
                f"FROM dbo.{tabela} WHERE RTRIM(DOBAVLJAC) IN ({uslov})"
            ),
            imenovani,
        ):
            if not red.rn:
                continue
            kljuc = kljuc_racuna(red.rn)
            oznaka = f"{tabela} {red.mag}/{red.br}"
            drugi = f"pod šifrom dobavljača {red.dob}" if red.dob != dobavljac.strip() else ""
            if kljuc == trazeni:
                return Pogodak(oznaka, drugi)
            if (
                len(red.rn) == DUZINA_U_ERPU
                and len(kljuc) >= NAJKRACI_POCETAK
                and trazeni.startswith(kljuc)
            ):
                razlog = f"broj odsečen na {DUZINA_U_ERPU} znakova: „{red.rn}”"
                odseceni.append(Pogodak(oznaka, f"{razlog}, {drugi}" if drugi else razlog))

    # Vise kandidata znaci da se ne zna koji je - tada je cutanje ispravnije od
    # pogodjenog para, jer bi lazan pogodak sklonio fakturu sa ekrana.
    if len(odseceni) == 1:
        return odseceni[0]
    return None


def moguca_kalkulacija(
    conn: Connection,
    broj_racuna: str,
    dobavljac: str,
    iznos: float | None,
    dan: int | None,
    poznati_brojevi: set[str],
) -> Pogodak | None:
    """Predlog kad se broj racuna ne poklapa, ali sve ostalo se poklapa.

    Bogdan je uneo `9999` gde faktura ima `999`, pa app njegovu kalkulaciju nije
    prepoznala i faktura mu je ostala na ekranu. Predlog se trazi po istom
    dobavljacu, istom datumu i istom iznosu.

    Samo to pravilo ne valja: mereno na ELBS_2026, **482 puta** dve razlicite
    fakture istog dobavljaca imaju isti datum i isti iznos, a ako se doda i
    „broj se razlikuje u jednom znaku” sudara ima **1.717** (uzastopni povrati
    `263X3000817` i `263X3000818`). Zato se gleda samo medju kalkulacijama koje
    **nisu** sparene ni sa jednim SEF dokumentom - tada je od 1.409 grupa
    dvosmisleno svega 13 (0,9 %) - i uzima se samo kad je kandidat jedan.

    Vraca `siguran=False`: predlog ide coveku na potvrdu, app sama ne sklanja
    nista sa ekrana. Lazan pogodak bi fakturu tiho izbrisao i niko je ne bi
    zaknjizio, a to je gore od toga da mu stoji na ekranu.
    """
    from ..services.mapiranje import kljuc_racuna

    trazeni = kljuc_racuna(broj_racuna)
    if not trazeni or not dobavljac or iznos is None or not dan:
        return None

    sifre = sestrinske_sifre(conn, dobavljac)
    imenovani = {f"d{i}": s for i, s in enumerate(sorted(sifre))}
    uslov = ", ".join(f":{k}" for k in imenovani)
    parametri = {**imenovani, "dan": int(dan), "iznos": float(iznos)}

    kandidati: list[Pogodak] = []
    for tabela in ("ULKALKUL", "MKALKUL"):
        for red in conn.execute(
            text(
                f"SELECT RTRIM(BROJ) br, RTRIM(MAGACIN) mag, RTRIM(DOBAVLJAC) dob, "
                f"RTRIM(ISNULL(BROJ_RN_DOB,'')) rn FROM dbo.{tabela} "
                f"WHERE RTRIM(DOBAVLJAC) IN ({uslov}) AND DATUM = :dan "
                f"AND ABS(ISNULL(IZNOS_ZA_NAP, 0) - :iznos) < 0.01"
            ),
            parametri,
        ):
            kljuc = kljuc_racuna(red.rn)
            if not kljuc or kljuc in poznati_brojevi:
                # Ta kalkulacija vec pripada nekoj drugoj fakturi sa SEF-a.
                continue
            razlog = (
                f"isti dobavljač, isti datum, isti iznos; broj „{red.rn}” — "
                f"{ocena_broja(broj_racuna, red.rn)}"
            )
            if red.dob != dobavljac.strip():
                razlog += f"; pod šifrom dobavljača {red.dob}"
            kandidati.append(Pogodak(f"{tabela} {red.mag}/{red.br}", razlog, siguran=False))

    return kandidati[0] if len(kandidati) == 1 else None


def upisi(nacrt: Nacrt, *, produkcija: bool = False) -> str:
    """Upisuje nacrt. Podrazumevano u test bazu; produkcija trazi dozvolu."""
    if not nacrt.spreman:
        prepreke = nacrt.problemi + [
            f"stavka {x['line_no']} nema našu šifru: {x['naziv']} "
            f"(šifra dob. {x['sifra_dobavljaca'] or '-'}, EAN {x['ean'] or '-'})"
            for x in nacrt.nemapirane
        ]
        raise ValueError("Kalkulacija nije spremna: " + "; ".join(prepreke[:5]))

    # Poslednja brana: knjizenje ide iskljucivo kroz ERP, jer povlaci glavnu
    # knjigu i ostale evidencije. Kalkulacija koja stigne oznacena kao
    # zaknjizena izgleda gotovo, a nista se nije desilo.
    if nacrt.zaglavlje.get("PROKNJIZENO") != PROKNJIZENO_NIJE:
        raise ValueError(
            "Aplikacija ne sme da upise kalkulaciju kao zaknjizenu "
            f"(PROKNJIZENO={nacrt.zaglavlje.get('PROKNJIZENO')!r}). "
            "Knjizenje se radi u ERP-u."
        )

    eng, baza = erp_engine_za_upis(produkcija)

    # Poslovodje i dalje rade rucno - ako su vec uneli ovu fakturu, app staje.
    with eng.connect() as conn:
        postoji = postojeca_kalkulacija(
            conn,
            str(nacrt.zaglavlje.get("BROJ_RN_DOB") or ""),
            str(nacrt.zaglavlje.get("DOBAVLJAC") or ""),
        )
    if postoji:
        raise VecPostoji(f"Kalkulacija za taj račun već postoji u ERP-u: {postoji}")

    # Broj kalkulacije je brojac po magacinu (MAX+1), a ne sekvenca. ERP ga
    # dodeljuje tek na "OK" - do tada glava stoji na ekranu bez broja i bez reda
    # u bazi (provereno 15.09.). Prozor za sudar je zato uzak, ali postoji.
    #
    # Sreca je sto tabele imaju primarni kljuc (MAGACIN, VRSTAKNJIZ, BROJ):
    # sudar zavrsi kao odbijen upis, nikad kao dva reda sa istim brojem. Zato je
    # dovoljno uzeti sledeci broj i probati ponovo.
    for pokusaj in range(1, POKUSAJA_ZA_BROJ + 1):
        try:
            with eng.begin() as conn:
                broj = sledeci_broj(conn, nacrt.tabela_zaglavlja, nacrt.magacin)
                nacrt.zaglavlje["BROJ"] = broj
                for s in nacrt.stavke:
                    s["BROJ"] = broj

                zaglavlje = dopuni_prazna(conn, nacrt.tabela_zaglavlja, nacrt.zaglavlje)
                kolone = list(zaglavlje)
                conn.execute(
                    text(
                        f"INSERT INTO dbo.{nacrt.tabela_zaglavlja} ({', '.join(kolone)}) "
                        f"VALUES ({', '.join(':' + k for k in kolone)})"
                    ),
                    zaglavlje,
                )
                for s in nacrt.stavke:
                    vidljive = {k: v for k, v in s.items() if not k.startswith("_")}
                    vidljive = dopuni_prazna(conn, nacrt.tabela_stavki, vidljive)
                    kolone = list(vidljive)
                    conn.execute(
                        text(
                            f"INSERT INTO dbo.{nacrt.tabela_stavki} ({', '.join(kolone)}) "
                            f"VALUES ({', '.join(':' + k for k in kolone)})"
                        ),
                        vidljive,
                    )
                for red in nacrt.pdv_evidencija:
                    vidljive = dopuni_prazna(
                        conn, TABELA_PDV_EVIDENCIJE,
                        dict(red, DOKUMENT=f"{nacrt.magacin}{broj}"),
                    )
                    kolone = list(vidljive)
                    conn.execute(
                        text(
                            f"INSERT INTO dbo.{TABELA_PDV_EVIDENCIJE} ({', '.join(kolone)}) "
                            f"VALUES ({', '.join(':' + k for k in kolone)})"
                        ),
                        vidljive,
                    )
            break
        except IntegrityError:
            # Neko je u medjuvremenu uzeo taj broj - u ERP-u ili drugi upis nas.
            if pokusaj == POKUSAJA_ZA_BROJ:
                raise
            log.info(
                "Broj %s/%s je zauzet, uzimam sledeci (pokusaj %d)",
                nacrt.magacin, nacrt.zaglavlje.get("BROJ"), pokusaj,
            )
            time.sleep(0.2 * pokusaj)

    log.info("Kalkulacija %s/%s upisana u %s", nacrt.magacin, broj, baza)
    opis = f"{baza}: {nacrt.tabela_zaglavlja} {nacrt.magacin}/U/{broj}, stavki {len(nacrt.stavke)}"
    if nacrt.pdv_evidencija:
        tipovi = ", ".join(str(r["TIP"]) for r in nacrt.pdv_evidencija)
        opis += f", PDV evidencija po tipovima {tipovi}"
    return opis
