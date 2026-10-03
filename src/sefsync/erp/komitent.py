"""Predlog novog komitenta u ERP-ov sifarnik `NAZIVI`, iz podataka sa e-fakture.

Dobavljac koga nema u sifarniku zaustavlja kalkulaciju. Mereno 03.10.: od 198
dobavljaca u istoriji panela, tri se ne nalaze ni po maticnom, ni po PIB-u, ni
po racunu - dakle redak ali potpun zastoj.

Sta ide gde (`NAZIVI` je jedan red po komitentu):

    ANALITIKA  pet cifara, 1..99999; slobodnih mesta 54.882 (mereno 03.10.)
    NAZIV1     varchar(30) - skraceno ime, vidi `skrati_naziv`
    APRNAZIV   varchar(255) - pun pravni naziv, onakav kakav je na fakturi
    ADRESA     varchar(30), SEDISTE varchar(30), PAK varchar(6)
    ZIRORACUN  varchar(22) - uprkos imenu, PIB (kod fizickih lica JMBG)
    MATBROJ    varchar(9), TEKUCI varchar(20), EMAIL varchar(49)

Ovaj modul nista ne upisuje sam - `upisi` se poziva iz toka u kom covek klikne.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..textutil import normalize
from .connection import erp_engine_za_upis, get_erp_engine

log = logging.getLogger(__name__)

NAJVECA_ANALITIKA = 99_999
# Kao i kod artikala: nove sifre idu u blok, ne u usamljene rupe. Mereno 03.10.:
# slobodnih mesta ima 54.882, a rupa duzine bar 100 ima 110 - prostora napretek.
MIN_RUPA = 100

# Pravni oblik: sta se prepoznaje u punom nazivu -> sta se pise u NAZIV1.
#
# Tacke se NE pisu. Istorijski ih ima ("d.o.o." 676 puta prema "doo" 1.441), ali
# su to greske operatera kroz godine i ne ponavljaju se (Bane, 03.10.).
OBLICI: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bjavno\s+komunalno\s+preduzece\b|\bjkp\b"), "JKP"),
    (re.compile(r"\bjavno\s+preduzece\b|\bjp\b"), "JP"),
    (re.compile(r"\bakcionarsko\s+drustvo\b|\ba\.?\s?d\.?\b|\bado\b"), "ad"),
    (re.compile(r"\bdrustvo\s+sa\s+ogranicenom\s+odgovornoscu\b|\bd\.?\s?o\.?\s?o\.?\b"), "doo"),
    (re.compile(r"\bpreduzetnik\b|\bpr\b"), "pr"),
    (re.compile(r"\bzemljoradnicka\s+zadruga\b|\bzz\b"), "zz"),
    (re.compile(r"\bortacko\s+drustvo\b|\bod\b"), "od"),
    (re.compile(r"\bkomanditno\s+drustvo\b|\bkd\b"), "kd"),
]

# Reci koje opisuju delatnost, a ne imenuju firmu. Sve se izbacuju pre nego sto
# se trazi brend: "PREDUZECE ZA SERVIS I TRGOVINU VITEL DOO NIS" -> "VITEL".
OPISNE = {
    "PREDUZECE", "DRUSTVO", "PRIVREDNO", "ZA", "I", "SA", "ZADRUGA", "RADNJA",
    "PROIZVODNJU", "PROIZVODNJA", "PROIZVODNO", "TRGOVINU", "TRGOVINA", "TRGOVINSKO",
    "USLUGE", "USLUGA", "USLUZNO", "SERVIS", "SERVISIRANJE", "PROMET", "PROMETNO",
    "EXPORT", "IMPORT", "EXPORTIMPORT", "EXPORT-IMPORT", "UVOZ", "IZVOZ",
    "SPOLJNU", "UNUTRASNJU", "SPOLJNO", "UNUTRASNJO", "DISTRIBUCIJU", "DISTRIBUCIJA",
    "GRADEVINARSTVO", "GRADEVINSKO", "POSREDOVANJE", "KONSALTING", "INZENJERING",
    "OGRANICENOM", "ODGOVORNOSCU", "AKCIONARSKO", "JAVNO", "KOMUNALNO", "KOMUNALNE",
    "DELATNOSTI", "OSTALO", "OSTALE", "DRUGO", "VELEPRODAJA", "MALOPRODAJA",
    "EKSPORT", "COMPANY", "DOO", "AD", "PR", "ADO", "KOMPANIJA", "NA", "VELIKO",
    "MALO", "PREVOZ", "PUTNIKA", "ROBE", "STRANIH", "DOMACIH", "TELEKOMUNIKACIJE",
    "OSIGURANJA", "POSLOVANJE", "DELATNOST", "IZNAJMLJIVANJE", "ODRZAVANJE",
}


@dataclass
class PredlogKomitenta:
    """Sta bi se upisalo u NAZIVI. Nista se ne upisuje samo od sebe."""

    document_id: int
    analitika: str | None = None          # prva slobodna
    naziv: str = ""                       # NAZIV1, do 30 znakova
    pun_naziv: str = ""                   # APRNAZIV
    adresa: str = ""
    mesto: str = ""
    posta: str = ""
    pib: str = ""
    maticni: str = ""
    racun: str = ""
    email: str = ""
    drzava: str = "Srbija"
    # Komitenti koji lice - provera da se isti ne otvori drugi put.
    slicni: list[dict] = field(default_factory=list)
    napomene: list[str] = field(default_factory=list)

    @property
    def spreman(self) -> bool:
        return bool(self.analitika and self.naziv and not self.postoji)

    @property
    def postoji(self) -> bool:
        """Nadjen je po broju - onda se ne otvara nego koristi postojeci."""
        return any(s.get("po_broju") for s in self.slicni)


def _preslovi(tekst: str) -> str:
    """Cirilica u latinicu; `normalize` radi ostalo (velika slova, kvacice)."""
    cir = "АБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШабвгдђежзијклљмнњопрстћуфхцчџш"
    lat = ["A","B","V","G","D","Dj","E","Z","Z","I","J","K","L","Lj","M","N","Nj",
           "O","P","R","S","T","C","U","F","H","C","C","Dz","S",
           "a","b","v","g","d","dj","e","z","z","i","j","k","l","lj","m","n","nj",
           "o","p","r","s","t","c","u","f","h","c","c","dz","s"]
    return "".join(lat[cir.index(z)] if z in cir else z for z in tekst)


def skrati_naziv(pun: str, mesto: str = "") -> str:
    """Pun pravni naziv -> ime za `NAZIV1` (30 znakova).

    Pravilo je procitano iz sifarnika, nije izmisljeno: **brend prvo, pa
    skraceni pravni oblik**, bez opisnog dela i bez mesta.

        PREDUZECE ZA SERVIS I TRGOVINU VITEL DOO NIS  -> Vitel doo
        EKSER PROIZVODNJA EXPORT-IMPORT I TRGOVINA DOO -> Ekser doo
        JAVNO KOMUNALNO PREDUZECE VODOKANAL SOMBOR    -> Vodokanal JKP
    """
    if not pun:
        return ""
    # Ono u zagradi je dopuna ("(NOVI BEOGRAD)"), ne deo imena. Mora pre
    # `normalize`, koja zagrade pretvara u razmake pa se vise ne vide.
    osnova = normalize(_preslovi(re.sub(r"\([^)]*\)", " ", pun)))
    oblik = next((o for uzorak, o in OBLICI if uzorak.search(osnova.lower())), "")

    bez_mesta = set(normalize(_preslovi(mesto)).split()) if mesto else set()
    reci = [r.strip(".,&-") for r in osnova.replace("-", " ").split()]
    brend = [
        r for r in reci
        # Slova duzine jedan su ostaci tackastih oblika ("d.o.o." -> D, O, O;
        # "A.D." -> A, D) i ne smeju da zavrse u imenu.
        if len(r) > 1 and r not in OPISNE and r not in bez_mesta and not r.isdigit()
    ]
    if not brend:
        brend = [r for r in reci if len(r) > 1][:2]

    # Brend je ono sto dolazi na pocetku; vise od tri reci ne staje u 30 znakova.
    #
    # Pisanje: prva rec velikim pocetnim slovom, ostale malim - tako stoji u
    # sifarniku ("Banca intesa", "Comtrade distribution", "Raiffeisen banka").
    delovi = brend[:3]
    ime = " ".join([delovi[0].capitalize()] + [d.lower() for d in delovi[1:]])
    return (f"{ime} {oblik}".strip() if oblik else ime)[:30].strip()


def slobodna_analitika(conn: Connection, *, najmanja_rupa: int = MIN_RUPA) -> str | None:
    """Prva slobodna sifra iz dovoljno velikog bloka."""
    zauzete = {
        int(r[0])
        for r in conn.execute(
            text("SELECT RTRIM(ANALITIKA) FROM dbo.NAZIVI "
                 "WHERE RTRIM(ANALITIKA) NOT LIKE '%[^0-9]%'")
        )
    }
    poc = None
    for i in range(1, NAJVECA_ANALITIKA + 1):
        if i in zauzete:
            if poc is not None and i - poc >= najmanja_rupa:
                return f"{poc:05d}"
            poc = None
        elif poc is None:
            poc = i
    if poc is not None and NAJVECA_ANALITIKA + 1 - poc >= najmanja_rupa:
        return f"{poc:05d}"
    return None


def slicni_komitenti(
    conn: Connection, *, naziv: str, mesto: str, pib: str = "", maticni: str = ""
) -> list[dict]:
    """Komitenti koji bi mogli biti isti - po broju, pa po imenu i mestu.

    Prvo brojevi, jer su dokaz: `MATBROJ`, pa `ZIRORACUN` (koji uprkos imenu
    drzi PIB). Kod fizickih lica tamo stoji JMBG od 13 cifara, pa se PIB od
    devet ne poredi sa njim.
    """
    nadjeni: list[dict] = []
    vidjene: set[str] = set()

    def dodaj(red, kako: str, po_broju: bool) -> None:
        if red.a in vidjene:
            return
        vidjene.add(red.a)
        nadjeni.append({"analitika": red.a, "naziv": red.nz, "mesto": red.se,
                        "pib": red.zr, "maticni": red.mb, "kako": kako,
                        "po_broju": po_broju})

    upit = ("SELECT TOP 20 RTRIM(ANALITIKA) a, RTRIM(NAZIV1) nz, "
            "RTRIM(ISNULL(SEDISTE,'')) se, RTRIM(ISNULL(ZIRORACUN,'')) zr, "
            "RTRIM(ISNULL(MATBROJ,'')) mb FROM dbo.NAZIVI WHERE ")
    if maticni.strip():
        for r in conn.execute(text(upit + "RTRIM(ISNULL(MATBROJ,''))=:v"),
                              {"v": maticni.strip()}):
            dodaj(r, "isti matični broj", True)
    if pib.strip() and len(pib.strip()) == 9:
        for r in conn.execute(text(upit + "RTRIM(ISNULL(ZIRORACUN,''))=:v"),
                              {"v": pib.strip()}):
            dodaj(r, "isti PIB", True)

    trazene = {x for x in normalize(_preslovi(naziv)).split() if len(x) > 2} - OPISNE
    if trazene:
        najduza = max(trazene, key=len)
        mesne = {x for x in normalize(_preslovi(mesto)).split() if len(x) > 2}
        for r in conn.execute(text(upit + "NAZIV1 LIKE :u"), {"u": f"%{najduza}%"}):
            njegove = {x for x in normalize(_preslovi(r.nz)).split() if len(x) > 2} - OPISNE
            if not njegove:
                continue
            if njegove <= trazene or (
                trazene & njegove and {x for x in normalize(r.se).split()} & mesne
            ):
                dodaj(r, "slično ime", False)
    return nadjeni


def upisi(predlog: PredlogKomitenta, *, produkcija: bool = False) -> str:
    """Upisuje komitenta u `NAZIVI`. Jedan red, jedna transakcija."""
    if not predlog.analitika or not predlog.naziv:
        raise ValueError("Predlog nije potpun — fale šifra ili naziv.")

    red = {
        "ANALITIKA": predlog.analitika,
        "NAZIV1": predlog.naziv[:30],
        "APRNAZIV": (predlog.pun_naziv or predlog.naziv)[:255],
        "ADRESA": predlog.adresa[:30],
        "SEDISTE": predlog.mesto[:30],
        "PAK": predlog.posta[:6],
        "ZIRORACUN": predlog.pib[:22],
        "MATBROJ": predlog.maticni[:9],
        "TEKUCI": predlog.racun[:20],
        "EMAIL": predlog.email[:49],
        "DRZAVA": predlog.drzava[:30],
        "SH_KOM": ".",
        "LASTUSER": 0,
    }
    engine, baza = erp_engine_za_upis(produkcija)
    with engine.begin() as conn:
        zauzeta = conn.execute(
            text("SELECT TOP 1 RTRIM(NAZIV1) n FROM dbo.NAZIVI WHERE RTRIM(ANALITIKA)=:a"),
            {"a": predlog.analitika},
        ).first()
        if zauzeta is not None:
            raise ValueError(
                f"Šifra {predlog.analitika} je u međuvremenu zauzeta "
                f"(„{zauzeta.n}“) — osveži predlog."
            )
        kolone = ", ".join(red)
        mesta = ", ".join(f":{k}" for k in red)
        conn.execute(text(f"INSERT INTO dbo.NAZIVI ({kolone}) VALUES ({mesta})"), red)

    opis = f"{baza}: komitent {predlog.analitika} „{predlog.naziv}“, PIB {predlog.pib or '-'}"
    log.info("Otvoren komitent: %s", opis)
    return opis


def predlozi(document_id: int, *, engine: Engine | None = None) -> PredlogKomitenta:
    """Sklapa predlog komitenta iz dobavljaca na dokumentu."""
    from ..db import session_scope
    from ..models import Document

    eng = engine or get_erp_engine()
    with session_scope() as session, eng.connect() as conn:
        doc = session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"Dokument #{document_id} ne postoji.")
        pun = (doc.supplier_name or "").strip()
        mesto = (doc.delivery_city or "").strip()
        predlog = PredlogKomitenta(
            document_id=document_id,
            pun_naziv=pun,
            naziv=skrati_naziv(pun, mesto),
            mesto=mesto,
            pib=(doc.supplier_vat or "").strip(),
            maticni=(doc.supplier_reg_no or "").strip(),
            racun=(doc.payment_account or "").strip(),
        )
        predlog.slicni = slicni_komitenti(
            conn, naziv=pun, mesto=mesto, pib=predlog.pib, maticni=predlog.maticni
        )
        if predlog.postoji:
            predlog.napomene.append(
                "Ovaj komitent već postoji u šifarniku — ne otvarati novog."
            )
            return predlog

        if predlog.slicni:
            predlog.napomene.append(
                "Postoji komitent sličnog imena — proveri da ne otvaraš drugi put."
            )
        predlog.analitika = slobodna_analitika(conn)
        if predlog.analitika is None:
            predlog.napomene.append("Nema slobodnog bloka šifara — odrediti ručno.")
        if len(pun) > 30:
            predlog.napomene.append(
                "Naziv sa fakture je duži od 30 znakova — predlog je skraćen, proveri ga."
            )
        return predlog
