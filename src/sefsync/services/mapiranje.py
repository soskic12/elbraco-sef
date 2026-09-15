"""Faza 2: punjenje tabele `item_mapping` iz onoga sto vec postoji.

Dobavljaceva sifra artikla je jedini identifikator koji je i cest (94,9% stavki)
i stabilan - EAN se menja kad isti artikal dodje sa drugog trzista, a nas ERP
ima samo jedno polje za barkod po sifri. Zato je kljuc mapiranja
`(PIB dobavljaca + njegova sifra) -> nasa SKU`.

Tabela se ne puni rucno od nule. Dva nezavisna izvora vec nose odgovore:

    barkod       EAN sa fakture -> ARTIKLI.BARCODE (magacin 099)
    kalkulacija  zaknjizene kalkulacije iz ERP-a - ono sto je covek stvarno uneo

Mereno 14.09. na 1.756 parova koje rese oba puta: slazu se u 99,7% slucajeva.
Zato veza koju potvrde oba izvora ide u upotrebu bez ljudske provere, a sve
ostalo ceka operatera.

Parovanje stavaka unutar sparenog dokumenta ide po kolicini i fakturnoj ceni.
To je lak problem: trazi se unutar fakture od desetak stavki, gde znamo da
odgovor postoji i da je parovanje jedan-na-jedan. Gde ostane dvosmisleno, par
se ne pravi.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from ..db import session_scope
from ..models import Document, DocumentLine, ItemMapping, utcnow
from ..erp.connection import get_erp_engine

log = logging.getLogger(__name__)

# Od kog Clarion dana se gledaju kalkulacije (82184 = 2026-01-01).
OD_DANA = 82184


def kljuc_racuna(broj: str | None) -> str:
    """Broj racuna u obliku u kom se moze porediti sa ERP-om.

    ERP cuva broj kakav dobavljac posalje, ali ljudi ponekad izbace crtice da
    stane u varchar(20). Zato se poredi samo po slovima i ciframa, bez vodecih
    nula: `FA-5543-03/26` == `FA55430326`.
    """
    return re.sub(r"[^A-Z0-9]", "", (broj or "").upper()).lstrip("0")


@dataclass
class Nalaz:
    """Sta je izvedeno iz istorije, pre upisa."""

    ukupno_parova: int = 0
    iz_barkoda: int = 0
    iz_kalkulacija: int = 0
    oba_se_slazu: int = 0
    oba_se_ne_slazu: int = 0
    kolebljivih: int = 0
    upisano: int = 0
    vec_postojalo: int = 0
    mapiranja: dict = field(default_factory=dict)  # (pib, sifra) -> dict

    @property
    def pouzdanih(self) -> int:
        return sum(1 for m in self.mapiranja.values() if m["source"] == "oba"
                   or (m["source"] == "kalkulacija" and m["evidence"] >= 2))


def _istina_iz_erpa(conn) -> tuple[dict, dict]:
    """Vraca (zaglavlja po kljucu racuna, stavke po kalkulaciji)."""
    naz_mb = {
        r.a: r.mb
        for r in conn.execute(
            text("SELECT RTRIM(ANALITIKA) a, RTRIM(ISNULL(MATBROJ,'')) mb FROM dbo.NAZIVI")
        )
    }
    zaglavlja: dict[str, list] = defaultdict(list)
    for tab in ("ULKALKUL", "MKALKUL"):
        for r in conn.execute(
            text(
                f"SELECT RTRIM(BROJ_RN_DOB) rn, RTRIM(MAGACIN) mag, RTRIM(VRSTAKNJIZ) vk, "
                f"RTRIM(BROJ) br, RTRIM(DOBAVLJAC) dob FROM dbo.{tab} WHERE DATUM >= :od"
            ),
            {"od": OD_DANA},
        ):
            if r.rn:
                zaglavlja[kljuc_racuna(r.rn)].append(
                    (tab, r.mag, r.vk, r.br, naz_mb.get(r.dob, ""))
                )

    stavke: dict[tuple, list] = defaultdict(list)
    for tab, tabela_stavki in (("ULKALKUL", "PRULKALK"), ("MKALKUL", "MPRULK")):
        for r in conn.execute(
            text(
                f"SELECT RTRIM(MAGACIN) mag, RTRIM(VRSTAKNJIZ) vk, RTRIM(BROJ) br, "
                f"RTRIM(ARTIKAL) art, KOLICINA, FAK_CENA FROM dbo.{tabela_stavki}"
            )
        ):
            stavke[(tab, r.mag, r.vk, r.br)].append((r.art, r.KOLICINA, r.FAK_CENA))
    return zaglavlja, stavke


def izvedi(engine: Engine | None = None) -> Nalaz:
    """Izvodi mapiranja iz istorije. Ne dira bazu - vraca nalaz."""
    eng = engine or get_erp_engine()
    nalaz = Nalaz()

    with session_scope() as session, eng.connect() as conn:
        # --- nase fakture ---
        doc_po_racunu: dict[str, list] = defaultdict(list)
        podaci: dict[int, tuple] = {}
        for did, broj, pib, mb in session.execute(
            select(Document.id, Document.document_number,
                   Document.supplier_vat, Document.supplier_reg_no)
        ):
            if broj and pib:
                doc_po_racunu[kljuc_racuna(broj)].append(did)
                podaci[did] = (pib, (mb or "").strip())

        linije: dict[int, list] = defaultdict(list)
        parovi: dict[tuple, set] = defaultdict(set)
        nazivi: dict[tuple, str] = {}
        for did, sifra, ean, kol, cena, naziv in session.execute(
            select(DocumentLine.document_id, DocumentLine.sellers_item_id,
                   DocumentLine.standard_item_id, DocumentLine.quantity,
                   DocumentLine.price, DocumentLine.name)
        ):
            if did not in podaci or not sifra:
                continue
            pib = podaci[did][0]
            par = (pib, sifra.strip())
            linije[did].append((par, ean, kol, cena))
            parovi[par].add(ean)
            nazivi.setdefault(par, naziv or "")
        nalaz.ukupno_parova = len(parovi)

        # --- izvor 1: barkod ---
        po_barkodu: dict[str, set] = defaultdict(set)
        for r in conn.execute(
            text("SELECT RTRIM(ARTIKAL) art, RTRIM(ISNULL(BARCODE,'')) bc "
                 "FROM dbo.ARTIKLI WHERE RTRIM(SIFRAMAG)='099'")
        ):
            if r.bc:
                po_barkodu[r.bc].add(r.art)
        # Barkod koji vodi na vise sifara ne dokazuje nista.
        ean_jednoznacan = {b: next(iter(v)) for b, v in po_barkodu.items() if len(v) == 1}

        iz_barkoda: dict[tuple, str] = {}
        for par, eanovi in parovi.items():
            pogodci = {ean_jednoznacan[e] for e in eanovi if e and e in ean_jednoznacan}
            if len(pogodci) == 1:
                iz_barkoda[par] = next(iter(pogodci))
        nalaz.iz_barkoda = len(iz_barkoda)

        # --- izvor 2: zaknjizene kalkulacije ---
        zaglavlja, erp_stavke = _istina_iz_erpa(conn)
        glasovi: dict[tuple, Counter] = defaultdict(Counter)
        for rn, kandidati in zaglavlja.items():
            dids = doc_po_racunu.get(rn)
            if not dids:
                continue
            for tab, mag, vk, br, mb_erp in kandidati:
                # Isti broj racuna moze da nosi vise dobavljaca - resava maticni broj.
                did = next((d for d in dids if podaci[d][1] and podaci[d][1] == mb_erp), None)
                if did is None and len(dids) == 1:
                    did = dids[0]
                if did is None:
                    continue
                nase = erp_stavke.get((tab, mag, vk, br))
                if not nase:
                    continue
                po_kolicini_i_ceni: dict[tuple, list] = defaultdict(list)
                for art, kol, cena in nase:
                    po_kolicini_i_ceni[(round(kol or 0, 3), round(cena or 0, 2))].append(art)
                for par, _ean, kol, cena in linije.get(did, ()):
                    kand = po_kolicini_i_ceni.get(
                        (round(kol or 0, 3), round(float(cena or 0), 2)), []
                    )
                    if len(set(kand)) == 1:
                        glasovi[par][kand[0]] += 1

        iz_kalkulacija = {p: c.most_common(1)[0][0] for p, c in glasovi.items() if len(c) == 1}
        nalaz.iz_kalkulacija = len(iz_kalkulacija)
        nalaz.kolebljivih = sum(1 for c in glasovi.values() if len(c) > 1)

        # --- spajanje ---
        for par in set(iz_barkoda) | set(iz_kalkulacija):
            b, k = iz_barkoda.get(par), iz_kalkulacija.get(par)
            dokaza = sum(glasovi[par].values()) if par in glasovi else 0
            if b and k:
                if b == k:
                    nalaz.oba_se_slazu += 1
                    izvor, sifra = "oba", b
                else:
                    # Dva izvora se ne slazu - ni jedan se ne upisuje bez coveka.
                    nalaz.oba_se_ne_slazu += 1
                    izvor, sifra = "nesaglasno", k
            elif k:
                izvor, sifra = "kalkulacija", k
            else:
                izvor, sifra = "barkod", b
            nalaz.mapiranja[par] = {
                "erp_item_code": sifra,
                "source": izvor,
                "evidence": dokaza,
                "barcode": next((e for e in parovi[par] if e), None),
                "name": nazivi.get(par, "")[:500],
            }
    return nalaz


def upisi(nalaz: Nalaz, *, samo_pouzdana: bool = True) -> Nalaz:
    """Upisuje izvedena mapiranja. Nesaglasna se nikad ne upisuju sama."""
    with session_scope() as session:
        postojeca = {
            (m.supplier_vat, m.supplier_item_id): m
            for m in session.scalars(select(ItemMapping))
        }
        for (pib, sifra), podatak in nalaz.mapiranja.items():
            if podatak["source"] == "nesaglasno":
                continue
            pouzdano = podatak["source"] == "oba" or (
                podatak["source"] == "kalkulacija" and podatak["evidence"] >= 2
            )
            if samo_pouzdana and not pouzdano:
                continue
            if (pib, sifra) in postojeca:
                nalaz.vec_postojalo += 1
                continue
            session.add(
                ItemMapping(
                    supplier_vat=pib,
                    supplier_item_id=sifra,
                    barcode=podatak["barcode"],
                    supplier_item_name=podatak["name"],
                    erp_item_code=podatak["erp_item_code"],
                    source=podatak["source"],
                    evidence=podatak["evidence"],
                )
            )
            nalaz.upisano += 1
    log.info("Upisano mapiranja: %d (već postojalo %d)", nalaz.upisano, nalaz.vec_postojalo)
    return nalaz
