"""Radni panel za ulazne e-fakture sa SEF-a.

Dva ekrana, dve uloge:

  OPERATER    — jedan red sa svim dokumentima koji traže potez: potvrdi ili
                ispravi poslovnu jedinicu, prihvati ili odbij na SEF-u,
                prosledi poslovnoj jedinici. Prihvatanje i prosleđivanje su
                nezavisni: operater bira redosled.
  POSLOVOĐA   — vidi samo svoju jedinicu i samo ono što mu je prosleđeno,
                i potvrđuje da je roba stigla.

Dokument napušta red tek kad je i na SEF-u odlučeno i prosleđen.
Pokretanje:  sefsync serve
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware

from ..config import get_settings
from ..db import init_db, session_scope
from ..logging_setup import setup_logging
from ..models import (
    AuditLog,
    BusinessUnit,
    Document,
    DocumentType,
    MatchField,
    MatchOp,
    Notification,
    ProcessState,
    RoutingRule,
    SefStatus,
    SyncRun,
    UnitKind,
)
from ..notify.dispatcher import TYPE_LABEL
from ..services.acceptance import AcceptanceService
from ..services.ingest import IngestService
from ..services.workflow import Workflow
from .auth import AuthError, Role, User, authenticate

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tip"] = lambda t: TYPE_LABEL.get(t, str(t))

ODLUCENO = [SefStatus.APPROVED, SefStatus.REJECTED, SefStatus.STORNO]


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging(naziv="sefsync")
    init_db()
    log.info("Panel pokrenut (SEF: %s)", get_settings().base_url)
    yield


app = FastAPI(
    title="ELBRACO — e-fakture (SEF)",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(SessionMiddleware, secret_key=get_settings().web_secret, max_age=12 * 3600)


# --------------------------------------------------------------------------- #
# prijava
# --------------------------------------------------------------------------- #


def current_user(request: Request) -> User:
    data = request.session.get("user")
    if not data:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/prijava"})
    return User(
        login=data["login"],
        name=data["name"],
        role=Role(data["role"]),
        unit_code=data.get("unit_code"),
        function=data.get("function", ""),
    )


def operator_only(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_operator:
        raise HTTPException(403, "Ovaj ekran je za operatera.")
    return user


Korisnik = Annotated[User, Depends(current_user)]
Operater = Annotated[User, Depends(operator_only)]


@app.get("/prijava", response_class=HTMLResponse)
def login_form(request: Request, greska: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"greska": greska, "user": None})


@app.post("/prijava")
def login(
    request: Request,
    korisnik: Annotated[str, Form()],
    lozinka: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        user = authenticate(korisnik.strip(), lozinka)
    except AuthError as exc:
        return RedirectResponse(f"/prijava?greska={quote(str(exc))}", status_code=303)

    request.session["user"] = {
        "login": user.login,
        "name": user.name,
        "role": user.role.value,
        "unit_code": user.unit_code,
        "function": user.function,
    }
    log.info("Prijava: %s (%s)", user.login, user.role.value)
    return RedirectResponse("/", status_code=303)


@app.get("/odjava")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/prijava", status_code=303)


# --------------------------------------------------------------------------- #
# pomocno
# --------------------------------------------------------------------------- #


def _back(url: str, message: str, ok: bool = True) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    prefix = "" if ok else "GREŠKA: "
    return RedirectResponse(f"{url}{sep}poruka={quote(prefix + message)}", status_code=303)


def _unit_for(session, user: User) -> BusinessUnit | None:
    """Poslovna jedinica poslovodje - po sifri objekta iz sifarnika osoblja."""
    if not user.unit_code:
        return None
    code = user.unit_code.strip()
    unit = session.scalar(
        select(BusinessUnit).where(
            BusinessUnit.erp_code == code, BusinessUnit.kind == UnitKind.RETAIL
        )
    )
    return unit or session.scalar(select(BusinessUnit).where(BusinessUnit.erp_code == code))


def _broj(vrednost: str | int | None) -> int | None:
    """Prazan izbor u formi je "nije birano", a ne nula."""
    if vrednost is None or vrednost == "":
        return None
    try:
        return int(vrednost)
    except (TypeError, ValueError):
        return None


def _ids(raw: list[str]) -> list[int]:
    out: list[int] = []
    for value in raw:
        for part in str(value).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out


def _queue_filter(query, prikaz: str):
    """Koji dokumenti pripadaju kojem prikazu."""
    aktivni = Document.archived == False
    if prikaz == "arhiva":
        return query.where(Document.archived == True)
    if prikaz == "nerazvrstano":
        return query.where(aktivni, Document.business_unit_id.is_(None))
    if prikaz == "ceka_prijem":
        return query.where(
            aktivni, Document.forwarded_at.isnot(None), Document.received_at.is_(None)
        )
    # "red": traži potez dok i SEF i prosleđivanje nisu gotovi
    return query.where(
        aktivni,
        or_(Document.forwarded_at.is_(None), Document.sef_status.notin_(ODLUCENO)),
    )


# --------------------------------------------------------------------------- #
# operaterov red
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: Korisnik):
    if not user.is_operator:
        return RedirectResponse("/moja-jedinica", status_code=303)
    return queue(request, user)


@app.get("/red", response_class=HTMLResponse)
def queue(
    request: Request,
    user: Operater,
    pj: str | None = None,
    tip: str | None = None,
    q: str | None = None,
    prikaz: str = "red",
    strana: str = "1",
) -> HTMLResponse:
    # Izbor "— sve —" u formi stize kao prazan string, a ne kao izostanak
    # parametra, pa se ne moze primiti kao int.
    pj = _broj(pj)
    strana = _broj(strana) or 1
    per_page = 60
    with session_scope() as session:
        query = _queue_filter(select(Document).options(selectinload(Document.business_unit)), prikaz)

        if pj == 0:
            query = query.where(Document.business_unit_id.is_(None))
        elif pj:
            query = query.where(Document.business_unit_id == pj)
        if tip:
            query = query.where(Document.document_type == DocumentType(tip))
        if q:
            needle = f"%{q.strip()}%"
            query = query.where(
                or_(
                    Document.document_number.ilike(needle),
                    Document.supplier_name.ilike(needle),
                    Document.supplier_vat.ilike(needle),
                    Document.delivery_address.ilike(needle),
                )
            )

        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = list(
            session.scalars(
                query.order_by(Document.sent_date.desc(), Document.id.desc())
                .offset((max(1, strana) - 1) * per_page)
                .limit(per_page)
            )
        )
        units = list(
            session.scalars(
                select(BusinessUnit)
                .where(BusinessUnit.active == True)
                .order_by(BusinessUnit.kind, BusinessUnit.code)
            )
        )
        brojaci = {
            naziv: session.scalar(
                select(func.count()).select_from(_queue_filter(select(Document), naziv).subquery())
            )
            for naziv in ("red", "nerazvrstano", "ceka_prijem", "arhiva")
        }
        last_run = session.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
        return templates.TemplateResponse(
            request,
            "queue.html",
            {
                "documents": rows,
                "units": units,
                "brojaci": brojaci,
                "last_run": last_run,
                "total": total,
                "page": max(1, strana),
                "pages": max(1, (total + per_page - 1) // per_page),
                "prikaz": prikaz,
                "filters": {"pj": pj, "tip": tip, "q": q},
                "types": list(DocumentType),
                "user": user,
            },
        )


# --------------------------------------------------------------------------- #
# radnje nad dokumentima
# --------------------------------------------------------------------------- #


@app.post("/prosledi")
def forward(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    rezultat = Workflow().forward(_ids(dokumenti), actor=user.login)
    return _back(povratak, rezultat.summary(), rezultat.ok)


@app.post("/prihvati")
def accept_many(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    """Grupno prihvatanje. Odbijanje ide pojedinacno - trazi obrazlozenje."""
    servis = AcceptanceService()
    prihvaceno, problemi = 0, []
    for doc_id in _ids(dokumenti):
        ishod = servis.accept(doc_id, actor=user.login)
        if ishod.ok:
            prihvaceno += 1
        else:
            problemi.append(f"#{doc_id}: {ishod.message}")
    poruka = f"Prihvaćeno na SEF-u: {prihvaceno}."
    if problemi:
        poruka += " " + "; ".join(problemi[:3])
    return _back(povratak, poruka, prihvaceno > 0 or not problemi)


@app.post("/potvrdi")
def confirm_routing(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    """Predlog je tacan - bez ove potvrde se tacnost razvrstavanja ne moze meriti."""
    rezultat = Workflow().confirm_routing(_ids(dokumenti), actor=user.login)
    return _back(povratak, rezultat.summary(), rezultat.ok)


@app.post("/arhiviraj")
def archive(
    user: Operater,
    dokumenti: Annotated[list[str], Form()],
    povratak: Annotated[str, Form()] = "/red",
) -> RedirectResponse:
    rezultat = Workflow().archive(_ids(dokumenti), actor=user.login)
    return _back(povratak, rezultat.summary(), rezultat.ok)


@app.post("/dokument/{doc_id}/vrati")
def unarchive(doc_id: int, user: Operater) -> RedirectResponse:
    rezultat = Workflow().unarchive(doc_id, actor=user.login)
    return _back(f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/pj")
def assign_unit(
    doc_id: int,
    user: Operater,
    business_unit_id: Annotated[int, Form()],
    napravi_pravilo: Annotated[str, Form()] = "",
    polje: Annotated[str, Form()] = MatchField.DELIVERY_ADDRESS.value,
    povratak: Annotated[str, Form()] = "",
) -> RedirectResponse:
    rezultat = Workflow().assign_unit(
        doc_id,
        business_unit_id,
        actor=user.login,
        remember=bool(napravi_pravilo),
        field_name=polje,
    )
    return _back(povratak or f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/prihvati")
def accept(
    doc_id: int,
    user: Operater,
    komentar: Annotated[str, Form()] = "",
    povratak: Annotated[str, Form()] = "",
) -> RedirectResponse:
    rezultat = AcceptanceService().accept(doc_id, actor=user.login, comment=komentar)
    return _back(povratak or f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/odbij")
def reject(
    doc_id: int,
    user: Operater,
    komentar: Annotated[str, Form()] = "",
    povratak: Annotated[str, Form()] = "",
) -> RedirectResponse:
    rezultat = AcceptanceService().reject(doc_id, actor=user.login, comment=komentar)
    return _back(povratak or f"/dokument/{doc_id}", rezultat.message, rezultat.ok)


@app.post("/dokument/{doc_id}/osvezi")
def refresh(doc_id: int, user: Operater) -> RedirectResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        sef_id = doc.sef_invoice_id
    IngestService().fetch_one(sef_id, force=True)
    return _back(f"/dokument/{doc_id}", "Dokument je ponovo preuzet sa SEF-a.")


@app.get("/dokument/{doc_id}", response_class=HTMLResponse)
def document_detail(request: Request, doc_id: int, user: Korisnik) -> HTMLResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id or doc.forwarded_at is None:
                raise HTTPException(403, "Ovaj dokument nije prosleđen tvojoj jedinici.")
        units = list(
            session.scalars(
                select(BusinessUnit).where(BusinessUnit.active == True).order_by(BusinessUnit.code)
            )
        )
        notifications = list(
            session.scalars(
                select(Notification)
                .where(Notification.document_id == doc_id)
                .order_by(Notification.id.desc())
            )
        )
        audit = list(
            session.scalars(
                select(AuditLog).where(AuditLog.document_id == doc_id).order_by(AuditLog.id.desc())
            )
        )
        return templates.TemplateResponse(
            request,
            "document.html",
            {
                "doc": doc,
                "lines": doc.lines,
                "units": units,
                "notifications": notifications,
                "audit": audit,
                "match_fields": list(MatchField),
                "user": user,
            },
        )


@app.get("/dokument/{doc_id}/ubl")
def document_ubl(doc_id: int, user: Korisnik) -> FileResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None or not doc.ubl_path or not Path(doc.ubl_path).exists():
            raise HTTPException(404, "UBL fajl nije dostupan.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")
        return FileResponse(
            doc.ubl_path,
            media_type="application/xml",
            filename=f"{doc.document_number or doc.sef_invoice_id}.xml",
        )


# --------------------------------------------------------------------------- #
# poslovodja
# --------------------------------------------------------------------------- #


@app.get("/moja-jedinica", response_class=HTMLResponse)
def my_unit(request: Request, user: Korisnik, prikaz: str = "novo"):
    with session_scope() as session:
        unit = _unit_for(session, user)
        if unit is None:
            if user.is_operator:
                return RedirectResponse("/red", status_code=303)
            raise HTTPException(
                403, f"Za šifru objekta {user.unit_code} nema poslovne jedinice u šifarniku."
            )

        query = select(Document).where(
            Document.business_unit_id == unit.id, Document.forwarded_at.isnot(None)
        )
        if prikaz == "novo":
            query = query.where(Document.received_at.is_(None))
        rows = list(session.scalars(query.order_by(Document.forwarded_at.desc()).limit(200)))
        ceka = session.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.business_unit_id == unit.id,
                Document.forwarded_at.isnot(None),
                Document.received_at.is_(None),
            )
        )
        return templates.TemplateResponse(
            request,
            "unit.html",
            {"unit": unit, "documents": rows, "ceka": ceka, "prikaz": prikaz, "user": user},
        )


@app.post("/dokument/{doc_id}/prijem")
def confirm_receipt(
    doc_id: int,
    user: Korisnik,
    napomena: Annotated[str, Form()] = "",
) -> RedirectResponse:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "Dokument ne postoji.")
        if not user.is_operator:
            moja = _unit_for(session, user)
            if moja is None or doc.business_unit_id != moja.id:
                raise HTTPException(403, "Nije tvoj dokument.")
    rezultat = Workflow().confirm_receipt(doc_id, actor=user.login, note=napomena)
    povratak = f"/dokument/{doc_id}" if user.is_operator else "/moja-jedinica"
    return _back(povratak, rezultat.message, rezultat.ok)


# --------------------------------------------------------------------------- #
# sifarnici
# --------------------------------------------------------------------------- #


@app.get("/tacnost", response_class=HTMLResponse)
def accuracy_page(request: Request, user: Operater) -> HTMLResponse:
    """Koliko razvrstavanje pogadja i koja pravila gresе."""
    from ..routing.stats import sazetak

    with session_scope() as session:
        podaci = sazetak(session)
        return templates.TemplateResponse(
            request, "accuracy.html", {**podaci, "user": user}
        )


@app.get("/pj", response_class=HTMLResponse)
def units_page(request: Request, user: Operater) -> HTMLResponse:
    with session_scope() as session:
        units = list(
            session.scalars(select(BusinessUnit).order_by(BusinessUnit.kind, BusinessUnit.code))
        )
        counts = dict(
            session.execute(
                select(Document.business_unit_id, func.count()).group_by(Document.business_unit_id)
            ).all()
        )
        return templates.TemplateResponse(
            request,
            "units.html",
            {"units": units, "counts": counts, "kinds": list(UnitKind), "user": user},
        )


@app.post("/pj")
def unit_save(
    user: Operater,
    code: Annotated[str, Form()],
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()] = UnitKind.RETAIL.value,
    erp_code: Annotated[str, Form()] = "",
    address: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    postal_code: Annotated[str, Form()] = "",
    emails: Annotated[str, Form()] = "",
    phones: Annotated[str, Form()] = "",
    unit_id: Annotated[int, Form()] = 0,
    active: Annotated[str, Form()] = "",
    routable: Annotated[str, Form()] = "",
) -> RedirectResponse:
    with session_scope() as session:
        unit = session.get(BusinessUnit, unit_id) if unit_id else None
        if unit is None:
            unit = BusinessUnit(code=code.strip(), name=name.strip())
            session.add(unit)
        unit.code = code.strip()
        unit.name = name.strip()
        unit.kind = UnitKind(kind)
        unit.erp_code = erp_code.strip() or None
        unit.address = address.strip() or None
        unit.city = city.strip() or None
        unit.postal_code = postal_code.strip() or None
        unit.emails = emails.strip() or None
        unit.phones = phones.strip() or None
        unit.active = bool(active)
        unit.routable = bool(routable)
    return _back("/pj", "Poslovna jedinica je sačuvana.")


@app.get("/pravila", response_class=HTMLResponse)
def rules_page(request: Request, user: Operater) -> HTMLResponse:
    with session_scope() as session:
        rules = list(
            session.scalars(
                select(RoutingRule)
                .options(selectinload(RoutingRule.business_unit))
                .order_by(RoutingRule.priority, RoutingRule.id)
            )
        )
        units = list(session.scalars(select(BusinessUnit).order_by(BusinessUnit.code)))
        return templates.TemplateResponse(
            request,
            "rules.html",
            {
                "rules": rules,
                "units": units,
                "fields": list(MatchField),
                "ops": list(MatchOp),
                "types": list(DocumentType),
                "user": user,
            },
        )


@app.post("/pravila")
def rule_save(
    user: Operater,
    field: Annotated[str, Form()],
    op: Annotated[str, Form()],
    pattern: Annotated[str, Form()],
    business_unit_id: Annotated[int, Form()],
    priority: Annotated[int, Form()] = 100,
    supplier_vat: Annotated[str, Form()] = "",
    document_type: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    rule_id: Annotated[int, Form()] = 0,
) -> RedirectResponse:
    with session_scope() as session:
        rule = session.get(RoutingRule, rule_id) if rule_id else None
        if rule is None:
            rule = RoutingRule(
                field=MatchField(field),
                op=MatchOp(op),
                pattern=pattern,
                business_unit_id=business_unit_id,
            )
            session.add(rule)
        rule.field = MatchField(field)
        rule.op = MatchOp(op)
        rule.pattern = pattern.strip()
        rule.business_unit_id = business_unit_id
        rule.priority = priority
        rule.supplier_vat = supplier_vat.strip() or None
        rule.document_type = DocumentType(document_type) if document_type else None
        rule.comment = comment.strip() or None
    return _back("/pravila", "Pravilo je sačuvano.")


@app.post("/pravila/{rule_id}/obrisi")
def rule_delete(rule_id: int, user: Operater) -> RedirectResponse:
    with session_scope() as session:
        rule = session.get(RoutingRule, rule_id)
        if rule is not None:
            session.delete(rule)
    return _back("/pravila", "Pravilo je obrisano.")


@app.post("/pravila/primeni")
def rules_apply(user: Operater, background: BackgroundTasks) -> RedirectResponse:
    def job() -> None:
        service = IngestService()
        with session_scope() as session:
            ids = list(
                session.scalars(
                    select(Document.id).where(
                        Document.business_unit_id.is_(None), Document.archived == False
                    )
                )
            )
        for doc_id in ids:
            try:
                service.reroute(doc_id)
            except Exception:  # noqa: BLE001
                log.exception("Ponovno razvrstavanje dokumenta %s nije uspelo", doc_id)

    background.add_task(job)
    return _back("/pravila", "Ponovno razvrstavanje je pokrenuto.")


# --------------------------------------------------------------------------- #
# sinhronizacija i API
# --------------------------------------------------------------------------- #


@app.post("/sync")
def run_sync(
    user: Operater,
    background: BackgroundTasks,
    od: Annotated[str, Form()] = "",
    do: Annotated[str, Form()] = "",
) -> RedirectResponse:
    date_from = date.fromisoformat(od) if od else None
    date_to = date.fromisoformat(do) if do else None
    background.add_task(lambda: IngestService().sync(date_from, date_to))
    return _back("/red", "Preuzimanje sa SEF-a je pokrenuto.")


@app.get("/api/dokumenti")
def api_documents(
    user: Korisnik,
    stanje: str | None = None,
    pj: str | None = None,
    limit: int = Query(200, le=1000),
) -> JSONResponse:
    with session_scope() as session:
        query = select(Document).order_by(Document.id.desc()).limit(limit)
        if stanje:
            query = query.where(Document.state == ProcessState(stanje))
        if pj:
            query = query.join(BusinessUnit).where(BusinessUnit.code == pj)
        if not user.is_operator:
            moja = _unit_for(session, user)
            query = query.where(Document.business_unit_id == (moja.id if moja else -1))
        docs = list(session.scalars(query))
        return JSONResponse(
            [
                {
                    "id": d.id,
                    "sef_invoice_id": d.sef_invoice_id,
                    "broj": d.document_number,
                    "tip": d.document_type.value,
                    "dobavljac": d.supplier_name,
                    "pib": d.supplier_vat,
                    "iznos": d.amount,
                    "valuta": d.currency,
                    "datum_prometa": d.delivery_date.isoformat() if d.delivery_date else None,
                    "poslovna_jedinica": d.business_unit.code if d.business_unit else None,
                    "sef_status": d.sef_status.value,
                    "prosledjeno": d.forwarded_at.isoformat() if d.forwarded_at else None,
                    "prijem_potvrdjen": d.received_at.isoformat() if d.received_at else None,
                    "stavki": len(d.lines),
                }
                for d in docs
            ]
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
