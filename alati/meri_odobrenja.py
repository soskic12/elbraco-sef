"""Meri signal "tacno jedna veza ka fakturi -> magacin 010" na svim odobrenjima.

Istina dolazi iz ERP-a: za knjizno odobrenje koje je covek zaknjizio zna se u
koji je magacin kalkulacija stvarno otisla. To je nezavisno od svega sto app radi.

Ne dira nijednu bazu - samo cita i ispisuje. Trazi vezu ka ERP-u (ERP_DB_URL ili
ERP_SECRETS_FILE) i popunjen billing_reference_count, dakle `sefsync osvezi-ubl`
pre prvog pokretanja.

    .\.venv\Scripts\python.exe alati\meri_odobrenja.py

Pokrenuti ponovo posle svake izmene pravila za knjizna odobrenja - po pravilu rada
od 15.09. nijedno ne ide u rad dok se ne izmeri na svim dokumentima sa poznatim
odgovorom iz ERP-a.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

sys.path.insert(0, "src")

from sqlalchemy import select, text  # noqa: E402

from sefsync.db import session_scope  # noqa: E402
from sefsync.erp.connection import get_erp_engine  # noqa: E402
from sefsync.models import BusinessUnit, Document, DocumentType  # noqa: E402
from sefsync.routing.engine import RoutingEngine  # noqa: E402
from sefsync.services.mapiranje import OD_DANA, kljuc_racuna  # noqa: E402

POVRACAJ = "010"   # reklamacija / povracaj robe
PROMET = "088"     # finansijsko odobrenje na promet (rabat, bonus)


def istina_iz_erpa() -> dict[str, list[tuple[str, str]]]:
    """kljuc racuna -> [(magacin, maticni broj dobavljaca)]"""
    with get_erp_engine().connect() as conn:
        naz = {
            r.a: r.mb
            for r in conn.execute(
                text("SELECT RTRIM(ANALITIKA) a, RTRIM(ISNULL(MATBROJ,'')) mb FROM dbo.NAZIVI")
            )
        }
        po_racunu: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for tab in ("ULKALKUL", "MKALKUL"):
            for r in conn.execute(
                text(
                    f"SELECT RTRIM(BROJ_RN_DOB) rn, RTRIM(MAGACIN) mag, RTRIM(DOBAVLJAC) dob "
                    f"FROM dbo.{tab} WHERE DATUM >= :od"
                ),
                {"od": OD_DANA},
            ):
                if r.rn:
                    po_racunu[kljuc_racuna(r.rn)].append((r.mag, naz.get(r.dob, "")))
    return po_racunu


def polja(doc: Document) -> dict:
    """Isto sto ingest daje mehanizmu razvrstavanja."""
    return {
        "delivery_address": doc.delivery_address,
        "delivery_city": doc.delivery_city,
        "delivery_name": doc.delivery_name,
        "buyer_address": doc.buyer_address,
        "buyer_city": doc.buyer_city,
        "buyer_reference": doc.buyer_reference,
        "order_reference": doc.order_reference,
        "contract_reference": doc.contract_reference,
        "additional_reference": doc.additional_reference,
        "billing_reference": doc.billing_reference,
        "attachment_text": doc.attachment_text,
        "note": doc.note,
        "supplier_vat": doc.supplier_vat,
        "supplier_name": doc.supplier_name,
        "document_number": doc.document_number,
        "item_text": " | ".join(
            [x.name for x in doc.lines if x.name] + [x.note for x in doc.lines if x.note]
        ) or None,
        "document_type": doc.document_type.value,
    }


def main() -> None:
    po_racunu = istina_iz_erpa()
    print(f"ERP: {len(po_racunu)} brojeva racuna sa kalkulacijom")

    with session_scope() as session:
        motor = RoutingEngine.from_db(session)
        magacin_pj = {
            u.id: (u.erp_code or "").strip()
            for u in session.scalars(select(BusinessUnit))
        }
        odobrenja = list(
            session.scalars(
                select(Document).where(Document.document_type == DocumentType.CREDIT_NOTE)
            )
        )
        print(f"Panel: {len(odobrenja)} knjiznih odobrenja")

        ukrstanje: Counter = Counter()      # (broj veza, magacin) -> koliko
        slojevi: Counter = Counter()        # (ishod pravila, broj veza, magacin)
        sada: Counter = Counter()           # kako razvrstava zatecen mehanizam
        nespareno = 0
        dvosmisleno = 0

        for doc in odobrenja:
            kandidati = po_racunu.get(kljuc_racuna(doc.document_number), [])
            if not kandidati:
                nespareno += 1
                continue
            magacini = {m for m, _ in kandidati}
            if len(magacini) > 1:
                po_dobavljacu = {
                    m for m, mb in kandidati if mb and mb == (doc.supplier_reg_no or "")
                }
                if len(po_dobavljacu) != 1:
                    dvosmisleno += 1
                    continue
                magacini = po_dobavljacu
            stvarni = magacini.pop()

            veza = doc.billing_reference_count
            grupa = "0" if veza == 0 else ("1" if veza == 1 else "2+")
            ukrstanje[(grupa, stvarni)] += 1

            odluka = motor.decide(polja(doc))
            predlog = magacin_pj.get(odluka.business_unit_id, "") if odluka.assigned else ""
            ishod = "pogodak" if predlog == stvarni else ("promasaj" if predlog else "nema")
            sada[ishod] += 1
            slojevi[(ishod, grupa, stvarni)] += 1

        sparenih = sum(ukrstanje.values())
        print(f"Spareno sa ERP-om: {sparenih}   (nespareno {nespareno}, dvosmisleno {dvosmisleno})")

        print("\n--- Stvarni magacin po broju veza ka fakturi ---")
        magacini = sorted({m for _, m in ukrstanje})
        vodeci = [m for m in (POVRACAJ, PROMET) if m in magacini]
        ostali = [m for m in magacini if m not in vodeci]
        print(f"{'veza':>5} | " + " | ".join(f"{m:>6}" for m in vodeci) + f" | {'ostalo':>6} | {'ukupno':>6}")
        for grupa in ("0", "1", "2+"):
            red = [ukrstanje[(grupa, m)] for m in vodeci]
            rest = sum(ukrstanje[(grupa, m)] for m in ostali)
            print(f"{grupa:>5} | " + " | ".join(f"{x:>6}" for x in red) + f" | {rest:>6} | {sum(red)+rest:>6}")

        print("\n--- Predlog: tacno jedna veza -> 010 ---")
        jedna = sum(v for (g, _), v in ukrstanje.items() if g == "1")
        jedna_tacno = ukrstanje[("1", POVRACAJ)]
        if jedna:
            print(f"odluka na {jedna} dokumenata, tacnih {jedna_tacno} ({jedna_tacno / jedna:.1%})")
        ostatak = sparenih - jedna
        ostatak_088 = sum(
            v for (g, m), v in ukrstanje.items() if g != "1" and m == PROMET
        )
        if ostatak:
            print(f"ostalo (0 ili 2+ veze) -> 088: {ostatak} dokumenata, "
                  f"tacnih {ostatak_088} ({ostatak_088 / ostatak:.1%})")

        print("\n--- Zateceni mehanizam na istim dokumentima ---")
        uk = sum(sada.values())
        for kljuc in ("pogodak", "promasaj", "nema"):
            print(f"{kljuc:>9}: {sada[kljuc]:>5} ({sada[kljuc] / uk:.1%})" if uk else kljuc)
        odluceno = sada["pogodak"] + sada["promasaj"]
        if odluceno:
            print(f"tacnost gde odluci: {sada['pogodak'] / odluceno:.1%}, "
                  f"pokrivenost: {odluceno / uk:.1%}")

        print("\n--- Sta veza dodaje TAMO GDE PRAVILA CUTE ---")
        for grupa in ("0", "1", "2+"):
            p010 = slojevi[("nema", grupa, POVRACAJ)]
            p088 = slojevi[("nema", grupa, PROMET)]
            uk_g = p010 + p088
            if uk_g:
                print(f"  {grupa:>2} veza: {uk_g:>4} dokumenata — 010: {p010:>4}, 088: {p088:>4} "
                      f"(010 bi bilo tacno u {p010 / uk_g:.1%})")

        print("\n--- Gde pravilo promasi, sta bi rekla veza ---")
        for grupa in ("0", "1", "2+"):
            p010 = slojevi[("promasaj", grupa, POVRACAJ)]
            p088 = slojevi[("promasaj", grupa, PROMET)]
            if p010 + p088:
                print(f"  {grupa:>2} veza: {p010 + p088:>3} promasaja — stvarno 010: {p010}, 088: {p088}")


if __name__ == "__main__":
    main()
