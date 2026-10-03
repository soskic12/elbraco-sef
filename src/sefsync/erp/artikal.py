"""Predlog nove sifre artikla iz stavke e-fakture.

Otvaranje sifre je do sada rucni posao: poslovodje slikaju ambalazu u Viber grupu,
Dusan otvara sifru u ERP-u i javi u grupu. Ovde se priprema sve sto se sa fakture
moze znati, da bi mu ostalo samo da pogleda i klikne.

Sta ide gde:

    ARTIKLI    maticni red (SIFRAMAG '099') + red po robnom magacinu - imenovani
               spisak, vidi MAGACINI nize
    ARTPROD    red po prodajnom mestu koje nije usluzno (cita se iz dbo.PRODAV)
    DARTIKLI   **ne pisemo mi.** Nocni posao na serveru svaku novu sifru iz
               jedinstvenog sifarnika sam otvori i u DARTIKLI, a Dule sutradan
               dopuni zemlju porekla. Tabela nema nijedan indeks (proverio
               03.10.) - dupli red se ne bi ni prijavio kao greska nego bi tiho
               ostao, pa bi se artikal duplirao u svemu sto iz nje cita. Dok se
               taj posao ne pogleda, mi je ne diramo (Bane, 03.10.).

ERP i sam kopira artikal iz 099 u lokalni cenovnik kad korisnik krene da radi
kalkulaciju a sifre tamo nema. Mi upisujemo unapred u sva robna mesta, da
poslovodja ne zapne.

Stalne vrednosti koje ERP ocekuje (sve mereno, 300/300):
    DECKOLIC=2, DECCENA=2, VLAGA=LOM=PRIMESE=NECISTO=0.00, PCENA=0.000
    PROIZVODJAC='.' (popuna, upisuje se u 294/300)
    TARIFA: 20 % -> '002', 10 % -> '001', 0 % -> '003' (PTARIFE)

Ovaj modul NISTA ne upisuje. Nalog nema pravo pisanja po sifarniku, a i da ima,
sifru otvara covek - 43.474 artikla se ne pune bez nadzora.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..db import session_scope
from ..models import Document, DocumentLine
from ..textutil import normalize
from .connection import erp_engine_za_upis, get_erp_engine

log = logging.getLogger(__name__)

MATICNI_MAGACIN = "099"

# Sifre robe zive ispod 100.000; iznad toga su posebne (do 9.999.999).
GORNJA_GRANICA = 100_000
# Nove sifre idu u blok, ne u usamljene rupe medju starim artiklima. Prag je
# odluka (Bane, 17.09.): bar 500 slobodnih mesta u komadu. Mereno, takvih rupa
# ima 26 sa ukupno 47.039 mesta - pri tempu od ~60 novih artikala mesecno to
# traje godinama, a po sifri se vidi da su iz istog perioda.
MIN_RUPA = 500

# UN/ECE Rec 20 (sa fakture) -> JEDMERE (u ERP-u). Mereno: 42.843 od 43.491
# artikla je "kom", pa je to i podrazumevano kad se jedinica ne prepozna.
JEDINICE = {
    "H87": "kom", "C62": "kom", "EA": "kom", "PCE": "kom", "NAR": "kom",
    "LTR": "l", "MLT": "l", "KGM": "kg", "GRM": "g", "MTR": "m",
    "MTK": "m2", "MTQ": "m3", "SET": "pak", "PK": "pak", "BX": "pak",
}

# PDV sa fakture -> sifra tarife u ERP-u (PTARIFE)
TARIFE = {20: "002", 10: "001", 0: "003"}

# Vrednosti koje ERP ocekuje na svakom novom artiklu (mereno 300/300)
STALNO_ARTIKLI = {
    "DECKOLIC": 2,
    "DECCENA": 2,
    "VLAGA": 0.0,
    "LOM": 0.0,
    "PRIMESE": 0.0,
    "NECISTO": 0.0,
    "PCENA": 0.0,
    "PROIZVODJAC": ".",
}


@dataclass
class PredlogArtikla:
    """Sta bi se upisalo da se sifra otvori. Nista se ne upisuje samo od sebe."""

    document_id: int
    line_no: int
    sifra: str | None = None          # popunjeno samo kad barkod vec ima sifru
    predlozena_sifra: str | None = None   # prva slobodna, ako se otvara nova
    naziv: str = ""
    barkod: str = ""
    jedmere: str = "kom"
    tarifa: str = "002"
    pdv_stopa: float = 20.0
    nabavna_cena: float | None = None
    magacini: list[str] = field(default_factory=list)
    prodajna_mesta: list[str] = field(default_factory=list)
    napomene: list[str] = field(default_factory=list)
    # Artikli ciji naziv lici - jedina provera kad EAN-a nema.
    slicni: list[dict] = field(default_factory=list)
    # Maloprodajna cena je uslov, ne ukras: bez nje kalkulacija u maloprodaji
    # staje sa "artikal nema cenu u objektu".
    cena_mp: float | None = None
    # Odakle je cena predlozena (ili zasto nije) - da covek vidi na cemu stoji.
    cena_predlog: dict | None = None
    # Covek je izricito rekao da artikal nema barkod - tek tada se upisuje
    # sifra umesto barkoda, kako stoji kod 7.005 postojecih artikala.
    bez_barkoda: bool = False

    @property
    def broj_redova(self) -> int:
        """Koliko redova bi nastalo: ARTIKLI po magacinu + ARTPROD po mestu."""
        return len(self.magacini) + len(self.prodajna_mesta)

    @property
    def barkod_za_upis(self) -> str:
        """Barkod koji ide u bazu; kad ga artikal nema, to je sama sifra."""
        if self.barkod:
            return self.barkod
        return (self.sifra or self.predlozena_sifra or "") if self.bez_barkoda else ""

    @property
    def spreman(self) -> bool:
        """Sme li da se upise. Barkod i cena su uslov, ne preporuka."""
        return bool(
            (self.sifra or self.predlozena_sifra)
            and self.naziv
            and self.barkod_za_upis
            and self.cena_mp
            and self.cena_mp > 0
        )


def _zauzete(conn: Connection) -> set[int]:
    """Sve brojcane sifre koje postoje u bilo kojoj od tri tabele.

    Namerno unija, ne samo ARTIKLI/099: sifra ume da postoji u DARTIKLI a da u
    maticnom magacinu vise nije, i takvu ne smemo dodeliti drugom artiklu.
    """
    zauzete: set[int] = set()
    for tabela in ("ARTIKLI", "ARTPROD", "DARTIKLI"):
        zauzete |= {
            r.v
            for r in conn.execute(
                text(f"SELECT DISTINCT TRY_CAST(RTRIM(ARTIKAL) AS bigint) v FROM dbo.{tabela}")
            )
            if r.v is not None
        }
    return zauzete


def rupe(conn: Connection, *, do: int = GORNJA_GRANICA, najmanja: int = 1) -> list[tuple[int, int]]:
    """Opsezi slobodnih sifara, redom. Vraca (od, do) ukljucivo.

    Mereno 17.09.2026: ispod 100.000 ima 43.110 zauzetih i **56.890 slobodnih**
    mesta u 829 rupa. Najvece su 40411-45378 (4.968) i 45380-50000 (4.621).
    """
    zauzete = sorted(x for x in _zauzete(conn) if 0 < x < do)
    nadjene = []
    for a, b in zip(zauzete, zauzete[1:]):
        if b - a - 1 >= najmanja:
            nadjene.append((a + 1, b - 1))
    return nadjene


def slobodna_sifra(conn: Connection, *, najmanja_rupa: int = MIN_RUPA) -> str | None:
    """Prva slobodna sifra iz prve dovoljno velike rupe.

    `najmanja_rupa` postoji da se nove sifre ne razbacuju po usamljenim rupama
    medju starim artiklima (ima ih 800+ velicine jedan), nego da idu redom u
    jednom bloku - tako se i po samoj sifri vidi da su iz istog perioda.

    Zauzetost se proverava jos jednom, upitom za bas tu sifru: spisak je snimljen
    na pocetku, a sifru u medjuvremenu moze otvoriti neko drugi.
    """
    for od, do_ in rupe(conn, najmanja=najmanja_rupa):
        for kandidat in range(od, do_ + 1):
            if not zauzeta(conn, str(kandidat)):
                return str(kandidat)
    return None


def zauzeta(conn: Connection, sifra: str) -> bool:
    """Postoji li ta sifra igde u sifarniku."""
    for tabela in ("ARTIKLI", "ARTPROD", "DARTIKLI"):
        red = conn.execute(
            text(f"SELECT TOP 1 1 FROM dbo.{tabela} WHERE RTRIM(ARTIKAL) = :s"),
            {"s": sifra.strip()},
        ).first()
        if red is not None:
            return True
    return False


# Magacini u koje ide nov artikal. Spisak je odluka (Bane, 17.09.), ne merenje:
# tehnicke magacine (izdata roba, rezervacije, inventarski, cenovnik, testni,
# avansni, 088) ERP puni sam kad zatrebaju - vidi se po televizoru 87187, koji je
# dobio redove u 002, 012 i 051 iako ga tamo niko nije otvarao.
#
# 022, 024 i 025 su namerno izostavljeni: ta mesta nemaju izdvojen magacin nego
# svu robu drze na lageru prodajnog mesta, pa su ti magacini prazni (0 artikala).
MAGACINI = ("001", "002", "005", "010", "012", "021", "023", "026", "029", "071", "099")


def _mesta(conn: Connection) -> tuple[list[str], list[str]]:
    """Magacini i prodajna mesta u koja ide nov artikal.

    Magacini su imenovani spisak; prodajna mesta se citaju iz ERP-a, sva osim
    usluznih ("PRODAJNO MESTO APATIN USLUGA") - tako nova prodavnica udje sama,
    bez diranja koda.
    """
    mesta = [
        r.s
        for r in conn.execute(
            text(
                "SELECT RTRIM(SIFRA) s FROM dbo.PRODAV "
                "WHERE UPPER(NAZIV) NOT LIKE '%USLUG%' ORDER BY SIFRA"
            )
        )
    ]
    return list(MAGACINI), mesta


def slicni_artikli(conn: Connection, naziv: str, *, najvise: int = 5) -> list[dict]:
    """Artikli ciji naziv lici - da se ista stvar ne otvori drugi put.

    Trazi se po najduzoj reci iz naziva, pa se poredi skup reci. Poredjenje u
    ERP-u ne razlikuje ni velika slova ni kvacice.
    """
    reci = [r for r in normalize(naziv).split() if len(r) > 2]
    if not reci:
        return []
    najduza = max(reci, key=len)
    trazene = set(reci)
    nadjeni = []
    for r in conn.execute(
        text("SELECT TOP 200 RTRIM(ARTIKAL) a, RTRIM(NAZIV) n, RTRIM(ISNULL(BARCODE,'')) b "
             f"FROM dbo.ARTIKLI WHERE RTRIM(SIFRAMAG)='{MATICNI_MAGACIN}' AND NAZIV LIKE :u"),
        {"u": f"%{najduza}%"},
    ):
        njegove = {x for x in normalize(r.n).split() if len(x) > 2}
        if not njegove:
            continue
        poklapanje = len(trazene & njegove) / len(trazene | njegove)
        if poklapanje >= 0.5:
            cene = conn.execute(
                text("SELECT TOP 1 CENAMALO mp, NABCENA nab FROM dbo.ARTPROD "
                     "WHERE RTRIM(ARTIKAL)=:a AND CENAMALO > 0"),
                {"a": r.a},
            ).first()
            nadjeni.append({
                "artikal": r.a, "naziv": r.n, "barkod": r.b,
                "poklapanje": round(poklapanje, 2),
                "cena_mp": float(cene.mp) if cene else None,
                "nabavna": float(cene.nab) if cene and cene.nab else None,
            })
    nadjeni.sort(key=lambda x: -x["poklapanje"])
    return nadjeni[:najvise]


# Predlog maloprodajne cene se daje samo kad je kod tog dobavljaca marza uska.
#
# Mereno 03.10. na 21.851 maloprodajnoj stavki: globalna medijana je 45 %, ali
# srednjih 50 % slucajeva ide od 34 % do 58 %, a po kategorijama od 33 % do 95 %.
# Po dobavljacu je kod nekih vrlo usko (Vimport 31-33 %, Candy 33-39 %, CSM
# 36-43 %), kod drugih nije (Coffee Center 69-134 %, Linkom 61-86 %), a Roaming
# Electronics ima **negativnu** medijanu - prodaje ispod nabavne.
#
# Zato: predlog samo kad je usko. Pogresan broj u polju je gori od praznog, jer
# covek prihvati ono sto vidi.
PRAG_STAVKI = 20          # koliko stavki dobavljac mora da ima u istoriji
PRAG_RASPONA = 0.10       # 25-75 percentil mora da stane u 10 procentnih poena
ZAOKRUZI_NA = 100         # 86 % maloprodajnih cena je zaokruzeno na 100 dinara


def predlog_cene(
    conn: Connection, dobavljac: str | None, nabavna: float | None, stopa: float
) -> dict | None:
    """Maloprodajna cena po tome kako taj dobavljac obicno ide. None kad nije sigurno.

    Vraca i brojke od kojih je nastao, da covek vidi na cemu stoji, i razlog kad
    predloga nema.
    """
    if not dobavljac or not nabavna or nabavna <= 0:
        return None

    marze = []
    for r in conn.execute(
        text("SELECT s.NAB_CENA nab, s.MALACENA mp, s.STOPA st FROM dbo.MPRULK s "
             "JOIN dbo.MKALKUL z ON z.MAGACIN=s.MAGACIN AND z.BROJ=s.BROJ "
             "WHERE RTRIM(z.DOBAVLJAC)=:d AND s.NAB_CENA > 0 AND s.MALACENA > 0"),
        {"d": dobavljac},
    ):
        m = (float(r.mp) / (1 + float(r.st or 20) / 100.0)) / float(r.nab) - 1
        if -0.5 < m < 5:
            marze.append(m)

    if len(marze) < PRAG_STAVKI:
        return {"cena": None, "n": len(marze),
                "razlog": f"kod ovog dobavljača ima svega {len(marze)} ranijih stavki "
                          f"— nemam na čemu da zasnujem predlog"}

    marze.sort()
    p25 = marze[len(marze) // 4]
    p50 = marze[len(marze) // 2]
    p75 = marze[(len(marze) * 3) // 4]
    if p75 - p25 > PRAG_RASPONA:
        return {"cena": None, "n": len(marze), "p25": p25, "p75": p75,
                "razlog": f"kod ovog dobavljača marža ide od {p25:.0%} do {p75:.0%} "
                          f"— ne predlažem, upiši sam"}

    cena = float(nabavna) * (1 + p50) * (1 + stopa / 100.0)
    return {
        "cena": round(cena / ZAOKRUZI_NA) * ZAOKRUZI_NA or ZAOKRUZI_NA,
        "n": len(marze), "p25": p25, "p50": p50, "p75": p75,
        "razlog": f"medijana marže {p50:.0%} ({p25:.0%}–{p75:.0%} na {len(marze)} stavki)",
    }


def predlozi(
    document_id: int, line_no: int, *, engine: Engine | None = None
) -> PredlogArtikla:
    """Sklapa predlog iz jedne stavke fakture."""
    eng = engine or get_erp_engine()
    with session_scope() as session, eng.connect() as conn:
        doc = session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"Dokument #{document_id} ne postoji.")
        linija = next((x for x in doc.lines if x.line_no == line_no), None)
        if linija is None:
            raise ValueError(f"Stavka {line_no} ne postoji na dokumentu #{document_id}.")

        predlog = PredlogArtikla(document_id=document_id, line_no=line_no)
        predlog.naziv = (linija.name or "").strip()[:60]
        predlog.barkod = (linija.standard_item_id or "").strip()
        predlog.nabavna_cena = linija.price

        jedinica = (linija.unit_code or "").strip().upper()
        predlog.jedmere = JEDINICE.get(jedinica, "kom")
        if jedinica and jedinica not in JEDINICE:
            predlog.napomene.append(
                f"Jedinica mere „{jedinica}“ nije prepoznata — predloženo „kom“."
            )

        stopa = float(linija.vat_percent) if linija.vat_percent is not None else 20.0
        predlog.pdv_stopa = stopa
        predlog.tarifa = TARIFE.get(int(stopa), "002")
        if int(stopa) not in TARIFE:
            predlog.napomene.append(f"PDV {stopa} % nema svoju tarifu — predloženo 20 %.")

        if not predlog.barkod:
            # "Nema EAN na fakturi" NIJE isto sto i "artikal nema barkod".
            # Zato i postoji slikanje ambalaze. Barkod se ne izmislja: baza ga
            # trazi jedinstvenim po magacinu (UQ na SIFRAMAG+BARCODE), pa bi
            # prazan odmah pao ili zauzeo tudje mesto.
            predlog.napomene.append(
                "Faktura nema EAN — upiši barkod sa slike ambalaže. "
                "Samo ako artikal zaista nema barkod, označi to izričito."
            )
        if not predlog.naziv:
            predlog.napomene.append("Stavka nema naziv na fakturi.")

        # Isti barkod vec postoji? Onda sifru ne treba otvarati nego povezati.
        if predlog.barkod:
            red = conn.execute(
                text("SELECT TOP 1 RTRIM(ARTIKAL) a, RTRIM(NAZIV) n FROM dbo.ARTIKLI "
                     "WHERE RTRIM(SIFRAMAG)=:m AND RTRIM(ISNULL(BARCODE,''))=:b"),
                {"m": MATICNI_MAGACIN, "b": predlog.barkod},
            ).first()
            if red is not None:
                predlog.sifra = red.a
                predlog.napomene.append(
                    f"Ovaj barkod već ima šifru {red.a} („{red.n}“) — "
                    f"ne otvarati novu, nego povezati."
                )

        # Kad EAN-a nema, jedina provera je naziv. Mereno 03.10.: u 099 ima
        # 43.531 artikal i 43.432 razlicita naziva, pa je laznih uzbuna malo.
        if predlog.sifra is None and predlog.naziv:
            predlog.slicni = slicni_artikli(conn, predlog.naziv)
            if predlog.slicni:
                predlog.napomene.append(
                    "Postoji artikal sličnog naziva — proveri da ne otvaraš drugi put."
                )

        if predlog.sifra is None:
            predlog.predlozena_sifra = slobodna_sifra(conn)
            if predlog.predlozena_sifra is None:
                predlog.napomene.append(
                    "Nema slobodnog bloka šifara ispod 100.000 — šifru odrediti ručno."
                )

        from .kalkulacija import _analitika_dobavljaca

        dobavljac = _analitika_dobavljaca(
            conn, doc.supplier_reg_no, doc.supplier_vat, doc.payment_account
        )
        predlog.cena_predlog = predlog_cene(
            conn, dobavljac, predlog.nabavna_cena, predlog.pdv_stopa
        )
        if predlog.cena_predlog and predlog.cena_predlog.get("cena"):
            predlog.cena_mp = predlog.cena_predlog["cena"]

        predlog.magacini, predlog.prodajna_mesta = _mesta(conn)
        return predlog


def redovi(predlog: PredlogArtikla) -> dict[str, list[dict]]:
    """Redovi koje bi trebalo upisati, po tabelama.

    `DARTIKLI` namerno nije tu - nju puni nocni posao na serveru, vidi vrh
    modula.
    """
    sifra = predlog.sifra or predlog.predlozena_sifra
    if not sifra:
        raise ValueError("Šifra artikla nije određena.")
    barkod = predlog.barkod_za_upis
    if not barkod:
        raise ValueError(
            "Barkod nije poznat. Upiši ga sa slike ambalaže, ili izričito označi "
            "da artikal nema barkod."
        )
    if not predlog.cena_mp or predlog.cena_mp <= 0:
        raise ValueError(
            "Maloprodajna cena mora da se unese — bez nje kalkulacija u "
            "maloprodaji staje sa „artikal nema cenu u objektu“."
        )

    osnova = {
        "ARTIKAL": sifra,
        "NAZIV": predlog.naziv[:30],
        "BARCODE": barkod,
        "JEDMERE": predlog.jedmere,
        "TARIFA": predlog.tarifa,
        **STALNO_ARTIKLI,
    }
    artikli = [{**osnova, "SIFRAMAG": m} for m in predlog.magacini]
    artprod = [
        {
            "SIFRAPRO": m,
            "SIFRAMAG": MATICNI_MAGACIN,
            "ARTIKAL": sifra,
            "NAZIV": predlog.naziv[:30],
            "BARCODE": barkod,
            "JEDMERE": predlog.jedmere,
            "TARIFA": predlog.tarifa,
            "CENAMALO": round(float(predlog.cena_mp), 2),
            "NABCENA": round(float(predlog.nabavna_cena or 0), 2),
            "DECCENA": 2,
            "DECKOLIC": 2,
            "PROIZVODJAC": ".",
        }
        for m in predlog.prodajna_mesta
    ]
    return {"ARTIKLI": artikli, "ARTPROD": artprod}


def kao_sql(predlog: PredlogArtikla) -> str:
    """INSERT naredbe, da se pre upisa moze pogledati sta bi se desilo."""

    def vrednost(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    delovi = []
    for tabela, lista in redovi(predlog).items():
        for red in lista:
            kolone = ", ".join(red)
            vrednosti = ", ".join(vrednost(v) for v in red.values())
            delovi.append(f"INSERT INTO dbo.{tabela} ({kolone}) VALUES ({vrednosti});")
    return "\n".join(delovi)


def upisi(predlog: PredlogArtikla, *, produkcija: bool = False) -> str:
    """Otvara sifru u ERP-u. Vraca opis onoga sto je upisano.

    Jedna transakcija: ili sve ili nista. Prvo jedinstveni sifarnik (099), pa
    lokalni cenovnici - bez njih kalkulacija ne bi mogla da se napravi, a
    ARTPROD bez `CENAMALO` staje sa "artikal nema cenu u objektu".

    Sifra se proverava **u trenutku upisa**, ne samo kad je predlog pravljen:
    izmedju jednog i drugog moze da prodje i pola sata, a sifru u medjuvremenu
    uzme neko drugi. Isto vazi za barkod - baza ga trazi jedinstvenim po
    magacinu (UQ na SIFRAMAG+BARCODE).
    """
    if not predlog.spreman:
        raise ValueError("Predlog nije potpun — fale šifra, naziv, barkod ili cena.")

    redovi_po_tabeli = redovi(predlog)
    sifra = predlog.sifra or predlog.predlozena_sifra
    barkod = predlog.barkod_za_upis
    engine, baza = erp_engine_za_upis(produkcija)

    with engine.begin() as conn:
        if zauzeta(conn, sifra):
            raise ValueError(f"Šifra {sifra} je u međuvremenu zauzeta — osveži predlog.")
        zauzet = conn.execute(
            text("SELECT TOP 1 RTRIM(ARTIKAL) a FROM dbo.ARTIKLI "
                 "WHERE RTRIM(SIFRAMAG)=:m AND RTRIM(ISNULL(BARCODE,''))=:b"),
            {"m": MATICNI_MAGACIN, "b": barkod},
        ).first()
        if zauzet is not None:
            raise ValueError(
                f"Barkod {barkod} već nosi šifra {zauzet.a} — ne otvaraj novu, nego poveži."
            )

        upisano = 0
        for tabela, redovi_tabele in redovi_po_tabeli.items():
            for red in redovi_tabele:
                kolone = ", ".join(red)
                mesta = ", ".join(f":{k}" for k in red)
                conn.execute(text(f"INSERT INTO dbo.{tabela} ({kolone}) VALUES ({mesta})"), red)
                upisano += 1

    opis = (f"{baza}: artikal {sifra} „{predlog.naziv[:30]}“, barkod {barkod}, "
            f"MP cena {predlog.cena_mp:,.2f} — {upisano} redova "
            f"({len(redovi_po_tabeli['ARTIKLI'])} magacina, "
            f"{len(redovi_po_tabeli['ARTPROD'])} prodajnih mesta)")
    log.info("Otvorena šifra: %s", opis)
    return opis
