r"""Koliko kalkulacija bi aplikacija sklopila sama, a koliko bi cekalo coveka.

Za svaki razvrstan dokument pokusa da sklopi nacrt i prebroji sta ga zaustavlja.
Time se meri koliko bi posla imao ekran na kom Dule otvara nove sifre.

Ne dira nijednu bazu - samo cita i ispisuje.

    .\.venv\Scripts\python.exe alati\meri_kalkulacije.py [broj_dana]
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from collections import Counter

sys.path.insert(0, "src")

from sqlalchemy import select  # noqa: E402

from sefsync.db import session_scope  # noqa: E402
from sefsync.erp.connection import get_erp_engine  # noqa: E402
from sefsync.erp.kalkulacija import pripremi  # noqa: E402
from sefsync.models import Document, DocumentType  # noqa: E402

DANA = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def vrsta_problema(tekst: str) -> str:
    """Skida brojeve stavki i sifre, da bi se slicni problemi grupisali."""
    t = re.sub(r"stavka \d+: ", "", tekst)
    t = re.sub(r"\b\d{4,}\b", "…", t)
    return t[:70]


def main() -> None:
    granica = dt.date.today() - dt.timedelta(days=DANA)
    with session_scope() as session:
        dokumenti = [
            (d.id, d.document_type, d.business_unit.code if d.business_unit else None)
            for d in session.scalars(
                select(Document).where(
                    Document.issue_date >= granica,
                    Document.business_unit_id.is_not(None),
                )
            )
        ]
    print(f"Dokumenata razvrstanih u poslednjih {DANA} dana: {len(dokumenti)}")

    eng = get_erp_engine()
    spremnih = 0
    ceka_sifru = 0
    ceka_drugo = 0
    puklo = 0
    problemi: Counter = Counter()
    nove_sifre: Counter = Counter()      # (dobavljac, sifra) -> koliko puta
    po_vrsti: Counter = Counter()

    for doc_id, vrsta, pj in dokumenti:
        try:
            nacrt = pripremi(doc_id, korisnik="merenje", engine=eng)
        except Exception as exc:  # noqa: BLE001
            puklo += 1
            problemi[f"PUKLO: {vrsta_problema(str(exc))}"] += 1
            continue

        if nacrt.spreman:
            spremnih += 1
            po_vrsti[(vrsta.value, "spremna")] += 1
            continue

        if nacrt.nemapirane and not nacrt.problemi:
            ceka_sifru += 1
            po_vrsti[(vrsta.value, "ceka sifru")] += 1
        else:
            ceka_drugo += 1
            po_vrsti[(vrsta.value, "ceka drugo")] += 1
        for p in nacrt.problemi:
            problemi[vrsta_problema(p)] += 1
        for n in nacrt.nemapirane:
            nove_sifre[n] += 1

    uk = len(dokumenti)
    if not uk:
        print("Nema dokumenata u tom periodu.")
        return

    print()
    print(f"  kalkulacija spremna za upis : {spremnih:>4}  ({spremnih / uk:.1%})")
    print(f"  čeka samo otvaranje šifre   : {ceka_sifru:>4}  ({ceka_sifru / uk:.1%})")
    print(f"  čeka nešto drugo            : {ceka_drugo:>4}  ({ceka_drugo / uk:.1%})")
    print(f"  generator pukao             : {puklo:>4}")

    print("\n--- Šta zaustavlja (10 najčešćih) ---")
    for p, n in problemi.most_common(10):
        print(f"  {n:>5}  {p}")

    print(f"\n--- Nemapiranih stavki: {sum(nove_sifre.values())} "
          f"({len(nove_sifre)} različitih šifara dobavljača) ---")
    for n, koliko in nove_sifre.most_common(8):
        print(f"  {koliko:>4}x  {n[:70]}")

    print("\n--- Po vrsti dokumenta ---")
    for (vrsta, ishod), n in sorted(po_vrsti.items()):
        print(f"  {vrsta:<12} {ishod:<12} {n:>4}")


if __name__ == "__main__":
    main()
