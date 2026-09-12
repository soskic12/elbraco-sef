"""Komandna linija: sefsync <komanda>."""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import typer

from .config import get_settings
from .db import init_db, session_scope
from .logging_setup import setup_logging


def _force_utf8() -> None:
    """Windows konzola je podrazumevano cp1252 - srpska slova bi rusila ispis."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # pragma: no cover
            pass


_force_utf8()

app = typer.Typer(add_completion=False, help="Preuzimanje i obrada ulaznih e-faktura sa SEF-a.")
log = logging.getLogger("sefsync.cli")


def _boot(naziv: str = "sefsync-cli") -> None:
    setup_logging(naziv=naziv)
    init_db()


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


# --------------------------------------------------------------------------- #
# osnovno
# --------------------------------------------------------------------------- #


@app.command("init-db")
def cmd_init_db() -> None:
    """Kreira tabele u bazi aplikacije."""
    _boot()
    from .db import get_engine, proveri_unicode

    # bez lozinke - ovaj ispis zavrsi u logovima i na tudjim ekranima
    typer.echo(f"Baza spremna: {get_engine().url.render_as_string(hide_password=True)}")

    sumnjive = proveri_unicode()
    if sumnjive:
        typer.echo("")
        typer.secho(
            f"UPOZORENJE: {len(sumnjive)} kolona je VARCHAR umesto NVARCHAR.", fg="red"
        )
        typer.echo("Ćirilica se u njima ne može sačuvati — slova postanu upitnici.")
        for kolona in sumnjive[:6]:
            typer.echo(f"  {kolona}")
        if len(sumnjive) > 6:
            typer.echo(f"  ... i još {len(sumnjive) - 6}")
        typer.echo("")
        typer.echo("Tip postojeće kolone se ne može promeniti dopunom šeme —")
        typer.echo("bazu treba napraviti iznova (DROP DATABASE pa sql/baza_panela.sql).")


@app.command("check")
def cmd_check() -> None:
    """Provera konekcije i API ključa ka SEF-u."""
    _boot()
    from .sef.client import SefClient, SefError

    settings = get_settings()
    typer.echo(f"Okruženje : {settings.sef_env} ({settings.base_url})")
    typer.echo(f"API ključ  : {'postavljen' if settings.sef_api_key else 'NIJE POSTAVLJEN'}")
    try:
        with SefClient(settings) as client:
            typer.echo(f"Verzija SEF-a: {client.version()}")
            if settings.company_vat and settings.company_reg_no:
                registered = client.company_registered(
                    vat_number=settings.company_vat,
                    registration_number=settings.company_reg_no,
                )
                typer.echo(f"PIB {settings.company_vat} registrovan na SEF-u: {registered}")
            elif settings.company_vat:
                typer.echo("Provera naloga preskočena — dodaj COMPANY_REG_NO (matični broj) u .env")
    except SefError as exc:
        typer.secho(f"Greška: {exc}", fg="red")
        raise typer.Exit(1) from exc


@app.command("subscribe")
def cmd_subscribe() -> None:
    """Pretplata na SEF notifikacije o promenama statusa (od narednog dana)."""
    _boot()
    from .sef.client import SefClient

    with SefClient() as client:
        typer.echo(f"Pretplata uspešna, ključ: {client.subscribe()}")


# --------------------------------------------------------------------------- #
# sinhronizacija
# --------------------------------------------------------------------------- #


@app.command("sync")
def cmd_sync(
    od: str = typer.Option(None, help="Datum od (YYYY-MM-DD)."),
    do: str = typer.Option(None, help="Datum do (YYYY-MM-DD)."),
    dana: int = typer.Option(None, help="Alternativa: poslednjih N dana."),
) -> None:
    """Preuzima ulazne dokumente sa SEF-a, razvrstava ih i šalje obaveštenja."""
    _boot()
    from .services.ingest import IngestService

    date_from = _parse_date(od)
    date_to = _parse_date(do)
    if dana:
        date_to = date_to or date.today()
        date_from = date_to - timedelta(days=dana)

    stats = IngestService().sync(date_from, date_to)
    typer.echo(stats.summary())
    for message in stats.messages:
        typer.secho(message, fg="red")


@app.command("backfill")
def cmd_backfill(
    od: str = typer.Option(..., help="Datum od (YYYY-MM-DD), npr. početak godine."),
    do: str = typer.Option(None, help="Datum do (podrazumevano danas)."),
    bez_mejlova: bool = typer.Option(
        True, "--bez-mejlova/--sa-mejlovima",
        help="Preuzimanje istorije ne šalje obaveštenja (podrazumevano)."
    ),
) -> None:
    """Preuzima duži period, mesec po mesec.

    Jedan `sync` preko osam meseci znači jedan ogroman odgovor i, ako pukne na
    kraju, ceo posao ispočetka. Po mesecima svaki komad staje sam za sebe i
    ponovno pokretanje nastavlja gde je stalo (dokumenti se prepoznaju po SEF ID-u).
    """
    _boot()
    from calendar import monthrange

    from .config import get_settings as _get
    from .services.ingest import IngestService

    pocetak = date.fromisoformat(od)
    kraj = date.fromisoformat(do) if do else date.today()
    if pocetak > kraj:
        typer.secho("Datum 'od' je posle datuma 'do'.", fg="red")
        raise typer.Exit(1)

    settings = _get()
    if bez_mejlova:
        # istorija je vec obradjena u firmi - obavestenja za nju nemaju smisla
        settings.notify_email_enabled = False
        settings.push_enabled = False
        typer.echo("Obaveštenja su isključena za ovo preuzimanje.")

    service = IngestService(settings=settings)
    ukupno = {"seen": 0, "created": 0, "updated": 0, "errors": 0}
    mesec = pocetak.replace(day=1)
    while mesec <= kraj:
        poslednji = date(mesec.year, mesec.month, monthrange(mesec.year, mesec.month)[1])
        komad_od = max(mesec, pocetak)
        komad_do = min(poslednji, kraj)
        typer.echo(f"{komad_od} .. {komad_do}")
        stats = service.sync(komad_od, komad_do)
        typer.echo(f"   {stats.summary()}")
        for kljuc in ukupno:
            ukupno[kljuc] += getattr(stats, kljuc)
        mesec = date(mesec.year + (mesec.month // 12), (mesec.month % 12) + 1, 1)

    typer.echo("")
    typer.echo(
        f"UKUPNO: pregledano={ukupno['seen']} novih={ukupno['created']} "
        f"azurirano={ukupno['updated']} greske={ukupno['errors']}"
    )


@app.command("worker")
def cmd_worker() -> None:
    """Neprekidno sinhronizuje na svakih POLL_INTERVAL_MINUTES minuta."""
    _boot("sefsync")
    from .services.scheduler import SyncWorker

    SyncWorker().run_forever()


@app.command("serve")
def cmd_serve(
    host: str = typer.Option(None),
    port: int = typer.Option(None),
    worker: bool = typer.Option(False, "--worker", help="Pokreni i sinhronizaciju u pozadini."),
) -> None:
    """Pokreće web aplikaciju (dashboard)."""
    _boot("sefsync")
    import uvicorn

    settings = get_settings()
    if worker:
        from .services.scheduler import SyncWorker

        SyncWorker().start_background()
    uvicorn.run(
        "sefsync.web.app:app",
        host=host or settings.web_host,
        port=port or settings.web_port,
        log_level=settings.log_level.lower(),
    )


# --------------------------------------------------------------------------- #
# analiza pre pisanja pravila
# --------------------------------------------------------------------------- #


@app.command("pregled")
def cmd_overview(
    dana: int = typer.Option(30, help="Koliko dana unazad."),
    po_dobavljacu: bool = typer.Option(False, "--po-dobavljacu", help="Prikaži i presek po dobavljaču."),
) -> None:
    """Sažetak ulaznih dokumenata na SEF-u (samo listanje, ne otvara dokumente)."""
    _boot()
    from collections import Counter

    from .sef.client import SefClient

    date_to = date.today()
    date_from = date_to - timedelta(days=dana)
    with SefClient() as client:
        records = client.purchase_overview(date_from, date_to)

    typer.echo(f"Period {date_from} .. {date_to}: {len(records)} dokumenata")
    typer.echo("")
    if not records:
        return

    def counter(key: str) -> Counter:
        return Counter(str(r.get(key) or "?") for r in records)

    for label, key in (("Po statusu", "Status"), ("Po vrsti", "DocumentType")):
        typer.echo(label)
        for value, count in counter(key).most_common():
            typer.echo(f"  {count:>5}  {value}")
        typer.echo("")

    total = sum(float(r.get("Amount") or 0) for r in records)
    typer.echo(f"Ukupna vrednost: {total:,.2f}".replace(",", " "))

    if po_dobavljacu:
        typer.echo("")
        typer.echo("Po dobavljaču (top 25)")
        suppliers = Counter(
            f"{r.get('SupplierName') or '?'} (PIB {r.get('SupplierVatRegistrationNumber') or '?'})"
            for r in records
        )
        for value, count in suppliers.most_common(25):
            typer.echo(f"  {count:>5}  {value}")


@app.command("sample")
def cmd_sample(
    dana: int = typer.Option(60, help="Koliko dana unazad."),
    limit: int = typer.Option(200, help="Maksimalan broj dokumenata."),
    izlaz: str = typer.Option("data/uzorak", help="Direktorijum za UBL fajlove."),
) -> None:
    """Skida uzorak UBL-ova sa SEF-a (bez obrade) radi analize razvrstavanja."""
    _boot()
    from .sef.client import SefClient, SefError

    out_dir = Path(izlaz)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_to = date.today()
    date_from = date_to - timedelta(days=dana)

    with SefClient() as client:
        records = client.purchase_overview(date_from, date_to)
        typer.echo(f"Pronađeno {len(records)} dokumenata u periodu {date_from} .. {date_to}")
        saved = 0
        for record in records[:limit]:
            invoice_id = record.get("InvoiceId") or record.get("invoiceId")
            if invoice_id is None:
                continue
            target = out_dir / f"{invoice_id}.xml"
            if target.exists():
                continue
            try:
                target.write_bytes(client.purchase_ubl(int(invoice_id)))
                saved += 1
            except SefError as exc:
                typer.secho(f"  {invoice_id}: {exc}", fg="yellow")
    typer.echo(f"Sačuvano {saved} UBL fajlova u {out_dir}")


@app.command("analyze")
def cmd_analyze(
    direktorijum: str = typer.Option("data/uzorak", help="Direktorijum sa UBL fajlovima."),
    limit: int = typer.Option(None, help="Maksimalan broj fajlova."),
    csv_izlaz: str = typer.Option(None, "--csv", help="Snimi tabelu polja u CSV."),
) -> None:
    """Analizira preuzete UBL-ove: koja polja dobavljači popunjavaju (osnova za pravila)."""
    _boot()
    from .routing.analyzer import analyze_dir

    directory = Path(direktorijum)
    if not directory.exists():
        typer.secho(f"Direktorijum {directory} ne postoji. Pokreni prvo: sefsync sample", fg="red")
        raise typer.Exit(1)

    report = analyze_dir(directory, limit)
    typer.echo(report.to_text())
    if csv_izlaz:
        report.to_csv(Path(csv_izlaz))
        typer.echo(f"\nCSV: {csv_izlaz}")


# --------------------------------------------------------------------------- #
# šifarnici
# --------------------------------------------------------------------------- #


@app.command("predlozi")
def cmd_suggest(
    direktorijum: str = typer.Option("data/uzorak", help="Direktorijum sa UBL fajlovima."),
    primeni: bool = typer.Option(False, "--primeni", help="Upiši predloge kao pravila."),
) -> None:
    """Predlaže pravila razvrstavanja uparivanjem adresa isporuke sa šifarnikom PJ."""
    _boot()
    from .routing.analyzer import load_ubl_dir
    from .routing.suggest import apply_suggestions, suggest_rules

    directory = Path(direktorijum)
    if not directory.exists():
        typer.secho(f"Direktorijum {directory} ne postoji. Pokreni prvo: sefsync sample", fg="red")
        raise typer.Exit(1)

    parsed, errors = load_ubl_dir(directory)
    for err in errors[:5]:
        typer.secho(f"  {err}", fg="yellow")

    with session_scope() as session:
        try:
            report = suggest_rules(session, [doc for _, doc in parsed])
        except ValueError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1) from exc
        typer.echo(report.to_text())
        if primeni:
            created = apply_suggestions(session, report)
            typer.echo("")
            typer.echo(f"Upisano novih pravila: {created}")
        else:
            typer.echo("")
            typer.echo("Ništa nije upisano. Za upis pokreni: sefsync predlozi --primeni")


@app.command("proba")
def cmd_dryrun(
    direktorijum: str = typer.Option("data/uzorak", help="Direktorijum sa UBL fajlovima."),
    prikazi_nerazvrstane: bool = typer.Option(False, "--nerazvrstani", help="Ispiši i one koji padnu."),
) -> None:
    """Proba razvrstavanja nad uzorkom: koliko dokumenata trenutna pravila pokrivaju."""
    _boot()
    from collections import Counter

    from sqlalchemy import select

    from .models import BusinessUnit
    from .routing.analyzer import load_ubl_dir
    from .routing.engine import RoutingEngine

    parsed, _ = load_ubl_dir(Path(direktorijum))
    if not parsed:
        typer.secho("Nema UBL fajlova u uzorku.", fg="red")
        raise typer.Exit(1)

    per_unit: Counter = Counter()
    per_supplier: Counter = Counter()
    unassigned: list[str] = []

    with session_scope() as session:
        engine = RoutingEngine.from_db(session)
        codes = {u.id: u.code for u in session.scalars(select(BusinessUnit))}
        for name, doc in parsed:
            decision = engine.decide(doc.routing_fields())
            if decision.assigned:
                per_unit[codes.get(decision.business_unit_id, "?")] += 1
            else:
                per_supplier[
                    f"{doc.supplier.name or '?'} (PIB {doc.supplier.vat or '?'})"
                ] += 1
                unassigned.append(f"{name}  {doc.document_number or ''}  {doc.supplier.name or ''}")

    total = len(parsed)
    covered = sum(per_unit.values())
    typer.echo(f"Uzorak: {total} dokumenata")
    typer.echo(f"Razvrstano automatski: {covered} ({100.0 * covered / total:.0f}%)")
    typer.echo(f"Nerazvrstano: {total - covered}")
    typer.echo("")
    typer.echo("Po poslovnoj jedinici")
    for code, count in per_unit.most_common():
        typer.echo(f"  {count:>4}  {code}")
    if per_supplier:
        typer.echo("")
        typer.echo("Nerazvrstano po dobavljaču (kandidati za pravilo supplier_vat -> PJ)")
        for supplier, count in per_supplier.most_common(20):
            typer.echo(f"  {count:>4}  {supplier}")
    if prikazi_nerazvrstane:
        typer.echo("")
        for line in unassigned:
            typer.echo(f"  {line}")


@app.command("ko-moze")
def cmd_who(
    svi: bool = typer.Option(False, "--svi", help="Prikaži i one koji nemaju prijavu."),
) -> None:
    """Ko može u panel i sa kojom ulogom — po šifarniku osoblja i spisku operatera."""
    _boot()
    from sqlalchemy import create_engine, text

    from .erp.connection import resolve_erp_url
    from .web.auth import AuthError, load_person

    settings = get_settings()
    operateri = settings.operator_logins()
    typer.echo(f"Spisak operatera (WEB_OPERATORS): {', '.join(sorted(operateri)) or '(prazno)'}")
    typer.echo("")

    url = resolve_erp_url()
    if not url:
        typer.secho("Veza ka ERP bazi nije podešena.", fg="red")
        raise typer.Exit(1)

    engine = create_engine(url, pool_pre_ping=True)
    uslov = "" if svi else "WHERE [User] IS NOT NULL AND RTRIM([User]) <> ''"
    try:
        with engine.connect() as conn:
            redovi = list(
                conn.execute(
                    text(
                        "SELECT RTRIM(ImePrezime) AS ime, RTRIM(Funkcija) AS funkcija, "
                        "RTRIM(ISNULL([User], '')) AS prijava "
                        f"FROM dbo.kontakti_osobe {uslov} ORDER BY ImePrezime"
                    )
                ).mappings()
            )
    finally:
        engine.dispose()

    for red in redovi:
        prijava = red["prijava"]
        if not prijava:
            uloga = "nema prijavu"
        else:
            try:
                uloga = load_person(prijava, settings).role.value
            except AuthError as exc:
                uloga = f"bez pristupa ({str(exc)[:34]}…)"
        boja = "green" if uloga == "operater" else None
        typer.secho(
            f"  {prijava or '—':<16} {red['ime'][:26]:<26} {red['funkcija'][:20]:<20} {uloga}",
            fg=boja,
        )


@app.command("prebaci-bazu")
def cmd_migrate_db(
    u: str = typer.Option(..., help="Veza ka novoj bazi (SQLAlchemy URL)."),
    iz: str = typer.Option(None, help="Veza ka staroj (podrazumevano: trenutna iz .env)."),
    prepisi: bool = typer.Option(False, "--prepisi", help="Obriši i upiši ponovo ako nešto već ima."),
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo prebroji šta bi prešlo."),
) -> None:
    """Prebacuje bazu panela na drugi server (npr. SQLite -> MS SQL).

    Dokumenti bi se mogli i ponovo preuzeti, ali rad operatera ne bi -
    razvrstavanje, prosleđeno, potvrde prijema i istorija radnji postoje samo ovde.
    """
    _boot()
    from sqlalchemy import create_engine, func as _func, inspect as _inspect, select as _select

    from .db import get_engine
    from .models import Base
    from .services.seoba import prebaci_na

    if not stvarno:
        izvor = create_engine(iz) if iz else get_engine()
        inspektor = _inspect(izvor)
        typer.echo(f"Izvor: {izvor.url.render_as_string(hide_password=True)}")
        typer.echo(f"Cilj : {create_engine(u).url.render_as_string(hide_password=True)}")
        typer.echo("")
        with izvor.connect() as veza:
            for tabela in Base.metadata.sorted_tables:
                if not inspektor.has_table(tabela.name):
                    continue
                broj = veza.execute(_select(_func.count()).select_from(tabela)).scalar_one()
                if broj:
                    typer.echo(f"  {tabela.name:<18} {broj:>7}")
        typer.echo("")
        typer.echo("Ovo je bio suvi prolaz. Za stvarno prebacivanje dodaj --stvarno")
        return

    izvestaj = prebaci_na(u, iz, prepisi=prepisi)
    typer.echo(izvestaj.tekst())
    typer.echo("")
    typer.echo("Sada upiši novu vezu u .env kao DB_URL i restartuj servis.")


@app.command("pravila-izvoz")
def cmd_rules_export(
    fajl: str = typer.Option("pravila.json", help="Gde da upiše izvoz."),
) -> None:
    """Izvozi šifarnik, pravila i mapiranja artikala u JSON (za prenos na server)."""
    _boot()
    from .services.transfer import upisi_u_fajl

    podaci = upisi_u_fajl(Path(fajl))
    typer.echo(
        f"{fajl}: {len(podaci['poslovne_jedinice'])} poslovnih jedinica, "
        f"{len(podaci['pravila'])} pravila, "
        f"{len(podaci['mapiranja_artikala'])} mapiranja artikala"
    )


@app.command("pravila-uvoz")
def cmd_rules_import(
    fajl: str = typer.Argument(..., help="JSON napravljen sa pravila-izvoz."),
    i_jedinice: bool = typer.Option(
        False, "--i-jedinice", help="Uvezi i šifarnik PJ (inače se očekuje da je već tu)."
    ),
) -> None:
    """Uvozi pravila razvrstavanja sa druge instalacije. Ponovni uvoz ne duplira."""
    _boot()
    from .services.transfer import procitaj_iz_fajla

    brojac = procitaj_iz_fajla(Path(fajl), i_jedinice=i_jedinice)
    for kljuc, vrednost in brojac.items():
        if vrednost:
            typer.echo(f"  {kljuc.replace('_', ' ')}: {vrednost}")
    typer.echo("Gotovo. Za primenu na postojeće dokumente pokreni: sefsync razvrstaj")


@app.command("razvrstaj")
def cmd_reroute(
    sve: bool = typer.Option(
        False, "--sve", help="Ponovo razvrstaj i one koji već imaju PJ (ne dira ručne)."
    ),
) -> None:
    """Primenjuje pravila na dokumente koji čekaju razvrstavanje."""
    _boot()
    from sqlalchemy import select as _select

    from .models import Document, RoutingSource
    from .services.ingest import IngestService

    with session_scope() as session:
        upit = _select(Document.id).where(Document.archived == False)
        if not sve:
            upit = upit.where(Document.business_unit_id.is_(None))
        else:
            # rucne odluke operatera se ne preispituju
            upit = upit.where(Document.routing_source != RoutingSource.MANUAL)
        ids = list(session.scalars(upit))

    typer.echo(f"Razvrstavam {len(ids)} dokumenata...")
    servis = IngestService()
    razvrstano = 0
    for doc_id in ids:
        try:
            if servis.reroute(doc_id) is not RoutingSource.NONE:
                razvrstano += 1
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"  #{doc_id}: {exc}", fg="yellow")
    typer.echo(f"Razvrstano: {razvrstano} od {len(ids)}")


@app.command("pj-import")
def cmd_units_import(
    fajl: str = typer.Argument(..., help="CSV: code;name;kind;erp_code;address;city;emails;phones"),
) -> None:
    """Uvozi poslovne jedinice iz CSV fajla."""
    _boot()
    from .services.units import import_csv

    result = import_csv(Path(fajl))
    for warning in result.warnings:
        typer.secho(f"  {warning}", fg="yellow")
    typer.echo(f"Poslovne jedinice: {result.summary()}")


@app.command("pj-sync")
def cmd_units_sync(
    deaktiviraj: bool = typer.Option(
        False, "--deaktiviraj-nestale",
        help="Jedinice kojih više nema u izvoru se gase (ne brišu)."
    ),
) -> None:
    """Sinhronizuje poslovne jedinice iz postojeće tabele na serveru (BU_SOURCE_SQL)."""
    _boot()
    from .services.units import sync_from_source

    try:
        result = sync_from_source(deactivate_missing=deaktiviraj)
    except ValueError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    for warning in result.warnings:
        typer.secho(f"  {warning}", fg="yellow")
    typer.echo(f"Poslovne jedinice: {result.summary()}")


@app.command("arhiviraj")
def cmd_archive(
    do: str = typer.Option(..., help="Arhiviraj sve poslato zaključno sa ovim datumom."),
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo prikazuje koliko bi ih bilo."),
) -> None:
    """Sklanja istoriju iz reda operatera (ostaje pretraživa u arhivi)."""
    _boot()
    from datetime import datetime

    from sqlalchemy import select

    from .models import Document
    from .services.workflow import Workflow

    granica = datetime.fromisoformat(do) + timedelta(days=1)
    with session_scope() as session:
        upit = select(Document.id).where(
            Document.archived == False, Document.sent_date < granica
        )
        ids = list(session.scalars(upit))
    if not stvarno:
        typer.echo(f"Arhiviralo bi se {len(ids)} dokumenata (poslatih zaključno sa {do}).")
        typer.echo("Za stvarno arhiviranje dodaj --stvarno")
        return

    rezultat = Workflow().archive(ids, actor="cli")
    typer.echo(rezultat.message)


@app.command("pravila")
def cmd_rules() -> None:
    """Ispisuje aktivna pravila razvrstavanja."""
    _boot()
    from sqlalchemy import select

    from .models import RoutingRule

    with session_scope() as session:
        rules = list(session.scalars(select(RoutingRule).order_by(RoutingRule.priority, RoutingRule.id)))
        if not rules:
            typer.echo("Nema definisanih pravila.")
            return
        for rule in rules:
            flag = "aktivno " if rule.active else "ISKLJUČENO"
            typer.echo(
                f"[{flag}] #{rule.priority:>4} {rule.field.value:<20} {rule.op.value:<10} "
                f"{rule.pattern[:40]:<40} -> {rule.business_unit.code}  ({rule.hits} pogodaka)"
            )


# --------------------------------------------------------------------------- #
# notifikacije i ERP
# --------------------------------------------------------------------------- #


@app.command("smtp-check")
def cmd_smtp_check(
    na: str = typer.Option(None, "--na", help="Pošalji probnu poruku na ovu adresu."),
) -> None:
    """Prikazuje razrešena SMTP podešavanja i, sa --na, šalje probnu poruku."""
    _boot()
    from .notify.base import Message
    from .notify.email_channel import EmailChannel
    from .notify.smtp_config import resolve_smtp

    smtp = resolve_smtp()
    typer.echo(smtp.describe())
    nedostaje = smtp.missing()
    if nedostaje:
        typer.secho("Nedostaje: " + ", ".join(nedostaje), fg="red")
        raise typer.Exit(1)
    if not na:
        typer.echo("Podešavanja su kompletna. Za probno slanje dodaj --na adresa@primer.rs")
        return

    poruka = Message(
        subject="ELBRACO SEF — probna poruka",
        body_text="Ovo je probna poruka iz aplikacije za ulazne e-fakture sa SEF-a.",
        body_html="<p>Ovo je <b>probna poruka</b> iz aplikacije za ulazne e-fakture sa SEF-a.</p>",
    )
    for ishod in EmailChannel().send([na], poruka):
        if ishod.ok:
            typer.secho(f"Poslato na {ishod.target}", fg="green")
        else:
            typer.secho(f"Nije poslato na {ishod.target}: {ishod.error}", fg="red")


@app.command("notify-test")
def cmd_notify_test(
    dokument_id: int = typer.Argument(..., help="ID dokumenta u bazi aplikacije."),
) -> None:
    """Šalje obaveštenje za jedan dokument poslovnoj jedinici kojoj pripada."""
    _boot()
    from .notify.dispatcher import Dispatcher

    ok = Dispatcher().notify_document(dokument_id)
    typer.secho("Poslato." if ok else "Nijedan kanal nije uspeo.", fg="green" if ok else "red")


@app.command("erp-check")
def cmd_erp_check() -> None:
    """Provera konekcije ka ERP bazi (faza 2)."""
    _boot()
    from .erp.connection import ErpNotConfigured, check_connection

    try:
        typer.echo(check_connection())
    except ErpNotConfigured as exc:
        typer.secho(str(exc), fg="yellow")
        raise typer.Exit(1) from exc


@app.command("erp-tables")
def cmd_erp_tables(sadrzi: str = typer.Option(None, help="Filtriraj po delu naziva.")) -> None:
    """Ispisuje tabele u ERP bazi (pomoć pri mapiranju kalkulacije)."""
    _boot()
    from .erp.connection import list_tables

    for name in list_tables(like=sadrzi):
        typer.echo(name)


@app.command("kalkulacija")
def cmd_calculation(
    dokument_id: int = typer.Argument(...),
    csv_izlaz: str = typer.Option("data/kalkulacije", "--csv-dir"),
) -> None:
    """FAZA 2 (nacrt): pravi ulaznu kalkulaciju iz dokumenta i snima je kao CSV."""
    _boot()
    from .erp.calculation import CsvWriter, build_draft

    with session_scope() as session:
        draft = build_draft(session, dokument_id)
    for warning in draft.warnings:
        typer.secho(f"UPOZORENJE: {warning}", fg="yellow")
    path = CsvWriter(Path(csv_izlaz)).write(draft)
    typer.echo(f"Kalkulacija: {path}")
    typer.echo(f"Stavki: {len(draft.lines)}, nemapiranih: {len(draft.unmapped_lines)}")


if __name__ == "__main__":  # pragma: no cover
    app()
