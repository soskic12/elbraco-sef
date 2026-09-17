"""Predlog nove sifre artikla iz stavke e-fakture.

Otvaranje sifre je do sada rucni posao: poslovodje slikaju ambalazu u Viber grupu,
Dusan otvara sifru u ERP-u i javi u grupu. Ovde se priprema sve sto se sa fakture
moze znati, da bi mu ostalo samo da pogleda i klikne.

Sta ide gde - mereno 17.09.2026. na 300 najnovijih sifara:

    ARTIKLI    maticni red (SIFRAMAG '099') + kopija po magacinu; novi artikal
               dobije 18 redova, a u 5 magacina ('001','006','012','029','099')
               je vise od 90 % njih
    ARTPROD    red po prodajnom mestu - 21 mesto kod vecine novih
    DARTIKLI   tacno jedan red po artiklu (dopuna: opis, slike, uvoznik)

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
from .connection import get_erp_engine

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

    @property
    def broj_redova(self) -> int:
        """Koliko redova bi nastalo: ARTIKLI + ARTPROD + jedan DARTIKLI."""
        return len(self.magacini) + len(self.prodajna_mesta) + 1


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


def _mesta(conn: Connection, prag: float = 0.9) -> tuple[list[str], list[str]]:
    """Magacini i prodajna mesta u koja idu kopije novog artikla.

    Ne nagadja se: uzima se gde su zavrsili poslednji otvoreni artikli, i to samo
    ono sto se ponavlja kod bar `prag` njih. Nova prodavnica tako udje sama, a
    ugasena ispadne.
    """
    novi = """
        select top 300 RTRIM(ARTIKAL) a from dbo.ARTIKLI
        where RTRIM(SIFRAMAG)='099' and TRY_CAST(RTRIM(ARTIKAL) as bigint) is not null
        order by TRY_CAST(RTRIM(ARTIKAL) as bigint) desc
    """
    granica = int(300 * prag)
    magacini = [
        r.m for r in conn.execute(
            text(f"""select RTRIM(SIFRAMAG) m from dbo.ARTIKLI
                     where RTRIM(ARTIKAL) in ({novi})
                     group by RTRIM(SIFRAMAG) having count(*) >= :g order by RTRIM(SIFRAMAG)"""),
            {"g": granica},
        )
    ]
    mesta = [
        r.m for r in conn.execute(
            text(f"""select RTRIM(SIFRAPRO) m from dbo.ARTPROD
                     where RTRIM(ARTIKAL) in ({novi})
                     group by RTRIM(SIFRAPRO) having count(*) >= :g order by RTRIM(SIFRAPRO)"""),
            {"g": granica},
        )
    ]
    return magacini, mesta


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
            predlog.napomene.append(
                "Faktura nema EAN — poslovođa treba da pošalje sliku ambalaže."
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

        if predlog.sifra is None:
            predlog.predlozena_sifra = slobodna_sifra(conn)
            if predlog.predlozena_sifra is None:
                predlog.napomene.append(
                    "Nema slobodnog bloka šifara ispod 100.000 — šifru odrediti ručno."
                )

        predlog.magacini, predlog.prodajna_mesta = _mesta(conn)
        return predlog


def redovi(predlog: PredlogArtikla) -> dict[str, list[dict]]:
    """Redovi koje bi trebalo upisati, po tabelama. Bez sifre se ne moze."""
    sifra = predlog.sifra or predlog.predlozena_sifra
    if not sifra:
        raise ValueError("Šifra artikla nije određena.")

    osnova = {
        "ARTIKAL": sifra,
        "NAZIV": predlog.naziv,
        "BARCODE": predlog.barkod,
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
            "NAZIV": predlog.naziv,
            "BARCODE": predlog.barkod,
            "JEDMERE": predlog.jedmere,
            "TARIFA": predlog.tarifa,
            "DECCENA": 2,
            "DECKOLIC": 2,
            "PROIZVODJAC": ".",
        }
        for m in predlog.prodajna_mesta
    ]
    dartikli = [
        {
            "SIFRAMAG": MATICNI_MAGACIN,
            "ARTIKAL": sifra,
            "NAZIV": predlog.naziv,
            "BARCODE": predlog.barkod,
            "PROIZVODJAC": ".",
            "CENA": predlog.nabavna_cena or 0.0,
        }
    ]
    return {"ARTIKLI": artikli, "ARTPROD": artprod, "DARTIKLI": dartikli}


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
