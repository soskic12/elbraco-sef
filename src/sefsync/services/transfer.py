"""Prenos sifarnika i pravila izmedju instalacija (razvojna masina -> server).

Dokumenti se uvek mogu ponovo preuzeti sa SEF-a, ali pravila razvrstavanja i
ono sto je operater rucno podesio ne mogu - to je rad koji postoji samo u bazi
aplikacije. Zato ide izvoz u citljiv JSON pa uvoz na drugoj instalaciji.

Poslovne jedinice se povezuju po `code`, ne po internom id-u, jer se id-evi
izmedju baza ne poklapaju.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..db import session_scope
from ..models import (
    BusinessUnit,
    DocumentType,
    ItemMapping,
    MatchField,
    MatchOp,
    RoutingRule,
    UnitKind,
)

log = logging.getLogger(__name__)

VERZIJA = 1


def izvezi() -> dict:
    """Sifarnik, pravila i mapiranja artikala u jedan recnik."""
    with session_scope() as session:
        jedinice = list(session.scalars(select(BusinessUnit).order_by(BusinessUnit.code)))
        pravila = list(
            session.scalars(select(RoutingRule).order_by(RoutingRule.priority, RoutingRule.id))
        )
        mapiranja = list(session.scalars(select(ItemMapping)))

        return {
            "verzija": VERZIJA,
            "izvezeno": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "poslovne_jedinice": [
                {
                    "code": u.code,
                    "name": u.name,
                    "kind": u.kind.value,
                    "erp_code": u.erp_code,
                    "address": u.address,
                    "city": u.city,
                    "postal_code": u.postal_code,
                    "emails": u.emails,
                    "phones": u.phones,
                    "active": u.active,
                    "routable": u.routable,
                    "note": u.note,
                }
                for u in jedinice
            ],
            "pravila": [
                {
                    "priority": r.priority,
                    "field": r.field.value,
                    "op": r.op.value,
                    "pattern": r.pattern,
                    "supplier_vat": r.supplier_vat,
                    "document_type": r.document_type.value if r.document_type else None,
                    "business_unit": r.business_unit.code,
                    "active": r.active,
                    "comment": r.comment,
                }
                for r in pravila
            ],
            "mapiranja_artikala": [
                {
                    "supplier_vat": m.supplier_vat,
                    "supplier_item_id": m.supplier_item_id,
                    "barcode": m.barcode,
                    "supplier_item_name": m.supplier_item_name,
                    "erp_item_code": m.erp_item_code,
                    "unit_factor": m.unit_factor,
                    "active": m.active,
                }
                for m in mapiranja
            ],
        }


def upisi_u_fajl(putanja: Path) -> dict:
    podaci = izvezi()
    putanja.parent.mkdir(parents=True, exist_ok=True)
    putanja.write_text(
        json.dumps(podaci, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return podaci


def uvezi(podaci: dict, i_jedinice: bool = False) -> dict[str, int]:
    """Upisuje pravila (i po zelji sifarnik). Vraca brojac po vrsti.

    Pravila se prepoznaju po (polje, operator, sablon, PJ, vrsta dokumenta) -
    ponovni uvoz istog fajla nista ne duplira.
    """
    if podaci.get("verzija") != VERZIJA:
        raise ValueError(f"Nepoznata verzija izvoza: {podaci.get('verzija')}")

    brojac = {"jedinice_nove": 0, "jedinice_azurirane": 0, "pravila_nova": 0,
              "pravila_postojeca": 0, "mapiranja_nova": 0, "preskoceno": 0}

    with session_scope() as session:
        if i_jedinice:
            for red in podaci.get("poslovne_jedinice", []):
                jedinica = session.scalar(
                    select(BusinessUnit).where(BusinessUnit.code == red["code"])
                )
                if jedinica is None:
                    jedinica = BusinessUnit(code=red["code"], name=red["name"])
                    session.add(jedinica)
                    brojac["jedinice_nove"] += 1
                else:
                    brojac["jedinice_azurirane"] += 1
                jedinica.name = red["name"]
                jedinica.kind = UnitKind(red["kind"])
                for polje in ("erp_code", "address", "city", "postal_code",
                              "emails", "phones", "note"):
                    if red.get(polje):
                        setattr(jedinica, polje, red[polje])
                jedinica.active = bool(red.get("active", True))
                jedinica.routable = bool(red.get("routable", True))
            session.flush()

        # sifre -> id, da se pravila zakace na PJ ove instalacije
        po_sifri = {
            u.code: u.id for u in session.scalars(select(BusinessUnit))
        }

        for red in podaci.get("pravila", []):
            sifra = red["business_unit"]
            if sifra not in po_sifri:
                log.warning("Pravilo preskočeno — nema poslovne jedinice %s", sifra)
                brojac["preskoceno"] += 1
                continue

            vrsta = DocumentType(red["document_type"]) if red.get("document_type") else None
            postoji = session.scalar(
                select(RoutingRule).where(
                    RoutingRule.field == MatchField(red["field"]),
                    RoutingRule.op == MatchOp(red["op"]),
                    RoutingRule.pattern == red["pattern"],
                    RoutingRule.business_unit_id == po_sifri[sifra],
                    RoutingRule.document_type == vrsta,
                )
            )
            if postoji is not None:
                brojac["pravila_postojeca"] += 1
                continue

            session.add(
                RoutingRule(
                    priority=red.get("priority", 100),
                    field=MatchField(red["field"]),
                    op=MatchOp(red["op"]),
                    pattern=red["pattern"],
                    supplier_vat=red.get("supplier_vat"),
                    document_type=vrsta,
                    business_unit_id=po_sifri[sifra],
                    active=bool(red.get("active", True)),
                    comment=red.get("comment"),
                )
            )
            brojac["pravila_nova"] += 1

        for red in podaci.get("mapiranja_artikala", []):
            postoji = session.scalar(
                select(ItemMapping).where(
                    ItemMapping.supplier_vat == red["supplier_vat"],
                    ItemMapping.supplier_item_id == red.get("supplier_item_id"),
                    ItemMapping.barcode == red.get("barcode"),
                )
            )
            if postoji is None:
                session.add(ItemMapping(**red))
                brojac["mapiranja_nova"] += 1

    return brojac


def procitaj_iz_fajla(putanja: Path, i_jedinice: bool = False) -> dict[str, int]:
    podaci = json.loads(Path(putanja).read_text(encoding="utf-8"))
    return uvezi(podaci, i_jedinice=i_jedinice)
