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

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Connection, Engine

from ..db import session_scope
from ..models import Document, DocumentLine, DocumentType, ItemMapping, UnitKind
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
            nacrt.problemi.append(
                f"Dobavljač {doc.supplier_name} (MB {doc.supplier_reg_no}) ne postoji u NAZIVI."
            )

        broj_racuna = broj_racuna_za_erp(doc.document_number)
        broj = sledeci_broj(conn, tabela_z, magacin)
        # Knjizno odobrenje ulazi minusom - kolicine i iznosi su negativni.
        predznak = -1.0 if (doc.document_type and "credit" in str(doc.document_type.value).lower()) else 1.0

        # Kalkulacija na magacinu 088 nema stavke. Nije stvar izbora nego pravilo
        # ERP-a: mereno 16.09., svih 3.016 zaglavlja na 088 nema nijedan red u
        # PRULKALK, a ROBNO_ZADUZ je u svima 0. Bez ovoga generator trazi sifru
        # artikla za "Naknada za transakcije na POS terminalu" - a takva sifra
        # nikad nece ni postojati, pa bi kalkulacija zauvek cekala coveka.
        bez_stavki = magacin == MAGACIN_ULAZNI_RACUN

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

        for linija in linije:
            if linija.nije_artikal:
                # Covek je rekao da to nije artikal - ne trazi se sifra i ne ulazi
                # u kalkulaciju. Vidi se na strani dokumenta, uz stavku.
                continue
            sifra, odakle = _nasa_sifra(session, conn, doc.supplier_vat, linija)
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
                    }
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
            pdv_ukupno = float(doc.vat_amount or 0) * predznak
            za_naplatu = round(float(doc.amount or 0) * predznak, 2)
            for linija in doc.lines:
                if linija.vat_percent is None or linija.line_amount is None:
                    continue
                osn = round(float(linija.line_amount) * predznak, 2)
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

        rok = clarion_dan(doc.due_date) if doc.due_date else 0
        nacrt.zaglavlje = {
            "LASTUSER": 1,
            "MAGACIN": magacin,
            "VRSTAKNJIZ": "U",
            "BROJ": broj,
            "DATUM": dan_prometa,
            "KONTO": "435",
            "DOBAVLJAC": dobavljac or "",
            "BROJ_RN_DOB": broj_racuna,
            "DAT_RAC_DOB": dan_prometa,
            "DATUM_PRIJEM": dan_prometa,
            "ROBNO_ZADUZ": round(osnovica, 2),
            "PDV": pdv_opsta,
            "PRENETIPOREZ": pdv_posebna,
            "IZNOS_ZA_NAP": za_naplatu,
            "MENJA_CENU": "Da" if maloprodaja else "",
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


def postojeca_kalkulacija(conn: Connection, broj_racuna: str, dobavljac: str) -> str | None:
    """Da li za taj racun tog dobavljaca vec postoji kalkulacija u ERP-u.

    Poslovodje i dalje prave kalkulacije rukom, pa app mora da proveri pre upisa -
    dve kalkulacije za istu fakturu su gore nego nijedna. Poredi se po
    normalizovanom broju (`FA-5543-03/26` == `FA55430326`), jer covek crtice i
    kose crte kuca kako mu dodje.
    """
    from ..services.mapiranje import kljuc_racuna

    trazeni = kljuc_racuna(broj_racuna)
    if not trazeni or not dobavljac:
        return None
    for tabela in ("ULKALKUL", "MKALKUL"):
        for red in conn.execute(
            text(
                f"SELECT RTRIM(BROJ) br, RTRIM(MAGACIN) mag, RTRIM(ISNULL(BROJ_RN_DOB,'')) rn "
                f"FROM dbo.{tabela} WHERE RTRIM(DOBAVLJAC) = :dob"
            ),
            {"dob": dobavljac.strip()},
        ):
            if red.rn and kljuc_racuna(red.rn) == trazeni:
                return f"{tabela} {red.mag}/{red.br}"
    return None


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
    return f"{baza}: {nacrt.tabela_zaglavlja} {nacrt.magacin}/U/{broj}, stavki {len(nacrt.stavke)}"
