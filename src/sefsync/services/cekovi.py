"""Cekovi gradjana: slikanje, citanje, sef, predaja u banku, vraceni.

Bane, 16.10.: „kod nas je vodjenje cekova pesacki posao“. Poslovodja svako
jutro uslika cekove iz jucerasnjeg pazara; aplikacija ih procita i upise, a
polja u koja nije sigurna zacrveni da ih poslovodja upise rukom.

Sta je izmereno na cetiri prava ceka (UniCredit, Erste x2, OTP) pre pisanja:

* Obrazac 19-c je isti kod svih banaka. **Serijski broj i racun izdavaoca su
  stampani masinski** u donjoj traci, pa se citaju skoro bez greske.
* Racun ima **kontrolni broj** (poslednje dve cifre, ISO 7064 mod 97): sva
  cetiri procitana racuna ga prolaze. Pogresno procitan racun se tako hvata
  bez coveka.
* Erste stampa partiju bez vodecih nula (`340 0032427249 65`) - dopunjuje se
  na 13 cifara pre provere.
* Iznos pise dvaput (ciframa i slovima), pa se to dvoje uporedjuje. Datum je
  jedino rucno polje bez kontrole - njega najcesce treba potvrditi.
* Gornji „racun br. 908-…“ je obracunski racun banke, ne kupca - ne koristi se.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import (
    AuditLog,
    Cek,
    PredajaCekova,
    SlikaCekova,
    StanjeCeka,
    utcnow,
)

log = logging.getLogger(__name__)

IZNOS_PO_PRAVILU = 5000.0          # prazan iznos na ceku (Bane, 16.10.)
NAJVECI_RAZUMAN_IZNOS = 1_000_000  # vise od ovoga je skoro sigurno pogresno procitano
DUZINA_SERIJSKOG = 13              # sve cetiri banke sa uzorka
NAJDUZA_STRANA = 2000              # piksela - dovoljno za rukopis, ispod granice API-ja

POLJA = ("serijski_broj", "racun", "iznos", "datum")


# --------------------------------------------------------------------------- #
# provere koje ne zavise od modela
# --------------------------------------------------------------------------- #


def kontrolni_ok(racun: str) -> bool:
    """Kontrolni broj racuna (poslednje dve cifre), ISO 7064 mod 97-10."""
    if not re.fullmatch(r"\d{18}", racun or ""):
        return False
    return 98 - (int(racun[:-2]) * 100) % 97 == int(racun[-2:])


def normalizuj_racun(procitano: str | None) -> str | None:
    """18 cifara bez razmaka i crtica, ili None ako se ne moze sloziti.

    Partija se dopunjuje nulama do 13 cifara kad je stampana skraceno (Erste).
    Kad su delovi razdvojeni razmakom ili crticom, deli se po njima; inace se
    uzimaju prve tri cifre za banku i poslednje dve za kontrolni broj.
    """
    if not procitano:
        return None
    delovi = [d for d in re.split(r"[\s\-]+", procitano.strip()) if d]
    if len(delovi) == 3 and all(d.isdigit() for d in delovi):
        banka, partija, kontrolni = delovi
    else:
        cifre = re.sub(r"\D", "", procitano)
        if len(cifre) < 6:
            return None
        banka, partija, kontrolni = cifre[:3], cifre[3:-2], cifre[-2:]
    if len(banka) != 3 or len(kontrolni) != 2 or not partija or len(partija) > 13:
        return None
    return banka + partija.zfill(13) + kontrolni


def lep_racun(racun: str | None) -> str:
    if not racun or len(racun) != 18:
        return racun or ""
    return f"{racun[:3]}-{racun[3:16]}-{racun[16:]}"


def rok_predaje(datum: date | None) -> date | None:
    """Kad cek ide u banku: datum sa ceka, a vikend -> prvi radni dan posle."""
    if datum is None:
        return None
    if datum.weekday() == 5:
        return datum + timedelta(days=2)
    if datum.weekday() == 6:
        return datum + timedelta(days=1)
    return datum


@dataclass
class Provera:
    """Ishod nase provere jednog procitanog ceka."""

    serijski_broj: str
    racun: str
    iznos: float
    iznos_po_pravilu: bool
    datum: date | None
    sumnjiva: list[str] = field(default_factory=list)
    razlozi: list[str] = field(default_factory=list)


def proveri(procitan: dict, *, primljeno: date | None, oznaka: str) -> Provera:
    """Pretvara ono sto je model procitao u cek, i sam odlucuje sta je sumnjivo.

    Model kaze gde nije siguran, ali se na to ne oslanja samo: racun mora da
    prodje kontrolni broj, iznos ciframa i slovima moraju da se slazu, a datum
    mora da bude razuman u odnosu na dan prijema.
    """
    sigurno = procitan.get("sigurno") or {}
    sumnjiva: list[str] = []
    razlozi: list[str] = []

    def sumnja(polje: str, razlog: str) -> None:
        if polje not in sumnjiva:
            sumnjiva.append(polje)
        razlozi.append(razlog)

    # -- serijski broj (stampan)
    serijski = re.sub(r"\D", "", procitan.get("serijski_broj") or "")
    if not serijski:
        sumnja("serijski_broj", "serijski broj nije pročitan")
        serijski = f"?{oznaka}"
    elif len(serijski) != DUZINA_SERIJSKOG:
        sumnja("serijski_broj", f"serijski broj ima {len(serijski)} cifara, očekivano 13")
    elif not sigurno.get("serijski_broj", True):
        sumnja("serijski_broj", "serijski broj nije jasno čitljiv")

    # -- racun (stampan, sa kontrolnim brojem)
    racun = normalizuj_racun(procitan.get("racun"))
    if racun is None:
        sumnja("racun", "broj računa nije pročitan")
        racun = f"?{oznaka}"[:18]
    elif not kontrolni_ok(racun):
        sumnja("racun", "kontrolni broj računa ne odgovara — neka cifra je pogrešno pročitana")

    # -- iznos (rucno, dvaput)
    ciframa = procitan.get("iznos_ciframa")
    slovima = procitan.get("iznos_slovima")
    po_pravilu = False
    if ciframa is None and slovima is None:
        iznos, po_pravilu = IZNOS_PO_PRAVILU, True
    elif ciframa is not None and slovima is not None:
        iznos = float(ciframa)
        if abs(float(ciframa) - float(slovima)) > 0.005:
            sumnja("iznos", f"ciframa piše {float(ciframa):,.2f}, a slovima {float(slovima):,.2f}")
    else:
        iznos = float(ciframa if ciframa is not None else slovima)
        if not sigurno.get("iznos", True):
            sumnja("iznos", "iznos nije jasno čitljiv")
    if not po_pravilu and not sigurno.get("iznos", True) and "iznos" not in sumnjiva:
        sumnja("iznos", "iznos nije jasno čitljiv")
    if iznos <= 0 or iznos > NAJVECI_RAZUMAN_IZNOS:
        sumnja("iznos", f"iznos {iznos:,.2f} nije verovatan")

    # -- datum (rucno, bez kontrole)
    datum = None
    tekst_datuma = procitan.get("datum")
    if tekst_datuma:
        try:
            datum = date.fromisoformat(tekst_datuma)
        except ValueError:
            datum = None
    if datum is None:
        sumnja("datum", "datum nije pročitan")
    else:
        osnova = primljeno or date.today()
        if datum < osnova - timedelta(days=45) or datum > osnova + timedelta(days=800):
            sumnja("datum", f"datum {datum:%d.%m.%Y} je neobično daleko od dana prijema")
        elif not sigurno.get("datum", True):
            sumnja("datum", "datum nije jasno čitljiv")

    return Provera(serijski, racun, round(iznos, 2), po_pravilu, datum, sumnjiva, razlozi)


# --------------------------------------------------------------------------- #
# slika
# --------------------------------------------------------------------------- #


def sacuvaj_sliku(
    business_unit_id: int,
    sadrzaj: bytes,
    *,
    mime: str | None,
    naziv_fajla: str | None,
    korisnik: str,
    primljeno: date | None,
) -> int:
    """Upisuje fotografiju cekova; vraca njen id. Citanje ide posebno."""
    from .slike import DOZVOLJENE, NAJVECA, _bezbedno_ime, _po_sadrzaju

    if not sadrzaj:
        raise ValueError("Prazan fajl.")
    if len(sadrzaj) > NAJVECA:
        raise ValueError(
            f"Slika je {len(sadrzaj) / (1024 * 1024):.1f} MB, a najviše sme "
            f"{NAJVECA // (1024 * 1024)} MB."
        )
    nastavak = DOZVOLJENE.get((mime or "").lower().split(";")[0].strip()) or _po_sadrzaju(sadrzaj)
    if nastavak is None:
        raise ValueError("Dozvoljene su samo slike (JPG, PNG, WEBP).")

    sada = datetime.now(timezone.utc)
    folder = get_settings().storage_dir / "cekovi" / f"{sada.year:04d}" / f"{sada.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    putanja = folder / f"{business_unit_id}-{sada.strftime('%d%H%M%S%f')}{nastavak}"
    putanja.write_bytes(sadrzaj)

    with session_scope() as session:
        red = SlikaCekova(
            business_unit_id=business_unit_id,
            putanja=str(putanja),
            naziv_fajla=_bezbedno_ime(naziv_fajla),
            primljeno=primljeno,
            poslao=korisnik,
            poslato_u=utcnow(),
        )
        session.add(red)
        session.flush()
        return red.id


def slika_za_claude(sadrzaj: bytes) -> tuple[str, str]:
    """(media_type, base64) - okrenuta po EXIF-u i smanjena.

    Telefonska slika je 4-5 MB i 4608 piksela; API prima najvise 5 MB po slici
    posle kodiranja, a rukopis se cita i na 2000 piksela.
    """
    from PIL import Image, ImageOps

    try:
        slika = Image.open(io.BytesIO(sadrzaj))
        slika = ImageOps.exif_transpose(slika)
    except Exception as exc:  # noqa: BLE001 - HEIC i sl. Pillow ne cita
        raise ValueError(
            "Ovaj format slike ne može da se pročita — slikaj ponovo kao JPG "
            "(na iPhone-u: Podešavanja → Kamera → Formati → Najkompatibilnije)."
        ) from exc
    slika = slika.convert("RGB")
    slika.thumbnail((NAJDUZA_STRANA, NAJDUZA_STRANA))
    izlaz = io.BytesIO()
    slika.save(izlaz, format="JPEG", quality=88)
    return "image/jpeg", base64.standard_b64encode(izlaz.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
# citanje
# --------------------------------------------------------------------------- #


SISTEM = """\
Čitaš fotografije srpskih čekova građana (obrazac 19-c) koje poslovođa slika \
ujutro. Na jednoj slici može biti jedan ili više čekova, okrenutih bilo kako.

Gde je šta na čeku:
- Donja traka, štampana mašinski: „Serijski broj“ (13 cifara, iza ume da stoji \
znak koji nije cifra — izostavi ga), „Broj tekućeg računa“ (račun izdavaoca; \
ume da bude spojeno 18 cifara ili razdvojeno razmacima, npr. „340 0032427249 \
65“ — prepiši tačno kako piše, sa razmacima), „Iznos“ i „V.pos.“ (npr. 15H — \
ne treba).
- Gore: „Platite po ovom čeku sa računa br. 908-…“ je račun BANKE — NE \
prepisuj ga kao račun.
- Ručno: iznos ciframa u polju „din.“, iznos slovima („slovima“), mesto i \
datum izdavanja. Datum izdavanja je dan naplate.

Pravila:
- Prepiši tačno ono što piše. Ne izmišljaj i ne „popravljaj“ cifre.
- Za iznos slovima vrati broj koji reči znače (npr. „pet hiljada dinara“ → \
5000). Ako polje nije popunjeno, vrati null.
- Čekovi se izdaju unapred, na više meseci i u sledeću godinu — datum u \
budućnosti je normalan i NIJE razlog za nesigurnost. Datumi su gotovo uvek \
5., 10., 15., 20., 25. ili 30. u mesecu.
- U „sigurno“ stavi false samo kad neka cifra ili reč stvarno može da se \
pročita na dva načina (rukopis, odsjaj, preklopljeno, odsečeno), i tada u \
napomeni napiši obe mogućnosti. Rukopis koji je jasan, iako nije lep, je \
siguran.
- Ako na slici nema čeka, vrati praznu listu i u napomeni reci šta jeste."""

SEMA = {
    "type": "object",
    "properties": {
        "cekovi": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "banka": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "serijski_broj": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "racun": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "iznos_ciframa": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "iznos_slovima_tekst": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "iznos_slovima": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "datum": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
                    "mesto": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "sigurno": {
                        "type": "object",
                        "properties": {p: {"type": "boolean"} for p in POLJA},
                        "required": list(POLJA),
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "banka", "serijski_broj", "racun", "iznos_ciframa",
                    "iznos_slovima_tekst", "iznos_slovima", "datum", "mesto", "sigurno",
                ],
                "additionalProperties": False,
            },
        },
        "napomena": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["cekovi", "napomena"],
    "additionalProperties": False,
}


def _klijent():
    from .specifikacija import _klijent as klijent_specifikacije

    return klijent_specifikacije()


def procitaj(media_type: str, podaci: str, *, klijent=None) -> dict:
    """Salje sliku Claude-u i vraca procitano kao recnik po SEMA."""
    import anthropic

    klijent = klijent or _klijent()
    try:
        odgovor = klijent.beta.messages.create(
            model=get_settings().cekovi_model,
            max_tokens=16000,
            system=SISTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": podaci}},
                    {"type": "text", "text": "Pročitaj sve čekove sa ove slike."},
                ],
            }],
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": SEMA},
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Claude nije odgovorio ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError("Nema veze sa Claude-om.") from exc

    if odgovor.stop_reason == "refusal":
        raise RuntimeError("Claude je odbio da pročita sliku.")
    if odgovor.stop_reason == "max_tokens":
        raise RuntimeError("Odgovor je presečen — previše čekova na jednoj slici.")
    tekst = next((b.text for b in odgovor.content if b.type == "text"), "")
    return json.loads(tekst)


def procitaj_sliku(slika_id: int, *, klijent=None) -> dict:
    """Cita jednu sliku i upisuje cekove. Vraca brojac za poruku coveku.

    Ne baca izuzetak: greska ostaje zapisana na slici, da se vidi na ekranu i
    da moze „Procitaj ponovo“.
    """
    with session_scope() as session:
        slika = session.get(SlikaCekova, slika_id)
        if slika is None:
            raise ValueError(f"Slika #{slika_id} ne postoji.")
        putanja, jedinica = slika.putanja, slika.business_unit_id
        primljeno, poslao = slika.primljeno, slika.poslao

    brojac = {"cekova": 0, "novih": 0, "vec_uneto": 0, "za_proveru": 0}
    try:
        with open(putanja, "rb") as f:
            media_type, podaci = slika_za_claude(f.read())
        rezultat = procitaj(media_type, podaci, klijent=klijent)
    except Exception as exc:  # noqa: BLE001 - poruka ide na ekran
        log.warning("Slika cekova #%s nije procitana: %s", slika_id, exc)
        with session_scope() as session:
            s = session.get(SlikaCekova, slika_id)
            s.greska = str(exc)[:500]
            s.procitano_u = utcnow()
        return brojac

    with session_scope() as session:
        for i, procitan in enumerate(rezultat.get("cekovi") or [], start=1):
            brojac["cekova"] += 1
            p = proveri(procitan, primljeno=primljeno, oznaka=f"{slika_id}-{i}")
            postoji = session.scalar(
                select(Cek).where(Cek.racun == p.racun, Cek.serijski_broj == p.serijski_broj)
            )
            if postoji is not None:
                brojac["vec_uneto"] += 1
                continue
            session.add(Cek(
                business_unit_id=jedinica,
                slika_id=slika_id,
                serijski_broj=p.serijski_broj,
                racun=p.racun,
                banka=(procitan.get("banka") or "")[:60] or None,
                iznos=p.iznos,
                iznos_po_pravilu=p.iznos_po_pravilu,
                iznos_slovima=(procitan.get("iznos_slovima_tekst") or "")[:200] or None,
                datum=p.datum,
                mesto=(procitan.get("mesto") or "")[:60] or None,
                primljeno=primljeno,
                sumnjiva=p.sumnjiva or None,
                razlozi=p.razlozi or None,
                uneo=poslao,
                uneto_u=utcnow(),
            ))
            session.flush()
            brojac["novih"] += 1
            if p.sumnjiva:
                brojac["za_proveru"] += 1

        s = session.get(SlikaCekova, slika_id)
        s.procitano_u = utcnow()
        s.procitano_cekova = brojac["cekova"]
        s.greska = None
        s.napomena = (rezultat.get("napomena") or "")[:500] or None
        session.add(AuditLog(
            actor=poslao, action="cekovi.procitano",
            detail=f"slika {slika_id}: {brojac}"[:250],
        ))
    return brojac


# --------------------------------------------------------------------------- #
# rad sa cekovima
# --------------------------------------------------------------------------- #


def ispravi(
    cek_id: int,
    *,
    korisnik: str,
    serijski_broj: str,
    racun: str,
    iznos: str,
    datum: str,
) -> None:
    """Poslovodja upisuje/potvrdjuje polja. Racun mora da prodje kontrolni broj."""
    serijski = re.sub(r"\D", "", serijski_broj or "")
    if not serijski:
        raise ValueError("Upiši serijski broj sa donje trake čeka.")
    rac = normalizuj_racun(racun)
    if rac is None or not kontrolni_ok(rac):
        raise ValueError(
            "Broj računa nije ispravan (kontrolni broj ne odgovara) — proveri cifre sa donje trake."
        )
    try:
        # Polje je type=number, pa pregledac salje tacku kao decimalni znak.
        vrednost = float((iznos or "").strip().replace(",", ".")) if (iznos or "").strip() else 0.0
    except ValueError:
        raise ValueError("Iznos nije broj.") from None
    if vrednost <= 0:
        raise ValueError("Upiši iznos (ako na čeku nije popunjen, upiši 5000).")
    try:
        dan = date.fromisoformat(datum)
    except ValueError:
        raise ValueError("Upiši datum sa čeka.") from None

    with session_scope() as session:
        cek = session.get(Cek, cek_id)
        if cek is None:
            raise ValueError("Nema tog čeka.")
        drugi = session.scalar(
            select(Cek).where(Cek.racun == rac, Cek.serijski_broj == serijski, Cek.id != cek_id)
        )
        if drugi is not None:
            raise ValueError(f"Taj ček je već unet (#{drugi.id}) — ovaj obriši.")
        cek.serijski_broj, cek.racun, cek.datum = serijski, rac, dan
        if abs(vrednost - cek.iznos) > 0.005:
            cek.iznos, cek.iznos_po_pravilu = round(vrednost, 2), False
        cek.potvrdio, cek.potvrdjeno_u = korisnik, utcnow()
        session.add(AuditLog(actor=korisnik, action="cek.potvrdjen",
                             detail=f"#{cek_id} {serijski} {lep_racun(rac)} {vrednost:,.2f} {dan}"))


def obrisi(cek_id: int, *, korisnik: str) -> None:
    with session_scope() as session:
        cek = session.get(Cek, cek_id)
        if cek is None:
            raise ValueError("Nema tog čeka.")
        if cek.stanje is not StanjeCeka.U_SEFU:
            raise ValueError("Ček koji je predat u banku se ne briše.")
        session.add(AuditLog(actor=korisnik, action="cek.obrisan",
                             detail=f"#{cek_id} {cek.serijski_broj} {cek.iznos:,.2f}"))
        session.delete(cek)


def za_predaju(session, business_unit_id: int, *, dan: date | None = None) -> list[Cek]:
    """Cekovi iz sefa ciji je rok predaje danas ili ranije (i zaboravljeni)."""
    dan = dan or date.today()
    kandidati = session.scalars(
        select(Cek).where(
            Cek.business_unit_id == business_unit_id,
            Cek.stanje == StanjeCeka.U_SEFU,
            Cek.datum.isnot(None),
            Cek.datum <= dan,
        ).order_by(Cek.datum, Cek.id)
    ).all()
    return [c for c in kandidati if rok_predaje(c.datum) <= dan]


def predaj(business_unit_id: int, cek_ids: list[int], *, korisnik: str,
           dan: date | None = None) -> int:
    """Odlazak u banku. Vraca id predaje (presek za knjigovodstvo)."""
    if not cek_ids:
        raise ValueError("Nije izabran nijedan ček.")
    dan = dan or date.today()
    with session_scope() as session:
        cekovi = session.scalars(select(Cek).where(Cek.id.in_(cek_ids))).all()
        if len(cekovi) != len(set(cek_ids)):
            raise ValueError("Neki od izabranih čekova ne postoji.")
        for c in cekovi:
            if c.business_unit_id != business_unit_id:
                raise ValueError("Ček nije iz ove jedinice.")
            if c.stanje is not StanjeCeka.U_SEFU:
                raise ValueError(f"Ček {c.serijski_broj} nije u sefu ({c.stanje.value}).")
            if c.za_proveru:
                raise ValueError(f"Ček {c.serijski_broj} ima crvena polja — prvo ga proveri.")
        predaja = PredajaCekova(
            business_unit_id=business_unit_id, datum=dan, komada=len(cekovi),
            iznos=round(sum(c.iznos for c in cekovi), 2), predao=korisnik, predato_u=utcnow(),
        )
        session.add(predaja)
        session.flush()
        for c in cekovi:
            c.stanje, c.predaja_id = StanjeCeka.PREDAT, predaja.id
        session.add(AuditLog(actor=korisnik, action="cekovi.predati",
                             detail=f"predaja {predaja.id}: {predaja.komada} kom, {predaja.iznos:,.2f}"))
        return predaja.id


def vrati(cek_id: int, *, korisnik: str, napomena: str = "") -> None:
    """Banka je vratila cek - ide kod poslovodje da resi sa kupcem."""
    with session_scope() as session:
        cek = session.get(Cek, cek_id)
        if cek is None or cek.stanje is not StanjeCeka.PREDAT:
            raise ValueError("Vraćen može da bude samo ček koji je predat u banku.")
        cek.stanje, cek.vracen_u = StanjeCeka.VRACEN, date.today()
        cek.napomena = (napomena or "").strip()[:500] or cek.napomena
        session.add(AuditLog(actor=korisnik, action="cek.vracen",
                             detail=f"#{cek_id} {cek.serijski_broj} {cek.iznos:,.2f} {napomena}"[:250]))


def resi(cek_id: int, *, korisnik: str, napomena: str) -> None:
    """Vracen cek je resen sa kupcem - napomena je obavezna (kako)."""
    if not (napomena or "").strip():
        raise ValueError("Upiši kako je rešeno (npr. kupac platio gotovinom, zamenjen ček).")
    with session_scope() as session:
        cek = session.get(Cek, cek_id)
        if cek is None or cek.stanje is not StanjeCeka.VRACEN:
            raise ValueError("Rešava se samo vraćen ček.")
        cek.stanje, cek.resen_u = StanjeCeka.RESEN, utcnow()
        cek.napomena = napomena.strip()[:500]
        session.add(AuditLog(actor=korisnik, action="cek.resen",
                             detail=f"#{cek_id} {cek.serijski_broj}: {napomena}"[:250]))
