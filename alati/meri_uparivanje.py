r"""Zasto dokument ne nadje svoju kalkulaciju u ERP-u.

Od 7.727 dokumenata njih oko 7 % ne nalazi par po broju racuna. Pretpostavka iz
faze 2 je bila da broj nije stao u `BROJ_RN_DOB varchar(20)`, ali je merenje
15.09. to vec oborilo: nijedan od 4.652 broja nije dug 20 znakova. Ovde se gleda
sta je zaista u pitanju, pre nego sto se uparivanje olabavi.

Ne dira nijednu bazu - samo cita i ispisuje.

    .\.venv\Scripts\python.exe alati\meri_uparivanje.py
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

sys.path.insert(0, "src")

from sqlalchemy import select, text  # noqa: E402

from sefsync.db import session_scope  # noqa: E402
from sefsync.erp.connection import get_erp_engine  # noqa: E402
from sefsync.models import Document  # noqa: E402
from sefsync.services.mapiranje import OD_DANA, kljuc_racuna  # noqa: E402

NAJKRACI_POCETAK = 10  # koliko znakova mora da se poklopi da poredjenje ima smisla


def iz_erpa() -> tuple[dict[str, list], dict[str, str]]:
    """(kljuc racuna -> [(tabela, magacin, matbroj)], analitika -> matbroj)"""
    with get_erp_engine().connect() as conn:
        naz = {
            r.a: r.mb
            for r in conn.execute(
                text("SELECT RTRIM(ANALITIKA) a, RTRIM(ISNULL(MATBROJ,'')) mb FROM dbo.NAZIVI")
            )
        }
        po_kljucu: dict[str, list] = defaultdict(list)
        for tab in ("ULKALKUL", "MKALKUL"):
            for r in conn.execute(
                text(
                    f"SELECT RTRIM(BROJ_RN_DOB) rn, RTRIM(MAGACIN) mag, RTRIM(DOBAVLJAC) dob "
                    f"FROM dbo.{tab} WHERE DATUM >= :od"
                ),
                {"od": OD_DANA},
            ):
                if r.rn:
                    po_kljucu[kljuc_racuna(r.rn)].append((tab, r.mag, naz.get(r.dob, "")))
    return po_kljucu, naz


def main() -> None:
    po_kljucu, _ = iz_erpa()
    kljucevi = list(po_kljucu)
    print(f"ERP: {len(kljucevi)} razlicitih brojeva racuna")

    # Indeks po pocetku kljuca - da se ne pretrazuje 8.500 kljuceva za svaki dokument.
    po_pocetku: dict[str, list[str]] = defaultdict(list)
    for k in kljucevi:
        if len(k) >= NAJKRACI_POCETAK:
            po_pocetku[k[:NAJKRACI_POCETAK]].append(k)

    with session_scope() as session:
        dokumenti = list(session.scalars(select(Document)))
    print(f"Panel: {len(dokumenti)} dokumenata")

    tacno = 0
    nespareni = []
    for doc in dokumenti:
        k = kljuc_racuna(doc.document_number)
        if k in po_kljucu:
            tacno += 1
        else:
            nespareni.append((doc, k))

    print(f"Tacno uparenih: {tacno} ({tacno / len(dokumenti):.1%})")
    print(f"Nesparenih:     {len(nespareni)} ({len(nespareni) / len(dokumenti):.1%})")

    print("\n--- Nespareni po vrsti dokumenta ---")
    for vrsta, n in Counter(d.document_type.value for d, _ in nespareni).most_common():
        print(f"  {vrsta:<12} {n:>4}")

    print("\n--- Duzina kljuca kod nesparenih ---")
    duzine = Counter(len(k) for _, k in nespareni)
    for duz in sorted(duzine):
        print(f"  {duz:>3} znakova: {duzine[duz]:>4}")

    print("\n--- Deset dobavljaca sa najvise nesparenih ---")
    for ime, n in Counter(
        (d.supplier_name or "?")[:40] for d, _ in nespareni
    ).most_common(10):
        print(f"  {n:>4}  {ime}")

    print(f"\n--- Sta bi vratilo poredjenje po prvih {NAJKRACI_POCETAK} znakova ---")
    nasao_jedan = nasao_vise = nista = prekratak = 0
    primeri = []
    for doc, k in nespareni:
        if len(k) < NAJKRACI_POCETAK:
            prekratak += 1
            continue
        kandidati = [
            e for e in po_pocetku.get(k[:NAJKRACI_POCETAK], [])
            if e.startswith(k) or k.startswith(e)
        ]
        if not kandidati:
            nista += 1
        elif len(kandidati) == 1:
            nasao_jedan += 1
            if len(primeri) < 8:
                primeri.append((doc.document_number, k, kandidati[0]))
        else:
            nasao_vise += 1
    print(f"  jedan kandidat: {nasao_jedan}")
    print(f"  vise kandidata: {nasao_vise}  (dvosmisleno - ne sme se uzeti)")
    print(f"  nista:          {nista}")
    print(f"  kljuc kraci od {NAJKRACI_POCETAK}: {prekratak}")
    for broj, k, e in primeri:
        print(f"    '{broj}' -> kljuc {k}  ~  ERP {e}")

    print("\n--- Nespareni po mesecu (i koliko ih je ukupno tog meseca) ---")
    # Kalkulacija se knjizi posle prijema robe, pa sveze fakture jos nemaju par -
    # to nije greska uparivanja nego posao koji tek predstoji.
    uk_mesec: Counter = Counter()
    nesp_mesec: Counter = Counter()
    for doc in dokumenti:
        m = doc.issue_date.strftime("%Y-%m") if doc.issue_date else "?"
        uk_mesec[m] += 1
        if kljuc_racuna(doc.document_number) not in po_kljucu:
            nesp_mesec[m] += 1
    for m in sorted(uk_mesec):
        n, u = nesp_mesec[m], uk_mesec[m]
        print(f"  {m}: {n:>4} od {u:>4} ({n / u:.1%})" if u else m)

    print("\n--- U kom smeru se broj razlikuje ---")
    erp_kraci = doc_kraci = 0
    for doc, k in nespareni:
        if len(k) < NAJKRACI_POCETAK:
            continue
        for e in po_pocetku.get(k[:NAJKRACI_POCETAK], []):
            if e.startswith(k):
                doc_kraci += 1   # ERP ima vise znakova nego dokument
                break
            if k.startswith(e):
                erp_kraci += 1   # ERP je skracen - pretpostavka iz faze 2
                break
    print(f"  ERP kraci (skracen broj): {erp_kraci}")
    print(f"  ERP duzi (visak znakova): {doc_kraci}")

    print("\n--- Da li dobavljac uopste ima kalkulacije ---")
    # Ako nijedan dokument tog dobavljaca nikad ne nadje par, on verovatno i nema
    # kalkulaciju (banke, usluge) - takvi ne pripadaju imeniocu.
    upareni_po_dobavljacu: Counter = Counter()
    ukupno_po_dobavljacu: Counter = Counter()
    for doc in dokumenti:
        pib = doc.supplier_vat or doc.supplier_name or "?"
        ukupno_po_dobavljacu[pib] += 1
        if kljuc_racuna(doc.document_number) in po_kljucu:
            upareni_po_dobavljacu[pib] += 1

    nikad = stvarni = 0
    for doc, _ in nespareni:
        pib = doc.supplier_vat or doc.supplier_name or "?"
        if upareni_po_dobavljacu[pib] == 0:
            nikad += 1
        else:
            stvarni += 1
    print(f"  dobavljac nema nijednu kalkulaciju (banke, usluge): {nikad}")
    print(f"  dobavljac inace ima kalkulacije - stvarni promasaj:  {stvarni}")

    print("\n--- Svih 405 razvrstano po uzroku (svaki dokument tacno jednom) ---")
    # ERP drzi bazu po godini (ELBS_2012 ... ELBS_2026), a ova pocinje 01.01.2026.
    # Kalkulacija za januarsku fakturu cesto stoji u proslogodisnjoj bazi, do koje
    # nalog nema pravo pristupa - zato januar ovde ima svoju kategoriju.
    razlozi: Counter = Counter()
    for doc, k in nespareni:
        pib = doc.supplier_vat or doc.supplier_name or "?"
        mesec = doc.issue_date.strftime("%Y-%m") if doc.issue_date else "?"
        kandidati = (
            [e for e in po_pocetku.get(k[:NAJKRACI_POCETAK], [])
             if e.startswith(k) or k.startswith(e)]
            if len(k) >= NAJKRACI_POCETAK else []
        )
        if doc.document_type.value == "Prepayment":
            razlozi["avans - kalkulacije ni nema"] += 1
        elif upareni_po_dobavljacu[pib] == 0:
            razlozi["dobavljac nema nijednu kalkulaciju (banke, usluge)"] += 1
        elif len(kandidati) == 1:
            razlozi["nasao bi se po pocetku broja (skracen u ERP-u)"] += 1
        elif len(kandidati) > 1:
            razlozi["po pocetku broja dvosmisleno"] += 1
        elif mesec == "2026-01":
            razlozi["januar - kalkulacija verovatno u bazi ELBS_2025"] += 1
        elif mesec >= "2026-09":
            razlozi["sveza faktura - jos nije zaknjizena"] += 1
        else:
            razlozi["neobjasnjeno"] += 1
    for razlog, n in razlozi.most_common():
        print(f"  {n:>4}  {razlog}")

    print("\n--- Koliko bi to pravilo napravilo laznih parova ---")
    # Provera na dokumentima koji VEC imaju tacan par: da li bi poredjenje po
    # pocetku naslo jos nekog, tj. da li bi uzelo pogresnog.
    lazni = 0
    provereno = 0
    for doc in dokumenti:
        k = kljuc_racuna(doc.document_number)
        if k not in po_kljucu or len(k) < NAJKRACI_POCETAK:
            continue
        provereno += 1
        drugi = [
            e for e in po_pocetku.get(k[:NAJKRACI_POCETAK], [])
            if e != k and (e.startswith(k) or k.startswith(e))
        ]
        if drugi:
            lazni += 1
    print(f"  od {provereno} tacno uparenih, {lazni} bi imalo i drugog kandidata "
          f"({lazni / provereno:.2%})" if provereno else "  nema sta da se proveri")


if __name__ == "__main__":
    main()
