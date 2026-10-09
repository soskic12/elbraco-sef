"""Faktor pakovanja izveden iz istorije: koliko je covek stvarno uneo.

Pub (071) robu kupuje u pakovanju a vodi je u jedinici posluzivanja - bure od
20 l ulazi kao 20 litara, kutija caja kao 40 kesica, kilogram kafe kao 1000
grama. To je poslovodja godinama radio rukom, pa taj posao **vec postoji u
ERP-u** i ne mora da se pogadja.

Ovde se radi isto sto i `services/mapiranje.py` sa vezama sifara: za svaku nasu
stavku kojoj je poznat artikal trazi se red u kalkulaciji koja nosi isti broj
racuna, pa se faktor izracuna kao `kolicina u ERP-u / kolicina na fakturi`.

**Sta se smatra pouzdanim** (i jedino se to upisuje):

* isti faktor vidjen **bar dvaput**, bez ijednog drugacijeg; ili
* vidjen jednom, ali se **poklapa sa onim sto cita `erp/pakovanje.py`** iz naziva
  stavke - dva nezavisna izvora koji kazu isto.

Sve ostalo ostaje coveku. Pogresan faktor tiho upise pogresnu zalihu, a to se
vidi tek kad proizvodnja ode u minus - kao `HAUSBRANDT HERMAN 1kg` koji je
zavrsio kao 6 grama umesto 6.000 (kalkulacija 00194, pub 071).

Postojeci faktor se **ne menja**: ono sto je covek potvrdio jace je od izvodjenja.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select, text

from ..db import session_scope
from ..models import Document, ItemMapping, utcnow

log = logging.getLogger(__name__)

# Koliko se razlikuju kolicine a da se jos smatra istom stavkom.
TOLERANCIJA = 0.001
# Faktor van ovog raspona je gotovo sigurno greska u podacima, ne pakovanje.
NAJVECI_FAKTOR = 5000.0


@dataclass
class Nalaz:
    dokumenata: int = 0
    uparenih_stavki: int = 0
    vidjeno: dict[tuple[str, str], Counter] = field(default_factory=dict)
    pouzdani: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    kolebljivi: dict[tuple[str, str], Counter] = field(default_factory=dict)
    upisano: int = 0
    vec_postojalo: int = 0

    @property
    def za_upis(self) -> int:
        return len(self.pouzdani)


def oceni(brojac: Counter, iz_naziva: float | None) -> tuple[float, str] | None:
    """Da li je izvedeni faktor pouzdan. `None` znaci - ostavi coveku.

    Pouzdano je samo:

    * isti faktor **bar dvaput**, bez ijednog drugacijeg; ili
    * vidjen **jednom**, ali se poklapa sa onim sto se cita iz naziva stavke -
      dva nezavisna izvora koji kazu isto.

    Faktor 1 se ne upisuje jer ne menja nista. Kolebljiva veza se ne upisuje
    nikad: pogresan faktor tiho upise pogresnu zalihu.
    """
    if len(brojac) != 1:
        return None
    faktor, koliko = next(iter(brojac.items()))
    if faktor == 1.0:
        return None
    if koliko >= 2:
        return faktor, f"viđeno {koliko} puta"
    if iz_naziva is not None and abs(iz_naziva - faktor) < TOLERANCIJA:
        return faktor, "viđeno jednom, i naziv kaže isto"
    return None


def _erp_stavke(conn, analitika: str) -> dict[str, list[dict]]:
    """Sve stavke kalkulacija tog dobavljaca, po broju racuna.

    Jedan upit po dobavljacu umesto jednog po dokumentu - na 'nacrti --osvezi'
    se vec videlo sta znaci upit po stavci (Bane, 06.10.: "vrti ga pola sata").
    """
    po_racunu: dict[str, list[dict]] = {}
    for zaglavlje, stavke in (("ULKALKUL", "PRULKALK"), ("MKALKUL", "MPRULK")):
        for r in conn.execute(
            text(
                f"SELECT REPLACE(RTRIM(z.BROJ_RN_DOB),' ','') rn, RTRIM(s.ARTIKAL) artikal, "
                f"s.KOLICINA kolicina, RTRIM(z.MAGACIN) magacin "
                f"FROM dbo.{zaglavlje} z JOIN dbo.{stavke} s "
                f"  ON RTRIM(z.MAGACIN)=RTRIM(s.MAGACIN) AND RTRIM(z.BROJ)=RTRIM(s.BROJ) "
                f" AND RTRIM(z.VRSTAKNJIZ)=RTRIM(s.VRSTAKNJIZ) "
                f"WHERE RTRIM(z.DOBAVLJAC)=:d AND ISNULL(z.BROJ_RN_DOB,'') <> '' "
                f"  AND s.KOLICINA <> 0"
            ),
            {"d": analitika},
        ):
            po_racunu.setdefault(r.rn, []).append(
                {"artikal": r.artikal, "kolicina": float(r.kolicina), "magacin": r.magacin}
            )
    return po_racunu


def izvedi(dana: int = 365, engine=None) -> Nalaz:
    """Prolazi kroz nase dokumente i uporedjuje ih sa kalkulacijama u ERP-u."""
    from ..erp.connection import get_erp_engine
    from ..erp.kalkulacija import _analitika_dobavljaca, veze_dobavljaca
    from ..erp.pakovanje import predlozi_faktor

    nalaz = Nalaz()
    eng = engine or get_erp_engine()
    granica = utcnow() - timedelta(days=dana)

    with session_scope() as session:
        veze = veze_dobavljaca(session, osvezi_kes=True)
        dokumenti = list(
            session.scalars(
                select(Document).where(
                    Document.supplier_vat.is_not(None),
                    Document.document_number.is_not(None),
                    Document.created_at >= granica.replace(tzinfo=None),
                )
            )
        )
        # Jedinica artikla iz sifarnika treba citacu naziva; cita se jednom.
        jedinice: dict[str, str] = {}
        po_dobavljacu: dict[str, list[Document]] = {}
        for d in dokumenti:
            po_dobavljacu.setdefault((d.supplier_vat or "").strip(), []).append(d)

        with eng.connect() as conn:
            for r in conn.execute(
                text("SELECT RTRIM(ARTIKAL) a, RTRIM(ISNULL(JEDMERE,'')) j FROM dbo.ARTIKLI "
                     "WHERE RTRIM(SIFRAMAG)='099'")
            ):
                jedinice[r.a] = r.j

            for pib, njihovi in po_dobavljacu.items():
                analitika = _analitika_dobavljaca(conn, njihovi[0].supplier_reg_no, pib)
                if not analitika:
                    continue
                erp = _erp_stavke(conn, analitika)
                if not erp:
                    continue
                for doc in njihovi:
                    kljuc_racuna = (doc.document_number or "").replace(" ", "")
                    redovi = erp.get(kljuc_racuna)
                    if not redovi:
                        continue
                    nalaz.dokumenata += 1
                    for linija in doc.lines:
                        if not linija.quantity:
                            continue
                        sifra = _sifra_stavke(veze, pib, linija)
                        if not sifra:
                            continue
                        isti = [x for x in redovi if x["artikal"] == sifra]
                        if len(isti) != 1:
                            # Dva reda istog artikla - ne zna se koji je koji.
                            continue
                        faktor = isti[0]["kolicina"] / abs(float(linija.quantity))
                        if faktor <= 0 or faktor > NAJVECI_FAKTOR:
                            continue
                        faktor = round(faktor, 4)
                        if abs(faktor - round(faktor)) < TOLERANCIJA:
                            faktor = float(round(faktor))
                        nalaz.uparenih_stavki += 1
                        nalaz.vidjeno.setdefault((pib, sifra), Counter())[faktor] += 1

        # Sta je pouzdano.
        for (pib, sifra), brojac in nalaz.vidjeno.items():
            iz_naziva = None
            if len(brojac) == 1 and next(iter(brojac)) != 1.0:
                primer = _primer_naziva(session, pib, sifra, granica)
                if primer is not None:
                    iz_naziva = predlozi_faktor(primer[0], primer[1], jedinice.get(sifra))[0]
            odluka = oceni(brojac, iz_naziva)
            if odluka is None:
                if len(brojac) > 1:
                    nalaz.kolebljivi[(pib, sifra)] = brojac
                continue
            nalaz.pouzdani[(pib, sifra)] = odluka

    return nalaz


def _sifra_stavke(veze, pib: str, linija) -> str | None:
    """Nasa sifra za stavku, iz veza. Bez pogadjanja po nazivu."""
    sifra_dob = (linija.sellers_item_id or "").strip()
    if sifra_dob:
        kod = veze.po_sifri.get((pib, sifra_dob))
        if kod:
            return kod
    barkod = (linija.standard_item_id or "").strip()
    if barkod:
        kodovi = veze.po_barkodu.get(barkod)
        if kodovi and len(kodovi) == 1:
            return next(iter(kodovi))
    return (linija.erp_item_code or "").strip() or None


def _primer_naziva(session, pib: str, sifra: str, granica) -> tuple[str, str] | None:
    """Naziv i jedinica jedne stavke te veze - da se moze procitati pakovanje."""
    from ..models import DocumentLine

    red = session.execute(
        select(DocumentLine.name, DocumentLine.unit_code)
        .join(Document, Document.id == DocumentLine.document_id)
        .where(
            Document.supplier_vat == pib,
            DocumentLine.erp_item_code == sifra,
            DocumentLine.name.is_not(None),
        )
        .limit(1)
    ).first()
    if red is None:
        veza = session.scalars(
            select(ItemMapping).where(
                ItemMapping.supplier_vat == pib,
                ItemMapping.erp_item_code == sifra,
                ItemMapping.supplier_item_name.is_not(None),
            ).limit(1)
        ).first()
        return (veza.supplier_item_name, "") if veza else None
    return (red[0], red[1] or "")


def upisi(nalaz: Nalaz) -> Nalaz:
    """Upisuje pouzdane faktore uz veze. Postojeci faktor se ne menja."""
    with session_scope() as session:
        for (pib, sifra), (faktor, _razlog) in nalaz.pouzdani.items():
            veze = list(
                session.scalars(
                    select(ItemMapping).where(
                        ItemMapping.supplier_vat == pib,
                        ItemMapping.erp_item_code == sifra,
                        ItemMapping.active == True,  # noqa: E712
                    )
                )
            )
            if not veze:
                continue
            if all(abs((v.unit_factor or 1.0) - faktor) < TOLERANCIJA for v in veze):
                nalaz.vec_postojalo += 1
                continue
            for v in veze:
                if abs((v.unit_factor or 1.0) - 1.0) > TOLERANCIJA:
                    # Nesto vec stoji i nije 1 - to je covekova odluka, ne diramo.
                    continue
                v.unit_factor = faktor
            nalaz.upisano += 1
    from ..erp.kalkulacija import zaboravi_veze

    zaboravi_veze()
    log.info("Upisano faktora: %d (vec postojalo %d)", nalaz.upisano, nalaz.vec_postojalo)
    return nalaz
