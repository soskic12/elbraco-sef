"""Storno na SEF-u: kad dobavljac povuce fakturu, panel to mora da sazna.

Sinhronizacija cita SEF-ov pregled po **datumu slanja** fakture, sa preklopom od
nekoliko dana. Mereno 13.10.: `R-26061900` poslata 23.07., stornirana 24.07. -
pregled za 24.07. je ne vraca. Storno fakture starije od preklopa panel zato
nikad ne vidi i kod nas ostaje „Approved“.

Za promene postoji poseban poziv, `purchase-invoice/changes`: svi dogadjaji
jednog dana (samo prosli dani, cuvaju se mesec dana). Storno u njemu nosi tacan
trenutak i broj storna - `2026-6919`: `2026-09-17T08:07:56.7682175Z`,
`ST2026-344`, isto do milisekunde kao u obavestenju koje je Bogdan poslao.

Svaki storno koji panel uoci od sada dobija red `sef.storno` u dnevniku
dokumenta. Po tom redu se zna da je storno nov (Bane, 13.10.: obavestenje se
nudi samo za storna posle uvodjenja, ne za zaostala), i u njemu stoji trenutak
storna, koji SEF trazi kao obavezan.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..db import session_scope
from ..models import AuditLog, Document, SefStatus, Setting

log = logging.getLogger(__name__)

OZNAKA = "sef.storno"
# Storno zatecen pri prvom citanju: status se ispravi, ali se obavestenje ne
# nudi (zaostala storna se ne nude - Bane, 13.10.).
OZNAKA_ZATECEN = "sef.storno.zatecen"
PROMENE_DO = "storno_promene_do"
# Dan od kog je storno „nov“ (nudi se obavestenje). Zapisuje se jednom i posle
# se samo cita. Do 14.10. se granica racunala iz `PROMENE_DO`, pa je drugi
# radnik koji je krenuo dok je prvi vec citao proglasio sva stara storna novim
# (14.10. 08:35: 2026-6919, 26-300-030998, 26-2075-237232, KP-446/26).
UVEDENO = "storno_uvedeno"
# SEF cuva dogadjaje mesec dana; dalje od toga nema sta da se trazi.
NAJVISE_DANA = 30


def je_zabelezen(session, document_id: int, *, i_zatecen: bool = False) -> bool:
    akcije = (OZNAKA, OZNAKA_ZATECEN) if i_zatecen else (OZNAKA,)
    return (
        session.scalar(
            select(AuditLog.id).where(
                AuditLog.document_id == document_id, AuditLog.action.in_(akcije)
            ).limit(1)
        )
        is not None
    )


def zabelezi(
    session,
    doc: Document,
    *,
    kada: str | None = None,
    broj: str | None = None,
    nov: bool = True,
) -> bool:
    """Oznaci dokument kao storniran i zapise to u dnevnik. Vraca da li je novo.

    `nov=False` je storno zatecen pri prvom citanju: status se ispravi (da se za
    stornirani dokument ne upise kalkulacija), a obavestenje se ne nudi.
    """
    if doc.id is None:
        session.flush()
    bio = doc.sef_status
    doc.sef_status = SefStatus.STORNO
    if je_zabelezen(session, doc.id, i_zatecen=True):
        return False
    # Detalj pocinje trenutkom storna, kad ga znamo - `trenutak_storna` ga
    # odatle cita. Iz pregleda trenutak ne stize.
    detalj = " ".join(x for x in (kada, broj and f"storno {broj}") if x) or "uočeno u pregledu"
    if not nov:
        if bio is not SefStatus.STORNO:
            session.add(
                AuditLog(
                    actor="sef", action=OZNAKA_ZATECEN, document_id=doc.id,
                    detail=f"{detalj} (bilo: {bio.value if bio else '?'})"[:500],
                )
            )
        return False
    session.add(AuditLog(actor="sef", action=OZNAKA, document_id=doc.id, detail=detalj[:500]))
    return True


def trenutak_storna(session, document_id: int) -> str | None:
    """Trenutak storna zapisan kad je uocen, ako je stigao iz `changes`."""
    detalj = session.scalar(
        select(AuditLog.detail).where(
            AuditLog.document_id == document_id, AuditLog.action == OZNAKA
        ).limit(1)
    )
    prvi = (detalj or "").split(" ")[0]
    try:
        datetime.fromisoformat(prvi.replace("Z", "+00:00"))
    except ValueError:
        return None
    return prvi


def granica_novih(session, juce: date) -> date:
    """Dan od kog je storno nov. Zapisuje se pri prvom citanju i vise se ne menja.

    Kad granica tek nastaje, zapisi `sef.storno` sa datumom storna pre nje
    prevode se u zatecene - to ispravlja ono sto je 14.10. nastalo pre ove
    popravke.
    """
    red = session.get(Setting, UVEDENO)
    if red is not None and red.value:
        return date.fromisoformat(red.value)
    # Gde se vec citalo (server, 14.10.), granica je dan do kog je procitano -
    # tada je uvedeno. Inace juce.
    vec = session.get(Setting, PROMENE_DO)
    granica = min(date.fromisoformat(vec.value), juce) if vec is not None and vec.value else juce
    session.add(Setting(key=UVEDENO, value=granica.isoformat()))
    for zapis in session.scalars(select(AuditLog).where(AuditLog.action == OZNAKA)):
        prvi = (zapis.detail or "").split(" ")[0]
        try:
            kada = datetime.fromisoformat(prvi.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if kada < granica:
            zapis.action = OZNAKA_ZATECEN
            log.info("Storno #%s od %s je zatecen, ne nov", zapis.document_id, kada)
    session.flush()
    return granica


def procitaj_promene(
    *, klijent=None, danas: date | None = None, od: date | None = None
) -> dict:
    """Cita SEF-ove dogadjaje za protekle dane i belezi storna.

    Pamti dokle je stigao, pa svaki dan cita jednom. Prvi put cita svih 30
    dana koje SEF cuva i ispravlja status zatecenim stornima - mereno 13.10.:
    `2026-6919` stornirana 17.09. i odobrenje `26-2075-237232` stornirano 07.10.
    su kod nas stajali kao „Approved“. Obavestenje se nudi samo za storna od
    juce nadalje; zaostala se namerno ne nude.
    """
    from ..sef.client import SefClient

    danas = danas or date.today()
    juce = danas - timedelta(days=1)
    with session_scope() as session:
        novi_od = granica_novih(session, juce)
        red = session.get(Setting, PROMENE_DO)
        procitano_do = date.fromisoformat(red.value) if red and red.value else None
    najranije = danas - timedelta(days=NAJVISE_DANA)
    if od:
        pocetak = od
    elif procitano_do:
        pocetak = procitano_do + timedelta(days=1)
    else:
        pocetak = najranije
    pocetak = max(pocetak, najranije)

    ishod = {"dana": 0, "storna": 0, "novih": 0, "zatecenih": 0, "nepoznatih": 0}
    if pocetak > juce:
        return ishod

    with (klijent or SefClient()) as sef:
        dan = pocetak
        while dan <= juce:
            dogadjaji = sef.purchase_changes(dan)
            with session_scope() as session:
                for d in dogadjaji:
                    if str(d.get("NewInvoiceStatus") or "") != "Storno":
                        continue
                    ishod["storna"] += 1
                    doc = session.scalar(
                        select(Document).where(
                            Document.sef_invoice_id == int(d.get("PurchaseInvoiceId") or 0)
                        )
                    )
                    if doc is None:
                        ishod["nepoznatih"] += 1
                        continue
                    nov = dan >= novi_od
                    if zabelezi(
                        session, doc, kada=d.get("Date"), broj=d.get("StornoNumber"), nov=nov
                    ):
                        ishod["novih"] += 1
                        log.info("Storno: #%s %s", doc.id, doc.document_number)
                    elif not nov:
                        ishod["zatecenih"] += 1
                red = session.get(Setting, PROMENE_DO)
                if red is None:
                    session.add(Setting(key=PROMENE_DO, value=dan.isoformat()))
                else:
                    red.value = max(red.value or "", dan.isoformat())
            ishod["dana"] += 1
            dan += timedelta(days=1)
    return ishod
