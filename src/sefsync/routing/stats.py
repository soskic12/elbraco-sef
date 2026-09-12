"""Koliko razvrstavanje zaista pogadja.

Bez potvrde operatera ne postoji razlika izmedju "razvrstano tacno" i "niko
jos nije pogledao". Zato se meri samo ono sto je covek video: potvrdjeno ili
ispravljeno.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import BusinessUnit, Document, RoutingRule, RoutingSource


@dataclass
class Tacnost:
    pregledano: int = 0
    tacno: int = 0
    ispravljeno: int = 0

    @property
    def procenat(self) -> float | None:
        return None if self.pregledano == 0 else 100.0 * self.tacno / self.pregledano


def sazetak(session: Session) -> dict:
    aktivni = Document.archived == False

    def broj(*uslovi) -> int:
        return session.scalar(select(func.count()).select_from(Document).where(*uslovi)) or 0

    # Meri se samo ono gde je aplikacija nesto TVRDILA pa je covek to video:
    #   tacno        - predlog je ostao (izvor nije rucni)
    #   ispravljeno  - predlog je postojao pa ga je operater promenio
    # Rucna dodela dokumenta koji je bio nerazvrstan ne ulazi ni u jedno:
    # tu aplikacija nije imala odgovor, pa nema ni sta da se oceni.
    tacno = broj(
        aktivni,
        Document.routing_confirmed_at.isnot(None),
        Document.routing_corrected == False,
        Document.routing_source != RoutingSource.MANUAL,
    )
    ispravljeno = broj(aktivni, Document.routing_corrected == True)
    tacnost = Tacnost(pregledano=tacno + ispravljeno, tacno=tacno, ispravljeno=ispravljeno)

    po_izvoru = {
        izvor.value: broj(aktivni, Document.routing_source == izvor) for izvor in RoutingSource
    }

    pravila = list(
        session.scalars(
            select(RoutingRule)
            .options(selectinload(RoutingRule.business_unit))
            .order_by(RoutingRule.misses.desc(), RoutingRule.hits.desc())
        )
    )
    sumnjiva = [r for r in pravila if r.misses > 0]

    return {
        "tacnost": tacnost,
        "po_izvoru": po_izvoru,
        "pravila": pravila,
        "sumnjiva": sumnjiva,
        "ukupno": broj(aktivni),
        "nerazvrstano": broj(aktivni, Document.business_unit_id.is_(None)),
        "neprovereno": broj(
            aktivni,
            Document.business_unit_id.isnot(None),
            Document.routing_confirmed_at.is_(None),
        ),
    }
