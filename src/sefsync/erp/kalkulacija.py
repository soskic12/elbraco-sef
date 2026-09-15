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
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..db import session_scope
from ..models import Document, DocumentLine, ItemMapping, UnitKind
from .connection import erp_engine_za_upis, get_erp_engine

log = logging.getLogger(__name__)

# Clarion broji dane od ovog datuma.
CLARION_NULA = dt.date(1800, 12, 28)

MAGACIN_ULAZNI_RACUN = "088"
IZVOR = "Elektronske fakture"

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
PROKNJIZENO_NIJE = 0    # otvorena kalkulacija, ceka poslovodju
PROKNJIZENO_JESTE = 1   # zaknjizeno u ERP-u - NIKAD iz aplikacije
PROKNJIZENO_ZAKLJUCANO = 2  # administrator zakljucao stare kalkulacije


def clarion_dan(d: dt.date) -> int:
    return (d - CLARION_NULA).days


def clarion_datum(n: int) -> dt.date:
    return CLARION_NULA + dt.timedelta(days=n)


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
    nemapirane: list[str] = field(default_factory=list)

    @property
    def spreman(self) -> bool:
        return not self.problemi and not self.nemapirane


# --------------------------------------------------------------------------- #
# citanje iz ERP-a
# --------------------------------------------------------------------------- #


def _analitika_dobavljaca(conn: Connection, maticni: str | None, pib: str | None) -> str | None:
    """NAZIVI.ANALITIKA - veza je maticni broj, PIB u toj tabeli nije popunjen."""
    if maticni:
        red = conn.execute(
            text("SELECT TOP 1 RTRIM(ANALITIKA) a FROM dbo.NAZIVI WHERE RTRIM(MATBROJ)=:m"),
            {"m": maticni.strip()},
        ).first()
        if red:
            return red.a
    if pib:
        red = conn.execute(
            text("SELECT TOP 1 RTRIM(ANALITIKA) a FROM dbo.NAZIVI WHERE RTRIM(ISNULL(BPG,''))=:p"),
            {"p": pib.strip()},
        ).first()
        if red:
            return red.a
    return None


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


def _nasa_sifra(session, conn: Connection, pib: str | None, linija: DocumentLine) -> tuple[str | None, str]:
    """Vraca (sifra, odakle). Redosled po pouzdanosti - vidi docs/FAZA-2.md."""
    if pib and linija.sellers_item_id:
        m = (
            session.query(ItemMapping)
            .filter(
                ItemMapping.supplier_vat == pib,
                ItemMapping.supplier_item_id == linija.sellers_item_id.strip(),
                ItemMapping.active == True,  # noqa: E712 - MS SQL nema IS TRUE
            )
            .first()
        )
        if m is not None:
            return m.erp_item_code, "mapiranje"

    if linija.standard_item_id:
        redovi = conn.execute(
            text(
                "SELECT DISTINCT RTRIM(ARTIKAL) a FROM dbo.ARTIKLI "
                "WHERE RTRIM(ISNULL(BARCODE,''))=:b AND RTRIM(SIFRAMAG)='099'"
            ),
            {"b": linija.standard_item_id.strip()},
        ).all()
        if len(redovi) == 1:
            return redovi[0].a, "barkod"
        if len(redovi) > 1:
            return None, "barkod vodi na vise sifara"

    return None, "nepoznat"


# --------------------------------------------------------------------------- #
# gradjenje nacrta
# --------------------------------------------------------------------------- #


def pripremi(
    document_id: int,
    korisnik: str,
    *,
    datum: dt.date | None = None,
    engine: Engine | None = None,
) -> Nacrt:
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

        dobavljac = _analitika_dobavljaca(conn, doc.supplier_reg_no, doc.supplier_vat)
        if dobavljac is None:
            nacrt.problemi.append(
                f"Dobavljač {doc.supplier_name} (MB {doc.supplier_reg_no}) ne postoji u NAZIVI."
            )

        broj_racuna = broj_racuna_za_erp(doc.document_number)
        broj = sledeci_broj(conn, tabela_z, magacin)
        # Knjizno odobrenje ulazi minusom - kolicine i iznosi su negativni.
        predznak = -1.0 if (doc.document_type and "credit" in str(doc.document_type.value).lower()) else 1.0

        linije = (
            session.query(DocumentLine)
            .filter(DocumentLine.document_id == document_id)
            .order_by(DocumentLine.line_no)
            .all()
        )

        osnovica = pdv_ukupno = mp_vrednost = mp_pdv = 0.0
        trenutak = dt.datetime.now()

        for linija in linije:
            sifra, odakle = _nasa_sifra(session, conn, doc.supplier_vat, linija)
            if sifra is None:
                nacrt.nemapirane.append(
                    f"stavka {linija.line_no}: {linija.name or '-'} "
                    f"(šifra dob. {linija.sellers_item_id or '-'}, EAN {linija.standard_item_id or '-'})"
                )
                continue
            art = _artikal(conn, sifra)
            if art is None:
                nacrt.problemi.append(f"stavka {linija.line_no}: šifra {sifra} ne postoji u ARTIKLI/099")
                continue

            tarifa = (art["tarifa"] or "002").strip()
            stopa = _stopa(conn, tarifa)
            kolicina = float(linija.quantity or 0) * predznak
            kol_abs = abs(kolicina) or 1.0

            # Mereno na 4.425 stavki sa rabatom: FAK_CENA je bruto cena,
            # NAB_CENA neto, IZN_RABATA rabat po komadu, VREDNOST neto x kolicina.
            bruto = float(linija.price or 0)
            neto = (
                round(float(linija.line_amount) / kol_abs, 4)
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
                "DATUM": dan,
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
            nacrt.stavke.append(stavka)

        rok = clarion_dan(doc.due_date) if doc.due_date else 0
        nacrt.zaglavlje = {
            "LASTUSER": 1,
            "MAGACIN": magacin,
            "VRSTAKNJIZ": "U",
            "BROJ": broj,
            "DATUM": dan,
            "KONTO": "435",
            "DOBAVLJAC": dobavljac or "",
            "BROJ_RN_DOB": broj_racuna,
            "DAT_RAC_DOB": clarion_dan(doc.issue_date) if doc.issue_date else dan,
            "DATUM_PRIJEM": dan,
            "ROBNO_ZADUZ": round(osnovica, 2),
            "PDV": round(pdv_ukupno, 2),
            "IZNOS_ZA_NAP": round(osnovica + pdv_ukupno, 2),
            "MENJA_CENU": "Da" if maloprodaja else "",
            "OBRACUN_MALO": "",
            "MODEL": 0,
            "VALUTA": rok,
            "PROKNJIZENO": PROKNJIZENO_NIJE,
            "OZNAKA": "EUR",
            "TM": "000000",
            "IZVOREPP": IZVOR,
            "STAVKE": "Da" if (nacrt.stavke and not maloprodaja) else "",
        }
        if maloprodaja:
            nacrt.zaglavlje["IZN_RUCM"] = round(mp_vrednost - mp_pdv - osnovica, 2)
            nacrt.zaglavlje["UKAL_PDV2"] = round(mp_pdv, 2)
            nacrt.zaglavlje["MPVRED2"] = round(mp_vrednost, 2)

        return nacrt


# --------------------------------------------------------------------------- #
# ispis i upis
# --------------------------------------------------------------------------- #


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


def upisi(nacrt: Nacrt, *, produkcija: bool = False) -> str:
    """Upisuje nacrt. Podrazumevano u test bazu; produkcija trazi dozvolu."""
    if not nacrt.spreman:
        prepreke = nacrt.problemi + [f"nemapirano: {x}" for x in nacrt.nemapirane]
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
    with eng.begin() as conn:
        # Broj se uzima ponovo unutar transakcije - izmedju pripreme i upisa
        # neko je mogao da otvori kalkulaciju u ERP-u.
        broj = sledeci_broj(conn, nacrt.tabela_zaglavlja, nacrt.magacin)
        nacrt.zaglavlje["BROJ"] = broj
        for s in nacrt.stavke:
            s["BROJ"] = broj

        kolone = list(nacrt.zaglavlje)
        conn.execute(
            text(
                f"INSERT INTO dbo.{nacrt.tabela_zaglavlja} ({', '.join(kolone)}) "
                f"VALUES ({', '.join(':' + k for k in kolone)})"
            ),
            nacrt.zaglavlje,
        )
        for s in nacrt.stavke:
            vidljive = {k: v for k, v in s.items() if not k.startswith("_")}
            kolone = list(vidljive)
            conn.execute(
                text(
                    f"INSERT INTO dbo.{nacrt.tabela_stavki} ({', '.join(kolone)}) "
                    f"VALUES ({', '.join(':' + k for k in kolone)})"
                ),
                vidljive,
            )
    log.info("Kalkulacija %s/%s upisana u %s", nacrt.magacin, broj, baza)
    return f"{baza}: {nacrt.tabela_zaglavlja} {nacrt.magacin}/U/{broj}, stavki {len(nacrt.stavke)}"
