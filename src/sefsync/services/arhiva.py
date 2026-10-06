"""UBL fajlovi stariji od dva meseca idu u arhivu, a sa diska se sklanjaju.

Disk na serveru je bio skoro pun (7,7 GB slobodno od 140), a `C:\\efakture` je
drzao 12,2 GB - najvecim delom bas sacuvane UBL-ove. Mereno 06.10.: 7.728
fajlova, 1,8 GB, od cega **56 % otpada na priloge koje dobavljac zapakuje kao
base64 unutar samog XML-a** (pojedinacni idu i do 971 KB).

Zasto se sme sklanjati: **SEF i dalje daje te dokumente.** Provereno 06.10. -
UBL za januar, mart i jun se skida bez problema. Zato arhiva nije radni
materijal nego cuvanje; kad aplikaciji zatreba stari UBL, skine ga ponovo i
snimi na isto mesto (`ubl_bajtovi`).

Ne sklanja se:

* nista iz poslednjih `UBL_ARHIVA_DANA` dana;
* nijedan dokument koji jos nije zavrsen - nacrt koji nije upisan, otvoren
  manjak, stavka koja ceka sifru. Fajl koji nam treba sutra ostaje.

Zip se pravi po mesecima i **proverava pre brisanja**: broj fajlova i velicina
svakog od njih moraju da se poklope. Zip ostaje na serveru dok ga covek ne
prebaci - aplikacija ne brise jedini primerak.
"""

from __future__ import annotations

import logging
import zipfile
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import (
    Document,
    NacrtKalkulacije,
    PrijavaManjka,
    StanjeNacrta,
    utcnow,
)

log = logging.getLogger(__name__)

# Stanja u kojima dokument jos nije zavrsen - njegov UBL ostaje na disku.
NEZAVRSENO = (StanjeNacrta.CEKA_SIFRU, StanjeNacrta.PROBLEM, StanjeNacrta.SPREMAN)


def _arhiva_dir() -> Path:
    p = get_settings().storage_dir / "arhiva"
    p.mkdir(parents=True, exist_ok=True)
    return p


def za_arhivu(dana: int | None = None) -> dict[str, list[tuple[int, Path]]]:
    """Dokumenti ciji UBL sme da ode u arhivu, grupisani po mesecu `GGGG-MM`."""
    podesavanja = get_settings()
    granica = utcnow() - timedelta(days=dana or podesavanja.ubl_arhiva_dana)

    po_mesecu: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    with session_scope() as session:
        nezavrseni = {
            r[0]
            for r in session.execute(
                select(NacrtKalkulacije.document_id)
                .where(NacrtKalkulacije.stanje.in_(NEZAVRSENO))
            )
        }
        sa_manjkom = {
            r[0]
            for r in session.execute(
                select(PrijavaManjka.document_id)
                .where(PrijavaManjka.zatvoreno_u.is_(None))
            )
        }
        for doc_id, putanja, poslato, nastalo in session.execute(
            select(Document.id, Document.ubl_path, Document.sent_date, Document.created_at)
            .where(Document.ubl_path.is_not(None), Document.ubl_arhiviran.is_(None))
        ):
            kad = poslato or nastalo
            if kad is None or kad.replace(tzinfo=None) > granica.replace(tzinfo=None):
                continue
            if doc_id in nezavrseni or doc_id in sa_manjkom:
                continue
            fajl = Path(putanja)
            if not fajl.exists():
                continue
            po_mesecu[f"{kad.year:04d}-{kad.month:02d}"].append((doc_id, fajl))
    return dict(sorted(po_mesecu.items()))


def arhiviraj(dana: int | None = None, *, stvarno: bool = False) -> dict:
    """Pakuje stare UBL-ove po mesecima i sklanja ih sa diska.

    Brise tek kad proveri da je u zipu sve - i po broju fajlova i po velicini
    svakog ponaosob.
    """
    po_mesecu = za_arhivu(dana)
    ishod = {"meseci": {}, "fajlova": 0, "bajtova": 0, "obrisano": 0}

    for mesec, stavke in po_mesecu.items():
        bajtova = sum(f.stat().st_size for _, f in stavke)
        ishod["meseci"][mesec] = {"fajlova": len(stavke), "bajtova": bajtova}
        ishod["fajlova"] += len(stavke)
        ishod["bajtova"] += bajtova
        if not stvarno:
            continue

        zip_putanja = _arhiva_dir() / f"ubl-{mesec}.zip"
        with zipfile.ZipFile(zip_putanja, "a", zipfile.ZIP_DEFLATED) as zip_fajl:
            vec = set(zip_fajl.namelist())
            for _, fajl in stavke:
                ime = f"{mesec}/{fajl.name}"
                if ime not in vec:
                    zip_fajl.write(fajl, ime)

        # Provera pre brisanja: svaki fajl mora da je u zipu, iste velicine.
        with zipfile.ZipFile(zip_putanja) as zip_fajl:
            u_zipu = {i.filename: i.file_size for i in zip_fajl.infolist()}
        neispravni = [
            fajl.name
            for _, fajl in stavke
            if u_zipu.get(f"{mesec}/{fajl.name}") != fajl.stat().st_size
        ]
        if neispravni:
            log.error(
                "Arhiva %s ne sadrzi sve - ne brisem nista (%s...)",
                zip_putanja.name, ", ".join(neispravni[:3]),
            )
            ishod["meseci"][mesec]["greska"] = f"{len(neispravni)} fajlova nije u zipu"
            continue

        sada = utcnow()
        with session_scope() as session:
            for doc_id, fajl in stavke:
                try:
                    fajl.unlink()
                except OSError as exc:  # noqa: PERF203 - jedan fajl ne obara posao
                    log.warning("Ne mogu da obrisem %s: %s", fajl, exc)
                    continue
                doc = session.get(Document, doc_id)
                if doc is not None:
                    doc.ubl_arhiviran = sada
                ishod["obrisano"] += 1
        log.info("Arhivirano %s: %s fajlova u %s", mesec, len(stavke), zip_putanja.name)

    return ishod


def ubl_bajtovi(doc: Document) -> bytes | None:
    """Sadrzaj UBL-a. Ako fajla nema na disku, skida ga ponovo sa SEF-a.

    Arhiviranje time prestaje da bude odluka sa posledicama: sve sto radi na
    svezem dokumentu radi i na onom od pre pola godine, samo malo sporije.
    """
    putanja = (doc.ubl_path or "").strip()
    if putanja:
        fajl = Path(putanja)
        if fajl.exists():
            return fajl.read_bytes()

    if not doc.sef_invoice_id:
        return None
    try:
        from ..sef.client import SefClient

        with SefClient() as sef:
            sadrzaj = sef.purchase_ubl(doc.sef_invoice_id)
    except Exception as exc:  # noqa: BLE001 - SEF ume da bude nedostupan
        log.warning("UBL za #%s nije vracen sa SEF-a: %s", doc.id, exc)
        return None

    if sadrzaj and putanja:
        try:
            fajl = Path(putanja)
            fajl.parent.mkdir(parents=True, exist_ok=True)
            fajl.write_bytes(sadrzaj)
            log.info("UBL za #%s vracen sa SEF-a u %s", doc.id, fajl)
        except OSError as exc:
            log.info("UBL za #%s nije snimljen: %s", doc.id, exc)
    return sadrzaj or None
