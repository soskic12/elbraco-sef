"""Komandna linija: sefsync <komanda>."""

from __future__ import annotations

import logging
import time
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
    from .config import dom_nije_instalacija

    problem = dom_nije_instalacija()
    if problem:
        # Bolje stati nego uspeti nad pogresnom bazom - to se ne vidi dok neko
        # ne primeti da se posao nije desio.
        typer.secho(problem, fg="red")
        raise typer.Exit(2)
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
        preskoceno_novih = 0
        for record in records[:limit]:
            invoice_id = record.get("InvoiceId") or record.get("invoiceId")
            if invoice_id is None:
                continue
            # Skidanje UBL-a obara "Nova" u "Vidjena" na portalu. Uzorak se uzima
            # radi analize i ne sme da pomeri kolegama spisak novih dokumenata.
            if (
                get_settings().sef_preserve_new
                and str(record.get("Status") or record.get("status") or "") == "New"
            ):
                preskoceno_novih += 1
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
    if preskoceno_novih:
        typer.echo(
            f"Preskočeno {preskoceno_novih} dokumenata u statusu „Nova“ — "
            f"preuzimanje bi im oborilo status na portalu (SEF_PRESERVE_NEW)."
        )


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
    odobrenja: bool = typer.Option(
        False, "--odobrenja", help="Samo knjižna odobrenja."
    ),
) -> None:
    """Primenjuje pravila na dokumente koji čekaju razvrstavanje."""
    _boot()
    from sqlalchemy import select as _select

    from .models import Document, DocumentType, RoutingSource
    from .services.ingest import IngestService

    with session_scope() as session:
        upit = _select(Document.id).where(Document.archived == False)
        if odobrenja:
            # Odobrenja su jedina vrsta kod koje pogresna PJ znaci da roba ostane
            # na stanju: umesto povrata na 010 zavrsi kao usluga na 088. Mereno
            # 24.09.: zapisane odluke se sa stvarnim magacinom poklapaju u 36 %,
            # a danasnji motor u 93 % - razlika su stare odluke, ne pravila.
            upit = upit.where(Document.document_type == DocumentType.CREDIT_NOTE)
        if not sve:
            upit = upit.where(Document.business_unit_id.is_(None))
        else:
            # rucne odluke operatera se ne preispituju
            upit = upit.where(Document.routing_source != RoutingSource.MANUAL)
        ids = list(session.scalars(upit))

    typer.echo(f"Razvrstavam {len(ids)} dokumenata...")
    servis = IngestService()
    razvrstano = 0
    ostali: list[int] = []
    for doc_id in ids:
        try:
            if servis.reroute(doc_id) is not RoutingSource.NONE:
                razvrstano += 1
            else:
                ostali.append(doc_id)
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"  #{doc_id}: {exc}", fg="yellow")
    typer.echo(f"Razvrstano: {razvrstano} od {len(ids)}")

    # Ko su ostali, a ne samo koliko ih je. Bez ovoga je "Razvrstano: 0 od 8"
    # jedino sto se vidi sa konzole, pa se ne zna da li je to osam obicnih
    # dokumenata za operatera ili novi dobavljac kome fali pravilo.
    if ostali:
        typer.echo("")
        typer.echo("Cekaju operatera (nijedno pravilo ih ne hvata):")
        with session_scope() as session:
            docs = session.scalars(
                _select(Document).where(Document.id.in_(ostali)).order_by(Document.issue_date)
            )
            for d in docs:
                datum = d.issue_date.isoformat() if d.issue_date else "?"
                typer.echo(
                    f"  {datum}  {d.document_number or '?':<20} "
                    f"{(d.supplier_name or '?')[:34]:<34} PIB {d.supplier_vat or '?'}"
                )


@app.command("mapiranje")
def cmd_mapiranje(
    upisi: bool = typer.Option(
        False, "--upisi", help="Stvarno upiši mapiranja (podrazumevano samo ispis)."
    ),
    i_nesigurna: bool = typer.Option(
        False, "--i-nesigurna", help="Upiši i ono što je viđeno samo jednom."
    ),
) -> None:
    """Izvodi mapiranja artikala (dobavljačeva šifra → naša) iz istorije.

    Dva nezavisna izvora: barkod iz šifarnika i ono što je čovek zaknjižio u
    kalkulacijama. Podrazumevano samo ispisuje šta bi uradila.
    """
    _boot()
    from .services.mapiranje import izvedi
    from .services.mapiranje import upisi as upisi_u_bazu

    nalaz = izvedi()
    typer.echo(f"Parova (dobavljač + šifra): {nalaz.ukupno_parova}")
    typer.echo(f"  iz barkoda:            {nalaz.iz_barkoda}")
    typer.echo(f"  iz kalkulacija:        {nalaz.iz_kalkulacija}")
    typer.echo(f"  oba izvora se slažu:   {nalaz.oba_se_slazu}")
    typer.echo(f"  oba se NE slažu:       {nalaz.oba_se_ne_slazu}")
    typer.echo(f"  kolebljivih (odbačeno): {nalaz.kolebljivih}")
    typer.echo(f"  pouzdanih za upis:     {nalaz.pouzdanih}")
    if nalaz.dokumenata_po_pocetku:
        typer.echo(
            f"  (od toga {nalaz.dokumenata_po_pocetku} dokumenata spareno po početku "
            f"broja — u ERP-u je skraćen)"
        )
    if not upisi:
        typer.echo("")
        typer.echo("Nije upisano ništa. Za upis: sefsync mapiranje --upisi")
        return

    nalaz = upisi_u_bazu(nalaz, samo_pouzdana=not i_nesigurna)
    typer.echo(f"Upisano: {nalaz.upisano}, već postojalo: {nalaz.vec_postojalo}")


@app.command("sifre-slobodne")
def cmd_slobodne_sifre(
    koliko: int = typer.Option(12, help="Koliko rupa da ispiše."),
    najmanja: int = typer.Option(
        None, help="Prag veličine rupe (podrazumevano onaj koji koristi i predlog)."
    ),
) -> None:
    """Gde u šifarniku ima slobodnih šifara — po opsezima."""
    _boot()
    from .erp.artikal import GORNJA_GRANICA, MIN_RUPA, rupe, slobodna_sifra
    from .erp.connection import get_erp_engine

    prag = najmanja if najmanja is not None else MIN_RUPA
    with get_erp_engine().connect() as conn:
        nadjene = rupe(conn, najmanja=prag)
        slobodno = sum(do - od + 1 for od, do in nadjene)
        typer.echo(
            f"Rupa od bar {prag} mesta: {len(nadjene)}, "
            f"ukupno {slobodno} slobodnih šifara ispod {GORNJA_GRANICA}."
        )
        for od, do in nadjene[:koliko]:
            typer.echo(f"  {od:>7} - {do:<7} ({do - od + 1} slobodnih)")
        typer.echo("")
        # Isti prag mora da vazi i ovde - inace ispis mape i predlog govore razlicito.
        sifra = slobodna_sifra(conn, najmanja_rupa=prag)
        typer.echo(f"Prva slobodna koja bi se predložila: {sifra or '—'}")


@app.command("nacrti")
def cmd_nacrti(
    osvezi: bool = typer.Option(
        False, "--osvezi", help="Preračunaj nacrte (inače samo ispiše zatečeno stanje)."
    ),
    limit: int = typer.Option(None, help="Samo toliko najnovijih dokumenata."),
    magacin: str = typer.Option(None, help="Ispiši i spisak za taj magacin (npr. 010)."),
    spisak: bool = typer.Option(False, "--spisak", help="Ispiši spisak i bez magacina."),
    stanje: str = typer.Option("spreman", help="Koje stanje ispisati uz --magacin ili --spisak."),
) -> None:
    """Stanje kalkulacija koje aplikacija sklapa: šta je spremno, a šta čeka čoveka."""
    _boot()
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select

    from .models import Document, NacrtKalkulacije, StanjeNacrta

    if osvezi:
        from .services.nacrti import osvezi_sve

        typer.echo("Računam nacrte (za svaki dokument se pita ERP)...")
        poceto = time.monotonic()
        brojac = osvezi_sve(limit=limit)
        typer.echo(f"Trajalo: {time.monotonic() - poceto:.0f} s")
    else:
        with session_scope() as session:
            brojac = {
                s.value: session.scalar(
                    _select(_func.count()).select_from(NacrtKalkulacije)
                    .where(NacrtKalkulacije.stanje == s,
                           NacrtKalkulacije.document.has(
                               Document.archived == False))  # noqa: E712
                )
                for s in StanjeNacrta
            }

    for kljuc, vrednost in brojac.items():
        typer.echo(f"  {kljuc.replace('_', ' '):<12} {vrednost}")

    if magacin or spisak:
        from sqlalchemy.orm import selectinload as _selectinload

        from .models import Document

        with session_scope() as session:
            upit = (
                _select(NacrtKalkulacije)
                .options(_selectinload(NacrtKalkulacije.document))
                # Arhivirani dokument niko ne radi, a osvezavanje ga ne dira -
                # pa bi zauvek stajao na spisku "zaustavljeno". Coffee Center
                # 26-FA001010834 nas je tako dvaput poslao u proveru necega na
                # sta se ne moze uticati (07.10.).
                .where(NacrtKalkulacije.stanje == StanjeNacrta(stanje),
                       NacrtKalkulacije.document.has(
                           Document.archived == False))  # noqa: E712
            )
            if magacin:
                upit = upit.where(NacrtKalkulacije.magacin == magacin)
            redovi = list(
                session.scalars(
                    upit
                    .join(Document, Document.id == NacrtKalkulacije.document_id)
                    .order_by(Document.issue_date.desc())
                )
            )
            typer.echo("")
            gde = f"Magacin {magacin}, " if magacin else "Svi magacini, "
            typer.echo(f"{gde}stanje „{stanje}“: {len(redovi)}")
            for r in redovi[:25]:
                d = r.document
                typer.echo(
                    f"   #{r.document_id:<6} {d.issue_date}  {(d.document_number or '?'):<22}"
                    f" {(d.supplier_name or '')[:28]:<28} {r.iznos or 0:>12,.2f}"
                )
                # Kod zaustavljenih kalkulacija sam broj ne govori nista -
                # razlog je jedino sto covek moze da resi.
                for problem in (r.problemi or [])[:3]:
                    typer.echo(f"          {problem}")

    if not osvezi:
        typer.echo("")
        typer.echo("Za ponovno računanje: sefsync nacrti --osvezi")


@app.command("nemapirane")
def cmd_nemapirane() -> None:
    """Zašto stavke čekaju šifru — po uzroku, ne po utisku."""
    _boot()
    from .services.nacrti import razlozi_cekanja

    nalaz = razlozi_cekanja()
    typer.echo(
        f"Nacrta koji čekaju šifru: {nalaz['nacrta']}, stavki u njima: {nalaz['stavki']}"
    )
    typer.echo("")
    for uzrok, n in nalaz["uzroci"].most_common():
        typer.echo(f"  {n:>4}  {uzrok}")
        for primer in nalaz["primeri"].get(uzrok, []):
            typer.echo(f"        {primer}")
    if nalaz["po_dobavljacu"]:
        typer.echo("")
        typer.echo("Po dobavljaču:")
        for ime, n in nalaz["po_dobavljacu"].most_common(10):
            typer.echo(f"  {n:>4}  {ime}")


@app.command("zaboravi-upis")
def cmd_zaboravi_upis(
    document_id: int = typer.Argument(..., help="ID dokumenta u panelu."),
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispiše."),
) -> None:
    """Skida oznaku „upisano u ERP" sa nacrta, da se upis može ponoviti.

    Nacrt koji je jednom otisao u ERP se namerno ne vraca unazad - inace bi
    ponovno racunanje obrisalo trag o tome sta je vec zaknjizeno. Kod probnih
    upisa u ELBSX_2026 je to smetnja: zapis se obrise iz ERP-a, a panel i dalje
    misli da postoji, pa dugmeta nema.

    NE dira ERP. Zapis u ELBSX/ELBS se brise zasebno, pre ovoga.
    """
    _boot()

    from .models import NacrtKalkulacije, StanjeNacrta

    with session_scope() as session:
        red = session.get(NacrtKalkulacije, document_id)
        if red is None:
            typer.secho(f"Dokument #{document_id} nema nacrt.", fg="red")
            raise typer.Exit(1)
        if red.stanje is not StanjeNacrta.UPISAN:
            typer.echo(f"#{document_id}: stanje je „{red.stanje.value}“ — nema šta da se skine.")
            raise typer.Exit(0)

        typer.echo(
            f"#{document_id}  {red.magacin or '?'}  {red.erp_broj or '?'}  "
            f"upisao {red.upisao or '?'} {red.upisano_u or ''}"
        )
        if not stvarno:
            typer.echo("")
            typer.secho("Suvi prolaz. Za stvarno dodaj --stvarno.", fg="yellow")
            typer.echo("Pre toga obriši zapis iz ERP-a, inače ga zaustavi provera duplikata.")
            raise typer.Exit(0)

        red.stanje = StanjeNacrta.SPREMAN
        red.erp_broj = None
        red.erp_napomena = None
        red.upisano_u = None
        red.upisao = None
        # Ako je oznaka dosla iz potvrdjenog predloga, i on se brise - inace bi
        # dokument ostao sa odlukom koja vise ne vazi.
        red.sumnja = None
        red.sumnja_odbio = None
        red.sumnja_odbijeno_u = None

    typer.echo(f"#{document_id}: oznaka skinuta, upis se može ponoviti.")


@app.command("dopuni-cenovnike")
def cmd_dopuni_cenovnike(
    sifra: str = typer.Argument(..., help="Šifra artikla iz jedinstvenog šifarnika."),
    cena: float = typer.Option(None, "--cena", help="Maloprodajna cena za sva mesta."),
    samo: str = typer.Option(None, "--samo", help="Samo ta mesta, npr. 002,003,009."),
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispisuje."),
) -> None:
    """Dodaje postojećoj šifri redove po magacinima i prodajnim mestima.

    Šifra otvorena ručno završi samo u jedinstvenom šifarniku (099), pa
    kalkulacija posle staje sa „artikal nema cenu u objektu“. Tako je devet
    EWE faktura stalo na artiklu 87622 (07.10.).

    Ne dira ono što već postoji i ne menja nijednu postojeću cenu.
    """
    _boot()
    from .erp.artikal import bez_cenovnika, dopuni_cenovnike
    from .erp.connection import get_erp_engine

    with get_erp_engine().connect() as conn:
        art, magacini, mesta = bez_cenovnika(conn, sifra.strip())

    if art is None:
        typer.secho(f"Šifre {sifra} nema u jedinstvenom šifarniku (099).", fg="red")
        raise typer.Exit(1)

    typer.echo(f"{art['artikal']}  {art['naziv']}  barkod {art['barkod'] or '—'}  "
               f"kategorija {art['shkat'] or '—'}")
    typer.echo(f"   fali u magacinima:      {', '.join(magacini) or '—'}")
    typer.echo(f"   fali u prodajnim mestima: {', '.join(mesta) or '—'}")
    typer.echo("   (spisak je isti kao kod otvaranja nove šifre — ako artikal "
               "ne ide svuda, suzi ga sa --samo)")

    if not magacini and not mesta:
        typer.secho("Nema šta da se dopuni.", fg="green")
        raise typer.Exit(0)

    if not stvarno:
        typer.echo("")
        typer.secho("Suvi prolaz. Za stvarno dodaj --cena <iznos> --stvarno.", fg="yellow")
        raise typer.Exit(0)

    podesavanja = get_settings()
    try:
        opis = dopuni_cenovnike(
            sifra.strip(), cena,
            samo=[x for x in (samo or "").split(",") if x.strip()] or None,
            produkcija=podesavanja.erp_allow_production_write,
        )
    except ValueError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    typer.secho(opis, fg="green")


@app.command("izvestaj")
def cmd_izvestaj(
    dana: int = typer.Option(7, "--dana", help="Za koliko dana unazad."),
) -> None:
    """Šta je aplikacija i ko od ljudi uradio u poslednjih N dana.

    Posle isporuke se do sada proveravalo po osećaju — otvori ekran, pogledaj
    jednu fakturu, pa zaključi. Ovde se vidi odjednom: koliko je kalkulacija
    otišlo u ERP, ko je povezivao šifre, šta je otvoreno u šifarniku, i gde je
    nešto palo.
    """
    _boot()
    from collections import Counter
    from datetime import timedelta

    from sqlalchemy import func as _func
    from sqlalchemy import select as _select

    from .models import (
        AuditLog, Document, NacrtKalkulacije, ObavestenjeOPrethodnomPorezu,
        StanjeNacrta, utcnow,
    )

    granica = utcnow() - timedelta(days=dana)
    with session_scope() as session:
        zapisi = list(session.scalars(
            _select(AuditLog).where(AuditLog.at >= granica).order_by(AuditLog.id)
        ))
        po_akciji = Counter(z.action for z in zapisi)
        ko = Counter(z.actor for z in zapisi if z.actor and z.actor != "system")

        # Isti kriterijum kao u `nacrti`: arhivu niko ne radi, pa se ne broji.
        # Bez toga su dva ispisa davala razlicit broj zaustavljenih (10 i 9).
        stanja = {
            s.value: session.scalar(
                _select(_func.count()).select_from(NacrtKalkulacije)
                .where(NacrtKalkulacije.stanje == s,
                       NacrtKalkulacije.document.has(
                           Document.archived == False))  # noqa: E712
            )
            for s in StanjeNacrta
        }
        novih = session.scalar(
            _select(_func.count()).select_from(Document)
            .where(Document.created_at >= granica)
        )
        devizne = list(session.execute(
            _select(Document.currency, _func.count())
            .where(Document.created_at >= granica, Document.currency.isnot(None))
            .group_by(Document.currency)
        ))
        # `uspelo` je svojstvo, ne kolona - uspeh se vidi po tome ima li greske.
        obavestenja = [
            (o.uspelo, o.rucno_u is not None, o.greska)
            for o in session.scalars(
                _select(ObavestenjeOPrethodnomPorezu)
                .where(ObavestenjeOPrethodnomPorezu.poslato_u >= granica)
            )
        ]
        pali = [
            (n.document_id, (n.upozorenje or "")[:70])
            for n in session.scalars(
                _select(NacrtKalkulacije).where(NacrtKalkulacije.upozorenje.isnot(None))
            )
        ]

    typer.echo(f"Poslednjih {dana} dana — pristiglo {novih} dokumenata")
    typer.echo("   po valuti: " + (", ".join(f"{v or '?'} {n}" for v, n in devizne) or "—"))

    typer.echo("")
    typer.echo("Šta se dešavalo")
    for akcija, n in po_akciji.most_common():
        typer.echo(f"   {akcija:<30} {n}")
    if not po_akciji:
        typer.secho("   ništa — ili se ne radi, ili komanda gleda pogrešnu bazu", fg="yellow")

    typer.echo("")
    typer.echo("Ko je radio")
    for covek, n in ko.most_common(10):
        typer.echo(f"   {covek:<16} {n}")

    typer.echo("")
    typer.echo("Nacrti, trenutno stanje")
    for kljuc, n in stanja.items():
        typer.echo(f"   {kljuc.replace('_', ' '):<16} {n}")

    if obavestenja:
        typer.echo("")
        typer.echo("Obaveštenja o prethodnom porezu")
        typer.echo(f"   {'poslato':<16} {sum(1 for u, _, _ in obavestenja if u)}")
        rucno = sum(1 for u, r, _ in obavestenja if not u and r)
        if rucno:
            typer.echo(f"   {'ručno na portalu':<16} {rucno}")
        pala = [g for u, r, g in obavestenja if not u and not r]
        if pala:
            typer.secho(f"   {'NIJE POSLATO':<16} {len(pala)}", fg="yellow")
            for greska in pala[:5]:
                typer.echo(f"      {(greska or '')[:90]}")

    if pali:
        typer.echo("")
        typer.secho(f"Upozorenja koja stoje na dokumentima: {len(pali)}", fg="yellow")
        for doc_id, tekst in pali[:10]:
            typer.echo(f"   #{doc_id}  {tekst}")


@app.command("arhiviraj-ubl")
def cmd_arhiviraj_ubl(
    dana: int = typer.Option(None, "--dana", help="Koliko dana ostaje na disku."),
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispisuje."),
) -> None:
    """Pakuje stare UBL fajlove u zip po mesecima i sklanja ih sa diska.

    Ne dira poslednjih 60 dana, ni dokumente koji nisu završeni. Kad aplikaciji
    zatreba stari UBL, skida ga ponovo sa SEF-a — provereno, daje i januarske.

    Zip ostaje na serveru dok ga čovek ne prebaci; aplikacija ne briše jedini
    primerak.
    """
    _boot()
    from .services.arhiva import arhiviraj

    ishod = arhiviraj(dana, stvarno=stvarno)
    if not ishod["meseci"]:
        typer.echo("Nema šta da se arhivira.")
        raise typer.Exit(0)

    typer.echo(f"{'mesec':<10}{'fajlova':>9}{'MB':>9}")
    for mesec, red in ishod["meseci"].items():
        typer.echo(f"{mesec:<10}{red['fajlova']:>9}{red['bajtova']/1_000_000:>9.0f}"
                   + (f"   {red['greska']}" if red.get("greska") else ""))
    typer.echo(f"{'UKUPNO':<10}{ishod['fajlova']:>9}{ishod['bajtova']/1_000_000:>9.0f}")

    if not stvarno:
        typer.echo("")
        typer.secho("Suvi prolaz. Za stvarno dodaj --stvarno.", fg="yellow")
        raise typer.Exit(0)

    typer.echo("")
    typer.secho(f"Sklonjeno sa diska: {ishod['obrisano']} fajlova. "
                f"Zipovi su u data\arhiva — prebaci ih pa obriši.", fg="green")


@app.command("arhiviraj-stare")
def cmd_arhiviraj_stare(
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispisuje."),
    magacin: str = typer.Option("", "--magacin", help="Samo ta jedinica (npr. 088)."),
) -> None:
    """Sklanja sa ekrana stare fakture koje su vec zavrsene.

    Posle prelaska ljudima na ekranu ostaju fakture od pre granice, sa
    napomenom „uradi po starom“. Dok ih covek ne zavrsi, tu i treba da stoje.
    Kad ih zavrsi, nema vise sta da se gleda, a niko nece rucno arhivirati
    dvesta redova.

    Arhivira se **samo** ono sto je i staro i vec gotovo: nacrt u stanju
    `upisan` (kalkulacija je u ERP-u, nasa ili rucna) ili `ne_treba` (avans).
    Sve ostalo se ne dira - dokument bez nacrta, „ceka sifru“, „problem“ i
    „spreman“ ostaju, jer za njih posao jos postoji.

    Arhiviranje nista ne brise: dokument se i dalje nadje pretragom i u kartici
    „Arhiva“.
    """
    _boot()
    from sqlalchemy import select

    from .models import BusinessUnit, Document, NacrtKalkulacije, StanjeNacrta

    GOTOVO = (StanjeNacrta.UPISAN, StanjeNacrta.NE_TREBA)
    granica = get_settings().novi_tok_od
    if granica is None:
        typer.secho("NOVI_TOK_OD nije zadat — bez granice se ne zna šta je staro.", fg="red")
        raise typer.Exit(1)

    with session_scope() as session:
        upit = (
            select(Document, NacrtKalkulacije, BusinessUnit)
            .join(NacrtKalkulacije, NacrtKalkulacije.document_id == Document.id)
            .join(BusinessUnit, BusinessUnit.id == Document.business_unit_id, isouter=True)
            .where(
                Document.archived == False,  # noqa: E712
                NacrtKalkulacije.stanje.in_(GOTOVO),
            )
        )
        if magacin:
            upit = upit.where(BusinessUnit.erp_code == magacin)

        redovi = [
            (doc, nacrt, jedinica)
            for doc, nacrt, jedinica in session.execute(upit)
            if doc.po_starom
        ]
        if not redovi:
            typer.echo("Nema starih faktura koje su gotove — nema šta da se arhivira.")
            raise typer.Exit(0)

        po_jedinici: dict[str, int] = {}
        for _, _, jedinica in redovi:
            kljuc = f"{jedinica.erp_code or '?'} {jedinica.name[:26]}" if jedinica else "bez jedinice"
            po_jedinici[kljuc] = po_jedinici.get(kljuc, 0) + 1

        typer.echo(f"Stare a gotove fakture (granica {granica}): {len(redovi)}")
        for kljuc in sorted(po_jedinici):
            typer.echo(f"   {po_jedinici[kljuc]:>4}  {kljuc}")
        typer.echo("")
        for doc, nacrt, _ in redovi[:6]:
            typer.echo(
                f"   {(doc.document_number or '')[:24]:<24} "
                f"{(doc.supplier_name or '')[:26]:<26} {nacrt.stanje.value}"
            )

        if not stvarno:
            typer.echo("")
            typer.secho("Suvi prolaz. Za stvarno dodaj --stvarno.", fg="yellow")
            raise typer.Exit(0)

        for doc, _, _ in redovi:
            doc.archived = True

    typer.echo("")
    typer.secho(f"Arhivirano: {len(redovi)}. Ništa nije obrisano.", fg="green")


@app.command("analiza-razvrstavanja")
def cmd_analiza_razvrstavanja(
    prag: int = typer.Option(20, "--prag", help="Koliko pogodaka pravilo mora imati."),
) -> None:
    """Koliko je razvrstavanje pouzdano i sta bi automatsko prosledjivanje obuhvatilo.

    Odgovara na pitanje kad se sme ukljuciti automatsko prosledjivanje: ne po
    osecaju, nego po tome koliko je puta operater morao da ispravi predlog.

    Meri se samo ono gde je aplikacija nesto TVRDILA pa je covek to video.
    Rucna dodela dokumenta koji app nije umela da razvrsta ne ulazi u ocenu -
    tu nije ni imala odgovor.
    """
    _boot()
    from collections import Counter

    from sqlalchemy import func, select

    from .models import Document, RoutingRule, RoutingSource

    with session_scope() as session:
        def broj(*uslovi) -> int:
            return session.scalar(
                select(func.count()).select_from(Document).where(*uslovi)
            ) or 0

        typer.echo("Po izvoru razvrstavanja:")
        for izvor in RoutingSource:
            n = broj(Document.routing_source == izvor)
            if n:
                typer.echo(f"   {izvor.value:<18} {n}")

        tacno = broj(
            Document.routing_confirmed_at.isnot(None),
            Document.routing_corrected == False,  # noqa: E712
            Document.routing_source != RoutingSource.MANUAL,
        )
        ispravljeno = broj(Document.routing_corrected == True)  # noqa: E712
        pregledano = tacno + ispravljeno
        typer.echo("")
        if not pregledano:
            typer.secho(
                "Nijedan predlog još nije prošao kroz ljudske ruke — nema šta da se oceni.",
                fg="yellow",
            )
        else:
            typer.echo(f"Predloga koje je čovek video: {pregledano}")
            typer.echo(f"   ostalo kako je app predložila: {tacno} ({tacno / pregledano:.1%})")
            typer.echo(f"   operater ispravio:             {ispravljeno} "
                       f"({ispravljeno / pregledano:.1%})")

        pravila = [p for p in session.scalars(select(RoutingRule))
                   if (p.hits or 0) or (p.misses or 0)]
        gresila = [p for p in pravila if p.misses]
        typer.echo("")
        typer.echo(f"Pravila u upotrebi: {len(pravila)}, od toga sa promašajem: {len(gresila)}")
        for p in sorted(gresila, key=lambda x: -(x.misses or 0))[:10]:
            ukupno = (p.hits or 0) + (p.misses or 0)
            typer.secho(
                f"   #{p.id:<4} {p.field.value:<20} pogodaka {p.hits or 0:<5} "
                f"promašaja {p.misses or 0:<4} tačnost {(p.hits or 0) / ukupno:.0%}",
                fg="yellow",
            )

        po_pravilu: Counter = Counter()
        for _, rid in session.execute(
            select(Document.id, Document.routing_rule_id)
            .where(Document.routing_rule_id.isnot(None))
        ):
            po_pravilu[rid] += 1
        ukupno_dok = sum(po_pravilu.values()) or 1

        typer.echo("")
        typer.echo("Šta bi automatsko prosleđivanje obuhvatilo:")
        for p_ in (5, 10, 20, 50):
            pouzdana = {p.id for p in pravila if not p.misses and (p.hits or 0) >= p_}
            obuhvat = sum(n for rid, n in po_pravilu.items() if rid in pouzdana)
            oznaka = " <- predlog" if p_ == prag else ""
            typer.echo(
                f"   bar {p_:>2} pogodaka bez ijednog promašaja: {len(pouzdana):>3} pravila, "
                f"{obuhvat:>5} dokumenata ({obuhvat / ukupno_dok:.0%}){oznaka}"
            )

        typer.echo("")
        typer.echo("Predomišljanje operatera:")
        typer.echo("   ispravljeno posle prosleđivanja:   "
                   f"{broj(Document.routing_corrected == True, Document.forwarded_at.isnot(None))}")  # noqa: E712
        typer.echo("   ispravljeno posle potvrde prijema: "
                   f"{broj(Document.routing_corrected == True, Document.received_at.isnot(None))}")  # noqa: E712


@app.command("pravilo")
def cmd_pravilo(
    broj: int = typer.Argument(..., help="Broj pravila, onaj iza # u ispisu."),
    ugasi: bool = typer.Option(False, "--ugasi", help="Isključi pravilo."),
    upali: bool = typer.Option(False, "--upali", help="Vrati isključeno pravilo."),
) -> None:
    """Prikazuje jedno pravilo i, po potrebi, gasi ga ili vraca.

    Gasi se, ne brise: brojaci pogodaka i promasaja su jedini podatak o tome
    koliko je pravilo vredelo, pa ostaju. Ugaseno pravilo se vidi u `pravila` i
    moze da se vrati jednim pozivom.
    """
    _boot()
    from sqlalchemy.orm import selectinload

    from .models import AuditLog, RoutingRule

    with session_scope() as session:
        pravilo = session.get(
            RoutingRule, broj, options=[selectinload(RoutingRule.business_unit)]
        )
        if pravilo is None:
            typer.secho(f"Nema pravila #{broj}.", fg="red")
            raise typer.Exit(1)

        jedinica = pravilo.business_unit.code if pravilo.business_unit else "?"
        ukupno = (pravilo.hits or 0) + (pravilo.misses or 0)
        tacnost = f"{(pravilo.hits or 0) / ukupno:.0%}" if ukupno else "—"
        typer.echo(
            f"#{pravilo.id}  {'aktivno' if pravilo.active else 'ISKLJUČENO'}  "
            f"prioritet {pravilo.priority}"
        )
        typer.echo(f"   {pravilo.field.value} {pravilo.op.value} „{pravilo.pattern}“")
        typer.echo(f"   -> {jedinica}")
        typer.echo(
            f"   pogodaka {pravilo.hits or 0}, promašaja {pravilo.misses or 0}, "
            f"tačnost {tacnost}"
        )
        if pravilo.comment:
            typer.echo(f"   {pravilo.comment}")

        if not ugasi and not upali:
            typer.echo("")
            typer.echo("Za gašenje dodaj --ugasi, za vraćanje --upali.")
            raise typer.Exit(0)
        if ugasi and upali:
            typer.secho("Ili --ugasi ili --upali, ne oboje.", fg="red")
            raise typer.Exit(1)

        pravilo.active = not ugasi
        session.add(AuditLog(
            actor="cli",
            action="routing.rule.disabled" if ugasi else "routing.rule.enabled",
            detail=f"#{pravilo.id} {pravilo.field.value} „{pravilo.pattern[:60]}“ -> {jedinica}",
        ))

    typer.echo("")
    typer.secho(f"Pravilo #{broj} je {'ugašeno' if ugasi else 'vraćeno'}.", fg="green")


@app.command("dovrsi-upise")
def cmd_dovrsi_upise(
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispisuje."),
    limit: int = typer.Option(50, "--limit", help="Najvise toliko dokumenata."),
) -> None:
    """Upisuje kalkulacije za robu koja je primljena a nacrt u medjuvremenu spreman.

    Isti posao radi i petnaestominutni ciklus; ovo je rucni prolaz, i jedini
    nacin da se pre upisa vidi spisak.
    """
    _boot()
    from .services.nacrti import KORISNIK_DOVRSAVANJA, upisi_u_erp, za_dovrsavanje

    podesavanja = get_settings()
    if not podesavanja.erp_upis_kalkulacije:
        typer.secho("ERP_UPIS_KALKULACIJE je isključen — nema šta da se upisuje.", fg="red")
        raise typer.Exit(1)

    kandidati = za_dovrsavanje(limit)
    if not kandidati:
        typer.echo("Nema takvih faktura — sve primljeno je i upisano.")
        raise typer.Exit(0)

    typer.echo(f"Primljeno, spremno, a nije u ERP-u: {len(kandidati)}")
    for _, broj, dobavljac, magacin, ko in kandidati:
        typer.echo(f"   {broj[:24]:<24} {dobavljac[:26]:<26} "
                   f"mag {magacin:<4} prijem: {ko}")

    if not stvarno:
        typer.echo("")
        typer.secho("Suvi prolaz. Za stvarno dodaj --stvarno.", fg="yellow")
        raise typer.Exit(0)

    uspelo = 0
    for doc_id, broj, *_ in kandidati:
        ok, poruka = upisi_u_erp(
            doc_id, korisnik=KORISNIK_DOVRSAVANJA,
            produkcija=podesavanja.erp_allow_production_write,
        )
        typer.secho(f"   {broj[:24]:<24} {poruka[:80]}", fg="green" if ok else "yellow")
        uspelo += 1 if ok else 0

    typer.echo("")
    typer.secho(f"Upisano: {uspelo} od {len(kandidati)}.", fg="green")


@app.command("opste-sifre")
def cmd_opste_sifre() -> None:
    """Šifre dobavljača koje su zapravo oznaka vrste dokumenta, ne artikal.

    Na knjižnom odobrenju dobavljač ume da pošalje istu „šifru artikla“ na svaki
    dokument — Eurotehna „Porez 20%“, EWE „USL00027“, Comtrade „R0001“. Jedna
    veza za takvu šifru štampa isti artikal na svako sledeće odobrenje.

    Ispisuje šta aplikacija smatra oznakom i da li za to postoji veza.
    """
    _boot()
    from collections import defaultdict

    from sqlalchemy import select as _select

    from .erp.kalkulacija import OZNAKA_OD_NAZIVA
    from .models import Document, DocumentLine, DocumentType, ItemMapping
    from .textutil import normalize

    with session_scope() as session:
        veze = {
            (m.supplier_vat, (m.supplier_item_id or "").strip()): m.erp_item_code
            for m in session.scalars(
                _select(ItemMapping).where(ItemMapping.active == True)  # noqa: E712
            )
            if m.supplier_item_id
        }
        podaci: dict = defaultdict(lambda: {"nazivi": set(), "ko": 0, "ime": ""})
        for pib, vrsta, ime, sifra, naziv in session.execute(
            _select(Document.supplier_vat, Document.document_type, Document.supplier_name,
                    DocumentLine.sellers_item_id, DocumentLine.name)
            .join(DocumentLine, DocumentLine.document_id == Document.id)
        ):
            if not pib or not (sifra or "").strip():
                continue
            red = podaci[(pib, sifra.strip())]
            red["nazivi"].add(normalize(naziv or ""))
            red["ime"] = ime or ""
            if vrsta is DocumentType.CREDIT_NOTE:
                red["ko"] += 1

    oznake = {k: v for k, v in podaci.items()
              if v["ko"] and len(v["nazivi"]) >= OZNAKA_OD_NAZIVA}
    typer.echo(f"Oznaka (šifra pod kojom stoji {OZNAKA_OD_NAZIVA}+ naziva): {len(oznake)}")
    typer.echo("")
    for (pib, sifra), red in sorted(oznake.items(), key=lambda x: -len(x[1]["nazivi"])):
        veza = veze.get((pib, sifra))
        typer.secho(
            f"   {red['ime'][:26]:<28} {sifra[:16]:<18} naziva {len(red['nazivi']):>3}  "
            f"odobrenja {red['ko']:>3}  "
            + (f"VEZA -> {veza}  (više se neće koristiti na odobrenjima)" if veza else ""),
            fg="yellow" if veza else None,
        )
    typer.echo("")
    typer.echo("Na običnim fakturama se ništa ne menja — tamo je šifra ipak šifra.")


@app.command("ozivi-veze")
def cmd_ozivi_veze(
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispisuje."),
) -> None:
    """Vraca veze sifara ugasene greskom, ne zato sto su stvarno dvosmislene.

    Kad stavka nema ni sifru dobavljaca ni barkod, kljuc veze je bio
    (dobavljac, prazno, prazno) - isti za svaki takav artikal tog dobavljaca.
    Zato je svako novo povezivanje gasilo prethodno kao „kolebljivo“: Dule je
    povezao sve LEXA zvucnike i sve su mu ostale na spisku (30.09.).

    Ozivljava se samo ono gde naziv vodi na tacno jednu sifru. Ako isti naziv
    kod istog dobavljaca stvarno znaci vise artikala, veza ostaje ugasena - tu
    je gasenje bilo ispravno.
    """
    _boot()
    from collections import defaultdict

    from sqlalchemy import select

    from .models import ItemMapping
    from .textutil import normalize

    with session_scope() as session:
        mrtve = list(
            session.scalars(
                select(ItemMapping).where(
                    ItemMapping.active == False,  # noqa: E712
                    ItemMapping.source == "kolebljivo",
                    ItemMapping.supplier_item_id.is_(None),
                    ItemMapping.barcode.is_(None),
                )
            )
        )
        if not mrtve:
            typer.echo("Nema veza ugašenih zbog praznog ključa.")
            raise typer.Exit(0)

        po_imenu: dict[tuple, set] = defaultdict(set)
        for m in mrtve:
            po_imenu[(m.supplier_vat, normalize(m.supplier_item_name or ""))].add(
                (m.erp_item_code or "").strip()
            )

        za_oziv = [
            m
            for m in mrtve
            if len(po_imenu[(m.supplier_vat, normalize(m.supplier_item_name or ""))]) == 1
        ]
        ostaju = [m for m in mrtve if m not in za_oziv]

        typer.echo(f"Ugašenih zbog praznog ključa: {len(mrtve)}")
        typer.echo(f"   oživljava se (naziv vodi na jednu šifru): {len(za_oziv)}")
        typer.echo(f"   ostaje ugašeno (naziv stvarno dvosmislen): {len(ostaju)}")
        typer.echo("")
        for m in za_oziv[:15]:
            typer.echo(f"   {(m.supplier_item_name or '')[:44]:<44} -> {m.erp_item_code}")
        if len(za_oziv) > 15:
            typer.echo(f"   … i još {len(za_oziv) - 15}")

        if not stvarno:
            typer.echo("")
            typer.secho("Suvi prolaz. Za stvarno dodaj --stvarno.", fg="yellow")
            raise typer.Exit(0)

        for m in za_oziv:
            m.active = True
            m.source = "rucno"

    typer.echo("")
    typer.secho(f"Oživljeno: {len(za_oziv)}. Pusti `nacrti --osvezi`.", fg="green")


@app.command("dokument")
def cmd_dokument(
    broj: str = typer.Argument(..., help="Broj fakture ili #id iz panela."),
) -> None:
    """Sve o jednom dokumentu na jednom mestu: gde je, dokle je stigao i sta ga drzi.

    Postoji zato sto se odgovor na „zasto ovo nije proslo“ do sada sklapao iz
    tri-cetiri komande i pogleda u ekran.
    """
    _boot()
    from sqlalchemy import select

    from .models import (
        AuditLog, BusinessUnit, Document, NacrtKalkulacije, PrijavaManjka,
    )

    with session_scope() as session:
        if broj.startswith("#") and broj[1:].isdigit():
            doc = session.get(Document, int(broj[1:]))
        else:
            doc = session.scalar(
                select(Document).where(Document.document_number == broj.strip())
            ) or session.scalar(
                select(Document).where(Document.document_number.like(f"%{broj.strip()}%"))
            )
        if doc is None:
            typer.secho(f"Nema dokumenta „{broj}“.", fg="red")
            raise typer.Exit(1)

        jedinice = {u.id: u.code for u in session.scalars(select(BusinessUnit))}
        typer.echo(f"#{doc.id}  {doc.document_number or '?'}  {doc.document_type.value}")
        typer.echo(f"   dobavljač   {(doc.supplier_name or '')[:40]}  PIB {doc.supplier_vat or '-'}")
        typer.echo(f"   iznos       {doc.amount or 0:,.2f} {doc.currency or ''}")
        typer.echo(f"   jedinica    {jedinice.get(doc.business_unit_id, 'nerazvrstano')}"
                   f"   SEF: {doc.sef_status.value}   stanje: {doc.state.value}")
        typer.echo(f"   prosleđeno  {doc.forwarded_at or '—'}  {doc.forwarded_by or ''}")
        typer.echo(f"   prijem      {doc.received_at or '—'}  {doc.received_by or ''}")
        if doc.po_starom:
            typer.secho("   stiglo pre prelaska — kalkulaciju pravi čovek u ERP-u",
                        fg="yellow")

        manjak = session.get(PrijavaManjka, doc.id)
        if manjak is not None:
            stanje = "otvoren" if manjak.otvoren else f"zatvoren ({manjak.zatvoreno_kako})"
            typer.echo(f"   manjak      {stanje}: {manjak.sazetak[:60]}")

        red = session.get(NacrtKalkulacije, doc.id)
        typer.echo("")
        if red is None:
            typer.secho("Nacrt kalkulacije nije ni računat — pokreni `nacrti --osvezi`.",
                        fg="yellow")
        else:
            typer.echo(f"Kalkulacija: {red.stanje.value}"
                       f"   magacin {red.magacin or '?'}   {red.erp_broj or ''}")
            if red.upisao:
                typer.echo(f"   upisao {red.upisao} {red.upisano_u or ''}")
            if red.erp_napomena:
                typer.echo(f"   {red.erp_napomena}")
            if red.upozorenje:
                typer.secho(f"   {red.upozorenje}", fg="yellow")
            for problem in red.problemi or []:
                typer.secho(f"   zaustavlja: {problem}", fg="yellow")
            for stavka in red.nemapirane or []:
                typer.secho(
                    f"   čeka šifru: stavka {stavka.get('line_no')} "
                    f"{(stavka.get('naziv') or '')[:40]} "
                    f"(EAN {stavka.get('ean') or '-'}, "
                    f"šifra dob. {stavka.get('sifra_dobavljaca') or '-'})",
                    fg="yellow",
                )
            if red.sumnja:
                typer.echo(f"   možda već u ERP-u: {red.sumnja.get('oznaka')} "
                           f"— {red.sumnja.get('razlog')}")

        zapisi = list(session.scalars(
            select(AuditLog).where(AuditLog.document_id == doc.id).order_by(AuditLog.id)
        ))
        if zapisi:
            typer.echo("")
            typer.echo("Šta se dešavalo:")
            for z in zapisi[-12:]:
                typer.echo(f"   {z.at:%d.%m. %H:%M}  {z.actor:<14} {z.action}"
                           f"  {(z.detail or '')[:60]}")


@app.command("objasni")
def cmd_objasni(
    document_id: int = typer.Argument(..., help="Broj dokumenta iz panela."),
) -> None:
    """Zasto bi dokument otisao bas toj jedinici - koja pravila pogadjaju i koje odlucuje.

    Motor ide redom po prioritetu i uzima PRVO pravilo koje pogodi. Iz spiska
    pravila se ne vidi ko se otima oko konkretnog dokumenta; ovde se vidi, jer
    su poredjana onako kako ih motor stvarno gleda.

    Pita se sam motor (`RoutingEngine.pogodjena`), ne kopija njegovih uslova:
    prva verzija je imala svoju kopiju, ispustila proveru vrste dokumenta i
    prikazivala pravila koja motor nikad ne bi pogledao.

    Nista se ne menja: dokument ostaje tamo gde jeste.
    """
    _boot()
    from sqlalchemy import select

    from .models import BusinessUnit, Document
    from .routing.engine import RoutingEngine

    with session_scope() as session:
        doc = session.get(Document, document_id)
        if doc is None:
            typer.secho(f"Nema dokumenta #{document_id}.", fg="red")
            raise typer.Exit(1)

        jedinice = {u.id: u.code for u in session.scalars(select(BusinessUnit))}
        typer.echo(
            f"#{doc.id}  {doc.document_number or '?'}  "
            f"{(doc.supplier_name or '')[:34]}  PIB {doc.supplier_vat or '-'}"
        )
        typer.echo(f"   vrsta: {doc.document_type.value}")
        typer.echo(f"   adresa isporuke: {(doc.delivery_address or '—')[:60]}")
        typer.echo(
            f"   sada: {jedinice.get(doc.business_unit_id, 'nerazvrstano')}"
            f"  ({doc.routing_source.value})"
        )
        if doc.routing_note:
            typer.echo(f"   {doc.routing_note[:100]}")
        typer.echo("")

        motor = RoutingEngine.from_db(session)
        polja = doc.routing_fields()
        pogodila = list(motor.pogodjena(polja))

        if not pogodila:
            typer.secho("Nijedno pravilo ne pogađa ovaj dokument.", fg="yellow")
        else:
            typer.echo(f"Pravila koja pogađaju ({len(pogodila)}), redom kako ih motor gleda:")
            for i, pravilo in enumerate(pogodila):
                kod = jedinice.get(pravilo.business_unit_id, "?")
                vrsta = f" [samo {pravilo.document_type.value}]" if pravilo.document_type else ""
                typer.secho(
                    f"   prioritet {pravilo.priority:<4} #{pravilo.id:<5} "
                    f"{pravilo.field.value} {pravilo.op.value} „{pravilo.pattern[:28]}“ "
                    f"-> {kod}{vrsta}" + ("  <- ODLUČUJE" if i == 0 else ""),
                    fg="green" if i == 0 else None,
                )

        odluka = motor.decide(polja)
        typer.echo("")
        typer.echo(
            f"Motor bi sada rekao: {jedinice.get(odluka.business_unit_id, 'nerazvrstano')}"
            f"  ({odluka.source.value})"
        )


@app.command("pravila-prioritet")
def cmd_pravila_prioritet(
    prioritet: int = typer.Argument(..., help="Novi prioritet (manji broj ide prvi)."),
    polje: str = typer.Option(..., "--polje", help="npr. supplier_vat"),
    operacija: str = typer.Option("", "--op", help="npr. equals"),
    jedinica: str = typer.Option("", "--jedinica", help="Sifra jedinice, npr. OFFICE"),
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispisuje."),
) -> None:
    """Menja prioritet grupi pravila.

    Motor ide redom po prioritetu i uzima PRVO pravilo koje pogodi, pa je
    prioritet jedini nacin da se kaze sta je vaznije. Mereno 29.09.: racuni za
    struju i zakup stizu na adresu apatinske prodavnice, pa je adresno pravilo
    (prioritet 50) preticalo tacno pravilo po PIB-u (prioritet 60) i odvodilo
    ih u prodavnicu. Tacan pogodak po dobavljacu mora da ide pre opste adrese.
    """
    _boot()
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from .models import BusinessUnit, MatchField, MatchOp, RoutingRule

    try:
        trazeno_polje = MatchField(polje)
    except ValueError:
        typer.secho(f"Nepoznato polje „{polje}“.", fg="red")
        raise typer.Exit(1) from None
    trazena_op = None
    if operacija:
        try:
            trazena_op = MatchOp(operacija)
        except ValueError:
            typer.secho(f"Nepoznata operacija „{operacija}“.", fg="red")
            raise typer.Exit(1) from None

    with session_scope() as session:
        upit = (
            select(RoutingRule)
            .options(selectinload(RoutingRule.business_unit))
            .where(RoutingRule.field == trazeno_polje, RoutingRule.active == True)  # noqa: E712
        )
        if trazena_op is not None:
            upit = upit.where(RoutingRule.op == trazena_op)
        if jedinica:
            cilj = session.scalar(select(BusinessUnit).where(BusinessUnit.code == jedinica))
            if cilj is None:
                typer.secho(f"Nema jedinice „{jedinica}“.", fg="red")
                raise typer.Exit(1)
            upit = upit.where(RoutingRule.business_unit_id == cilj.id)

        pravila = [p for p in session.scalars(upit) if p.priority != prioritet]
        if not pravila:
            typer.echo("Nijedno pravilo ne odgovara, ili su svi već na tom prioritetu.")
            raise typer.Exit(0)

        typer.echo(f"Pravila koja menjaju prioritet na {prioritet}: {len(pravila)}")
        for pravilo in pravila[:12]:
            kod = pravilo.business_unit.code if pravilo.business_unit else "?"
            typer.echo(
                f"   #{pravilo.id:<5} {pravilo.priority} -> {prioritet}  "
                f"{pravilo.op.value} „{pravilo.pattern[:34]}“ -> {kod}  "
                f"({pravilo.hits or 0} pogodaka)"
            )
        if len(pravila) > 12:
            typer.echo(f"   … i još {len(pravila) - 12}")

        if not stvarno:
            typer.echo("")
            typer.secho("Suvi prolaz. Za stvarno dodaj --stvarno.", fg="yellow")
            raise typer.Exit(0)

        for pravilo in pravila:
            pravilo.priority = prioritet

    typer.echo("")
    typer.secho(f"Promenjeno: {len(pravila)}.", fg="green")


@app.command("pravila-sukobi")
def cmd_pravila_sukobi(
    stvarno: bool = typer.Option(False, "--stvarno", help="Bez ovoga samo ispisuje."),
) -> None:
    """Pravila koja se medjusobno bore za isti dokument.

    Kvacica „zapamti kao pravilo“ pravi novo pravilo svaki put, a ne gleda
    postoji li vec isto sa drugom jedinicom. Mereno 29.09.: ista adresa
    „SRPSKIH VLADARA 46, 25260, Apatin“ vodila je na **deset** razlicitih
    jedinica, a isti PIB 103699024 na osam. Ko prvi pogodi, taj odlucuje - i
    zato je jedno pravilo po adresi imalo tacnost 53 %.

    Sa `--stvarno` ostaje pravilo sa najvise pogodaka, ostala se gase. Nista se
    ne brise: ugaseno pravilo se vidi u spisku i moze da se vrati.
    """
    _boot()
    from collections import defaultdict

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from .models import RoutingRule

    with session_scope() as session:
        pravila = list(
            session.scalars(
                select(RoutingRule)
                .options(selectinload(RoutingRule.business_unit))
                .where(RoutingRule.active == True)  # noqa: E712
            )
        )
        grupe: dict[tuple, list[RoutingRule]] = defaultdict(list)
        for pravilo in pravila:
            kljuc = (
                pravilo.field.value,
                pravilo.op.value,
                " ".join((pravilo.pattern or "").upper().split()),
            )
            grupe[kljuc].append(pravilo)

        sporna = {
            k: v for k, v in grupe.items()
            if len({p.business_unit_id for p in v}) > 1
        }
        dvojnici = {
            k: v for k, v in grupe.items()
            if k not in sporna and len(v) > 1
        }

        if not sporna and not dvojnici:
            typer.secho("Nema pravila koja se bore za isti dokument.", fg="green")
            raise typer.Exit(0)

        ugasiti: list[RoutingRule] = []

        if sporna:
            typer.secho(
                f"Isti obrazac vodi na razlicite jedinice: {len(sporna)}", fg="yellow"
            )
            for (polje, operacija, obrazac), lista in sorted(
                sporna.items(), key=lambda x: -len(x[1])
            ):
                lista.sort(key=lambda p: -(p.hits or 0))
                # Gasi se samo ono sto NIKAD nista nije odlucilo. Pravilo sa
                # pogotkom je nesto radilo, pa makar i lose - o njemu odlucuje
                # covek. Prva verzija je birala "najvise pogodaka pobedjuje" i
                # time bi zadrzala pravilo sa 43 pogotka i 36 promasaja, bas ono
                # zbog kog je sve i krenulo (29.09.).
                radila = [r for r in lista if (r.hits or 0) or (r.misses or 0)]
                prazna = [r for r in lista if r not in radila]
                typer.echo("")
                typer.echo(f"   {polje} {operacija} „{obrazac[:52]}“")
                for pravilo in lista:
                    jedinica = pravilo.business_unit.code if pravilo.business_unit else "?"
                    if pravilo in prazna:
                        oznaka = "     gasi se (nikad nije odlučilo)"
                    elif len(radila) > 1:
                        oznaka = "  <- TVOJA ODLUKA"
                    else:
                        oznaka = "  <- ostaje"
                    typer.echo(
                        f"      #{pravilo.id:<5} -> {jedinica:<8} "
                        f"pogodaka {pravilo.hits or 0:<5} promašaja {pravilo.misses or 0:<4}"
                        f"{oznaka}"
                    )
                if len(radila) > 1:
                    typer.secho(
                        "      → dva pravila su stvarno odlučivala; ovo ne diram, "
                        "odluči i ugasi ručno u ekranu Pravila.",
                        fg="yellow",
                    )
                ugasiti += prazna

        if dvojnici:
            broj = sum(len(v) - 1 for v in dvojnici.values())
            typer.echo("")
            typer.echo(f"Isto pravilo upisano vise puta: {len(dvojnici)} obrazaca, "
                       f"{broj} suvisnih")
            for lista in dvojnici.values():
                lista.sort(key=lambda p: -(p.hits or 0))
                # I ovde: cuva se ono sto je radilo, gasi se visak bez traga.
                ugasiti += [r for r in lista[1:] if not (r.hits or 0) and not (r.misses or 0)]

        typer.echo("")
        if not stvarno:
            typer.secho(
                f"Suvi prolaz — ugasilo bi se {len(ugasiti)} pravila. "
                f"Za stvarno dodaj --stvarno.",
                fg="yellow",
            )
            raise typer.Exit(0)

        for pravilo in ugasiti:
            pravilo.active = False

    typer.secho(f"Ugašeno: {len(ugasiti)}. Ništa nije obrisano.", fg="green")


@app.command("sumnje")
def cmd_sumnje(
    magacin: str = typer.Option("", "--magacin", help="Samo taj magacin (npr. 088)."),
) -> None:
    """Ispisuje fakture za koje app misli da su možda već u ERP-u.

    Predlog se nadje po istom dobavljacu, datumu i iznosu kad se broj racuna ne
    poklapa - obicno greska u kucanju. App ga nikad ne primenjuje sama; ovo je
    spisak da covek prodje kroz njih odjednom, umesto jednu po jednu na ekranu.
    """
    from sqlalchemy import select

    from .models import Document, NacrtKalkulacije

    with session_scope() as session:
        upit = (
            select(NacrtKalkulacije, Document)
            .join(Document, Document.id == NacrtKalkulacije.document_id)
            .where(NacrtKalkulacije.sumnja.is_not(None))
            .order_by(NacrtKalkulacije.magacin, Document.document_number)
        )
        if magacin:
            upit = upit.where(NacrtKalkulacije.magacin == magacin)
        # MS SQL u JSON koloni cuva 'null' kao tekst, sto nije SQL NULL - pa
        # filter propusti red u kom nema nicega. Provera u Pajtonu je jedina
        # pouzdana (naslo se 29.09.: komanda je pukla na drugom redu).
        redovi = [(red, doc) for red, doc in session.execute(upit) if red.sumnja]

        if not redovi:
            typer.echo("Nema nijednog predloga.")
            raise typer.Exit(0)

        neodluceno = sum(1 for red, _ in redovi if not red.sumnja_odbio)
        typer.echo(f"Predloga: {len(redovi)}, od toga neodlučenih {neodluceno}")
        typer.echo("")
        for red, doc in redovi:
            stanje = f"odbio {red.sumnja_odbio}" if red.sumnja_odbio else "čeka odluku"
            typer.echo(
                f"  #{doc.id:<6} {(doc.document_number or '')[:26]:<26} "
                f"{(doc.supplier_name or '')[:26]:<26} {red.magacin or '?':<4} {stanje}"
            )
            typer.echo(f"         → {red.sumnja.get('oznaka')}  {red.sumnja.get('razlog')}")


@app.command("stavke")
def cmd_stavke(
    broj: str = typer.Argument(..., help="Broj racuna dobavljaca (ili deo broja)."),
) -> None:
    """Ispisuje stavke jednog dokumenta: kolicina, iznos, stopa, poreska kategorija.

    Sluzi kad neka faktura zaskripi u ERP-u pa treba videti sta je tacno u njoj -
    npr. racun za struju gde su PDV i naknade razdvojeni po stavkama.
    """
    _boot()
    from sqlalchemy import select as _select

    from .models import Document

    with session_scope() as session:
        docs = list(
            session.scalars(
                _select(Document)
                .where(Document.document_number.like(f"%{broj}%"))
                .order_by(Document.issue_date.desc())
            )
        )
        _ispisi_stavke(docs, broj)


def _ispisi_stavke(docs, broj: str) -> None:
    if not docs:
        typer.secho(f"Nema dokumenta sa brojem koji sadrzi '{broj}'.", fg="red")
        raise typer.Exit(1)
    if len(docs) > 1:
        typer.echo(f"Nadjeno {len(docs)} dokumenata, ispisujem sve.")
        typer.echo("")

    for doc in docs:
        typer.echo(f"#{doc.id}  {doc.document_number}  {doc.supplier_name} (PIB {doc.supplier_vat})")
        typer.echo(
            f"   izdat {doc.issue_date}  promet {doc.delivery_date or '-'}  "
            f"valuta {doc.due_date or '-'}"
        )
        typer.echo(
            f"   osnovica {doc.sum_without_vat or 0:,.2f}  PDV {doc.vat_amount or 0:,.2f}  "
            f"ukupno {doc.amount or 0:,.2f}"
        )
        if doc.billing_reference:
            typer.echo(f"   ispravlja: {doc.billing_reference} ({doc.billing_reference_count} veza)")
        for oznaka, tekst in (
            ("napomena", doc.note),
            ("prilog", doc.attachment_text),
            ("poziv kupca", doc.buyer_reference),
            ("ugovor", doc.contract_reference),
            ("dodatna veza", doc.additional_reference),
        ):
            if tekst:
                sazeto = " ".join(str(tekst).split())
                if len(sazeto) > 300:
                    sazeto = sazeto[:300] + " […]"
                typer.echo(f"   {oznaka}: {sazeto}")
        typer.echo("")
        typer.echo("   rb  kolicina        iznos  stopa kat  naziv")
        po_stopi: dict[tuple[float | None, str | None], float] = {}
        for linija in doc.lines:
            kljuc = (linija.vat_percent, linija.vat_category)
            po_stopi[kljuc] = po_stopi.get(kljuc, 0.0) + (linija.line_amount or 0.0)
            typer.echo(
                f"   {linija.line_no:>3} {linija.quantity or 0:>9.2f} "
                f"{linija.line_amount or 0:>12,.2f} {linija.vat_percent or 0:>5.0f}% "
                f"{(linija.vat_category or '-'):<3}  {(linija.name or '')[:44]}"
            )
        typer.echo("")
        typer.echo("   zbir po stopi i kategoriji")
        for (stopa, kat), iznos in sorted(po_stopi.items(), key=lambda t: (t[0][0] or 0)):
            typer.echo(f"      {stopa or 0:>3.0f}% {kat or '-':<3} {iznos:>14,.2f}")
        typer.echo("")


@app.command("provera-prelaska")
def cmd_provera_prelaska() -> None:
    """Šta još stoji na putu prelasku na novi način rada.

    Jedna komanda umesto spiska koji se pamti napamet. Ne menja ništa — samo
    gleda podešavanja, dozvole u ERP-u, adrese jedinica i stanje nacrta.
    """
    _boot()
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select
    from sqlalchemy import text as _text

    from .erp.connection import get_erp_engine
    from .models import BusinessUnit, Document, NacrtKalkulacije, StanjeNacrta, UnitKind

    podesavanja = get_settings()
    nalazi: list[tuple[bool, str]] = []

    def red(u_redu: bool, tekst: str) -> None:
        nalazi.append((u_redu, tekst))

    # --- podesavanja ---
    red(
        podesavanja.erp_upis_kalkulacije,
        "ERP_UPIS_KALKULACIJE je uključen" if podesavanja.erp_upis_kalkulacije
        else "ERP_UPIS_KALKULACIJE je isključen — dugme za upis odbija posao",
    )
    red(
        podesavanja.novi_tok_od is not None,
        f"NOVI_TOK_OD = {podesavanja.novi_tok_od}" if podesavanja.novi_tok_od
        else "NOVI_TOK_OD nije zadat — stare fakture bi nudile upis kao i nove",
    )
    # Prihvatanje na SEF-u je pravni cin prema dobavljacu. Upis kalkulacije ga
    # ne dira - jedini put do automatskog prihvatanja je ova politika, pa mora
    # da se vidi u proveri, a ne da se podrazumeva.
    red(
        podesavanja.auto_accept == "off",
        "Prihvatanje na SEF-u radi samo čovek (AUTO_ACCEPT=off)"
        if podesavanja.auto_accept == "off"
        else f"PAZI: AUTO_ACCEPT={podesavanja.auto_accept} — sinhronizacija sama "
        f"prihvata fakture na SEF-u",
    )

    red(
        True,
        "Kompletna faktura se prihvata na SEF-u odmah po upisu kalkulacije"
        if podesavanja.prihvati_posle_upisa
        else "Prihvatanje ne ide uz upis (PRIHVATI_POSLE_UPISA=false)",
    )

    proba = podesavanja.notify_override_to.strip()
    red(
        not proba,
        "Obaveštenja idu poslovnim jedinicama" if not proba
        else f"PROBNI REŽIM: sve ide na {proba}, poslovođe ne dobijaju ništa",
    )
    red(
        bool(podesavanja.public_base_url and "localhost" not in podesavanja.public_base_url),
        f"PUBLIC_BASE_URL = {podesavanja.public_base_url}",
    )
    red(
        bool(podesavanja.smtp_host or podesavanja.smtp_source_file),
        "Pošta je podešena" if (podesavanja.smtp_host or podesavanja.smtp_source_file)
        else "Pošta nije podešena — obaveštenja neće otići",
    )
    red(
        podesavanja.erp_allow_production_write,
        "ERP_ALLOW_PRODUCTION_WRITE je uključen — upis ide u pravu bazu"
        if podesavanja.erp_allow_production_write
        else "ERP_ALLOW_PRODUCTION_WRITE je isključen — upis ide u test bazu",
    )

    # --- dozvole u produkcionoj bazi ---
    TABELE = ("ULKALKUL", "PRULKALK", "MKALKUL", "MPRULK", "PDVEVIDX")
    try:
        with get_erp_engine().connect() as conn:
            ima = {
                r[0]
                for r in conn.execute(_text(
                    "select o.name from sys.database_permissions dp "
                    "join sys.database_principals p on p.principal_id = dp.grantee_principal_id "
                    "join sys.objects o on o.object_id = dp.major_id "
                    "where dp.permission_name = 'INSERT' and dp.state = 'G' "
                    "  and p.name = USER_NAME()"
                ))
            }
            baza, nalog = conn.execute(_text("select DB_NAME(), USER_NAME()")).first()
        fali = [t for t in TABELE if t not in ima]
        red(not fali, f"Dozvole za upis u {baza} (nalog {nalog}): "
            + ("sve na broju" if not fali else "fali INSERT na " + ", ".join(fali)
               + f" — GRANT INSERT ON dbo.<tabela> TO [{nalog}]"))
    except Exception as exc:  # noqa: BLE001 - poruka ide coveku
        red(False, f"Dozvole nisu proverene: {str(exc)[:90]}")

    # --- jedinice bez adrese ---
    with session_scope() as session:
        # Gledaju se SVE jedinice koje primaju dokumente, ne samo maloprodaja:
        # magacin povrata je bez mejla docekao prvi dokument i prosledjivanje je
        # palo porukom "slanje nije uspelo" (30.09.).
        bez_mejla = [
            u.code
            for u in session.scalars(
                _select(BusinessUnit).where(BusinessUnit.active == True)  # noqa: E712
            )
            if not (u.emails or "").strip()
            and session.scalar(
                _select(_func.count()).select_from(Document)
                .where(Document.business_unit_id == u.id)
            )
        ]
        red(not bez_mejla, "Sve jedinice sa dokumentima imaju mejl"
            if not bez_mejla else "Bez mejla, a imaju dokumente: " + ", ".join(sorted(bez_mejla)))

        stanja = {
            s.value: session.scalar(
                _select(_func.count()).select_from(NacrtKalkulacije)
                .where(NacrtKalkulacije.stanje == s)
            ) or 0
            for s in StanjeNacrta
        }
        spremnih = stanja.get("spreman", 0)
        po_starom = 0
        if podesavanja.novi_tok_od:
            po_starom = sum(
                1
                for n in session.scalars(
                    _select(NacrtKalkulacije)
                    .where(NacrtKalkulacije.stanje == StanjeNacrta.SPREMAN)
                )
                if n.document is not None and n.document.po_starom
            )
        red(
            spremnih == po_starom or podesavanja.novi_tok_od is not None,
            f"Nacrti: spremnih {spremnih} (od toga {po_starom} po starom), "
            f"čeka šifru {stanja.get('ceka_sifru', 0)}, "
            f"upisanih {stanja.get('upisan', 0)}, problema {stanja.get('problem', 0)}",
        )

        # --- ista prijava uz vise jedinica ---
        dvosmislene: dict[str, list[str]] = {}
        for j in session.scalars(
            _select(BusinessUnit).where(BusinessUnit.logins.is_not(None))
        ):
            for prijava in j.login_list():
                dvosmislene.setdefault(prijava, []).append(j.code)
        sporne = {p: v for p, v in dvosmislene.items() if len(v) > 1}
        red(
            not sporne,
            "Nijedna prijava nije upisana uz više jedinica" if not sporne
            else "Prijava uz više jedinica: "
            + "; ".join(f"{p} → {', '.join(v)}" for p, v in sorted(sporne.items())),
        )

        # --- prihvacena odobrenja bez obavestenja o prethodnom porezu ---
        try:
            from .services.obavestenje import bez_obavestenja

            propalo = bez_obavestenja(session)
        except Exception as exc:  # noqa: BLE001 - provera ne sme da padne zbog ovoga
            propalo = []
            log.debug("Provera obaveštenja nije prošla: %s", exc)
        red(
            not propalo,
            "Sva prihvaćena knjižna odobrenja imaju obaveštenje o prethodnom porezu"
            if not propalo
            else f"Bez obaveštenja o prethodnom porezu: {len(propalo)} "
            + ", ".join((d.document_number or "?") for d in propalo[:5]),
        )

        # --- sema panela: nove kolone i tabele posle dopune ---
        from sqlalchemy import inspect as _inspect

        from .db import get_engine as _get_engine
        from .models import PrijavaManjka

        inspektor = _inspect(_get_engine())
        fali_sema = []
        if not inspektor.has_table("prijava_manjka"):
            fali_sema.append("tabela prijava_manjka")
        kolone = {c["name"] for c in inspektor.get_columns("nacrt_kalkulacije")}
        for kolona in ("sumnja", "sumnja_odbio", "sumnja_odbijeno_u", "erp_napomena"):
            if kolona not in kolone:
                fali_sema.append(f"nacrt_kalkulacije.{kolona}")
        red(
            not fali_sema,
            "Šema panela je dopunjena" if not fali_sema
            else "Šema nije dopunjena, fali: " + ", ".join(fali_sema)
            + " — restartuj panel, dopuna ide pri pokretanju",
        )

        # --- predlozi i manjkovi ---
        if not fali_sema:
            ceka_odluku = session.scalar(
                _select(_func.count()).select_from(NacrtKalkulacije).where(
                    NacrtKalkulacije.sumnja.is_not(None),
                    NacrtKalkulacije.sumnja_odbio.is_(None),
                )
            ) or 0
            # vidi `sumnje`: JSON 'null' nije SQL NULL
            ceka_odluku = sum(
                1
                for red in session.scalars(
                    _select(NacrtKalkulacije).where(
                        NacrtKalkulacije.sumnja.is_not(None),
                        NacrtKalkulacije.sumnja_odbio.is_(None),
                    )
                )
                if red.sumnja
            )
            red(
                True,
                f"Predloga „možda je već u ERP-u“: {ceka_odluku} čeka odluku"
                + (" — prođi ih komandom `sumnje`" if ceka_odluku else ""),
            )
            otvorenih = session.scalar(
                _select(_func.count()).select_from(PrijavaManjka)
                .where(PrijavaManjka.zatvoreno_u.is_(None))
            ) or 0
            red(True, f"Otvorenih prijava manjka: {otvorenih}")

    typer.echo("")
    for u_redu, tekst in nalazi:
        typer.secho(("  ok    " if u_redu else "  PAZI  ") + tekst,
                    fg="green" if u_redu else "yellow")
    prepreka = sum(1 for u_redu, _ in nalazi if not u_redu)
    typer.echo("")
    if prepreka:
        typer.secho(f"{prepreka} stvar(i) traži odluku pre prelaska.", fg="yellow")
    else:
        typer.secho("Sve stoji.", fg="green")


@app.command("meri-odobrenja")
def cmd_meri_odobrenja(
    koliko: int = typer.Option(10, help="Koliko primera ispisati."),
    dobavljac: str = typer.Option(None, help="Samo taj PIB."),
) -> None:
    """Koliko knjižnih odobrenja bez artikla se može razrešiti iz napomene.

    Eurotehna u stavku upiše „Odobrenje po Saglasnosti broj …“, što nikad neće
    imati našu šifru, a u napomeni stoji broj fakture koju odobrenje ispravlja.
    Ovo meri koliko puta se po tom broju nađe tačno jedna ranija stavka — dakle
    koliko bi se moglo predložiti samo, bez čoveka.
    """
    _boot()
    from collections import Counter

    from sqlalchemy import select as _select

    from .erp.connection import get_erp_engine
    from .erp.kalkulacija import _analitika_dobavljaca, _nasa_sifra
    from .models import Document, DocumentType
    from .services.odobrenja import (
        artikal_po_modelu, artikli_sa_racuna, baze_po_godinama,
        broj_originalnog_racuna, izaberi_stavku, sifra_postoji_danas,
    )

    with session_scope() as session:
        odobrenja = list(
            session.scalars(
                _select(Document)
                .where(Document.document_type == DocumentType.CREDIT_NOTE)
                # Arhivirana odobrenja su upravo istorija na kojoj ovo ima smisla
                # meriti - bez njih se gleda dvadesetak najnovijih.
                .order_by(Document.issue_date.desc())
            )
        )
        podaci = [
            {
                "id": d.id,
                "broj": d.document_number,
                "dobavljac": d.supplier_name,
                "pib": d.supplier_vat,
                "mb": d.supplier_reg_no,
                "racun": d.payment_account,
                "napomena": d.note,
                "iznos": d.sum_without_vat,
                "linije": [x for x in d.lines if not x.nije_artikal],
            }
            for d in odobrenja
            if not dobavljac or (d.supplier_vat or "") == dobavljac
        ]
        # `_nasa_sifra` gleda i mapiranja u panelu, pa mora unutar sesije.
        with get_erp_engine().connect() as conn:
            for o in podaci:
                o["bez_sifre"] = any(
                    _nasa_sifra(session, conn, o["pib"], linija)[0] is None
                    for linija in o["linije"]
                ) or not o["linije"]
                del o["linije"]

    typer.echo(f"Knjižnih odobrenja: {len(podaci)}")
    ishod: Counter = Counter()
    po_godini: Counter = Counter()
    bez_broja: Counter = Counter()
    primeri: list[str] = []
    napomene: list[str] = []

    with get_erp_engine().connect() as conn:
        typer.echo(f"Baze koje smem da čitam: {', '.join(baze_po_godinama(conn)) or '—'}")
        for o in podaci:
            # Da li odobrenju treba pomoć ne procenjuje se po nazivu stavke nego
            # po tome da li stavka uopšte dobije šifru. Prva verzija je tražila
            # ključne reči i promašila „KNJIZNO ODOBRENJE 20%“ (EWE COMP), koje
            # jeste stavka bez artikla.
            if not o["bez_sifre"]:
                ishod["stavka već navodi artikal"] += 1
                continue
            bez_broja[o["dobavljac"] or "?"] += 0  # da se dobavljač pojavi u preseku
            broj = broj_originalnog_racuna(o["napomena"])
            if broj is None:
                ishod["napomena ne nosi broj računa"] += 1
                bez_broja[o["dobavljac"] or "?"] += 1
                if len(napomene) < koliko:
                    n = " ".join((o["napomena"] or "(prazna)").split())
                    napomene.append(f"   {o['broj']:<18} {(o['dobavljac'] or '?')[:22]:<22} {n[:90]}")
                continue
            dobavljac = _analitika_dobavljaca(conn, o["mb"], o["pib"], o["racun"])
            if dobavljac is None:
                ishod["dobavljač nije u NAZIVI"] += 1
                continue
            nadjeno = artikli_sa_racuna(conn, dobavljac, broj)
            a, kako = izaberi_stavku(nadjeno, o["iznos"])
            if a is not None:
                po_godini[a["baza"]] += 1
                if not sifra_postoji_danas(conn, a["artikal"]):
                    ishod["razrešeno, ali šifre više nema u šifarniku"] += 1
                else:
                    ishod[f"razrešeno ({kako})"] += 1
                if len(primeri) < koliko:
                    primeri.append(
                        f"   {o['broj']:<18} -> {broj:<14} {a['baza']:<10} {a['artikal']} "
                        f"{a['naziv'][:26]:<26} nab {float(a['nabavna']):>10,.2f}  {kako}"
                    )
            elif nadjeno:
                ishod[f"račun nađen, ali {kako}"] += 1
            else:
                po_modelu = artikal_po_modelu(conn, o["napomena"])
                ishod["račun nije nađen" + (", ali model jeste" if po_modelu else "")] += 1

    typer.echo("")
    for uzrok, n in ishod.most_common():
        typer.echo(f"  {n:>4}  {uzrok}")
    if po_godini:
        typer.echo("")
        typer.echo("Iz koje godine je originalna faktura:")
        for baza, n in po_godini.most_common():
            typer.echo(f"  {n:>4}  {baza}")
    if primeri:
        typer.echo("")
        typer.echo("Primeri razrešenih:")
        for p in primeri:
            typer.echo(p)
    if bez_broja:
        typer.echo("")
        typer.echo("Odobrenja bez artikla, po dobavljaču (u zagradi bez broja računa):")
        for ime, n in bez_broja.most_common(10):
            typer.echo(f"   {ime[:40]:<40} ({n})")
    if napomene:
        typer.echo("")
        typer.echo("Kako izgledaju napomene iz kojih broj nije izvučen:")
        for n in napomene:
            typer.echo(n)


@app.command("osvezi-ubl")
def cmd_reparse(
    dokument: int = typer.Option(None, "--dokument", help="Samo taj dokument (ID iz panela)."),
) -> None:
    """Ponovo čita sačuvane UBL fajlove sa diska — bez obraćanja SEF-u.

    Kad parser nauči novo polje, istorija ga nema u bazi. Fajlovi su već na disku,
    pa se ne skidaju ponovo. Potvrđene ERP šifre stavki ostaju netaknute, a
    razvrstavanje se ne dira — za to postoji `razvrstaj`.
    """
    _boot()
    from sqlalchemy import select as _select

    from .models import Document
    from .services.ingest import IngestService

    with session_scope() as session:
        upit = _select(Document.id).where(Document.ubl_path.is_not(None))
        if dokument is not None:
            upit = upit.where(Document.id == dokument)
        ids = list(session.scalars(upit.order_by(Document.id)))

    if not ids:
        typer.echo("Nema dokumenata sa sačuvanim UBL-om.")
        return

    typer.echo(f"Ponovo čitam {len(ids)} dokumenata...")
    servis = IngestService()
    procitano = bez_fajla = greske = 0
    for redni, doc_id in enumerate(ids, 1):
        try:
            if servis.ponovo_parsiraj(doc_id):
                procitano += 1
            else:
                bez_fajla += 1
        except Exception as exc:  # noqa: BLE001
            greske += 1
            typer.secho(f"  #{doc_id}: {exc}", fg="yellow")
        if redni % 500 == 0:
            typer.echo(f"  {redni}/{len(ids)}")
    typer.echo(f"Pročitano: {procitano}, bez fajla: {bez_fajla}, greške: {greske}")


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


def _ispisi_skriptu(
    novi: str, lozinka: str | None, sql_fajl: str | None, u_produkciju: bool = True
) -> None:
    """Naredbe za zaseban ERP nalog i red za `.env`, iz istog pokretanja."""
    from .erp.nalog import TABELE_UPISA, skripta

    s = skripta(novi, lozinka, upis_u_produkciju=u_produkciju)
    if s.get("greska"):
        typer.secho(s["greska"], fg="yellow")
        raise typer.Exit(1)

    typer.echo("")
    typer.echo("=" * 72)
    typer.echo(f"Naredbe za nalog {s['nalog']}")
    typer.echo(f"  čita : {', '.join(s['citanje'])}")
    typer.echo(f"  piše : {', '.join(s['upis'])}  —  {len(TABELE_UPISA)} tabela")
    typer.echo("=" * 72)
    if sql_fajl:
        from pathlib import Path

        # Red za .env ide u isti fajl, kao komentar. Lozinka je u njemu ionako
        # (CREATE LOGIN), a ovako ne mogu da se raziđu — najčešći razlog zašto
        # prijava novog naloga padne je .env iz jednog, a naredbe iz drugog
        # pokretanja.
        Path(sql_fajl).write_text(
            s["sql"] + "\n\n-- Red za C:\\efakture\\.env, iz ovog istog pokretanja:\n-- "
            + s["env"]
            + "\n",
            encoding="utf-8",
        )
        typer.echo(f"Naredbe upisane u {sql_fajl}")
        typer.secho(
            f"U tom fajlu je i lozinka — obriši ga kad završiš ({sql_fajl}).", fg="yellow"
        )
    else:
        typer.echo(s["sql"])
    typer.echo("")
    typer.echo("Posle toga u C:\\efakture\\.env dodaj jedan red:")
    typer.echo("")
    typer.echo("  " + s["env"])
    typer.echo("")
    typer.secho(
        "Lozinka se nigde ne pamti i svako pokretanje izmišlja novu — uzmi naredbe "
        "i red za .env iz ISTOG ispisa. Posle restarta proveri sa `sefsync erp-nalog`: "
        "mora da piše da je veza sopstvena.",
        fg="cyan",
    )


@app.command("erp-nalog")
def cmd_erp_nalog(
    novi: str = typer.Option(
        None, "--novi", help="Sastavi naredbe za zaseban nalog pod ovim imenom."
    ),
    lozinka: str = typer.Option(None, help="Lozinka za novi nalog; bez nje se izmišlja."),
    sql_fajl: str = typer.Option(None, "--sql-fajl", help="Upiši naredbe u ovaj fajl."),
    bez_produkcije: bool = typer.Option(
        False,
        "--bez-produkcije",
        help="Nalog za razvojnu mašinu: čita sve, piše samo u test bazu.",
    ),
) -> None:
    """Šta ERP nalog panela sme, i šta mu od toga aplikaciji fali.

    Bez opcija samo meri postojeće stanje. Sa `--novi sefsync_erp` sastavi i
    naredbe za zaseban nalog — aplikacija ih ne izvršava, `CREATE LOGIN` traži
    prava na nivou servera.
    """
    _boot()
    from .erp.nalog import pregled

    p = pregled()
    if p.get("greska"):
        typer.secho(p["greska"], fg="red")
        for red in p.get("objasnjenje", []):
            typer.echo(red)
        # Sa `--novi` se ne staje: naredbe se sastavljaju iz podešavanja, ne iz
        # veze, a trebaju baš kad veza ne radi.
        if not novi:
            raise typer.Exit(1)
        typer.echo("")
        typer.secho("Naredbe ispod ne zavise od te veze — pusti ih pa probaj ponovo.", fg="cyan")
        _ispisi_skriptu(novi, lozinka, sql_fajl, not bez_produkcije)
        raise typer.Exit(1)

    typer.echo(f"nalog       : {p['nalog']}  (u bazi: {p['korisnik']})")
    typer.echo(f"radna baza  : {p['radna_baza']}   test: {p['test_baza'] or '—'}")
    if p.get("razvojni"):
        typer.echo("vrsta       : razvojni — u produkciju NE upisuje, i tako treba")
    if p["iz_secrets_fajla"]:
        typer.secho(
            "veza        : POZAJMLJENA — ERP_DB_URL nije zadat, čita se iz "
            f"{p['secrets_fajl'] or 'web.config-a drugog projekta'}",
            fg="yellow",
        )
    else:
        typer.echo("veza        : ERP_DB_URL (sopstvena)")
    typer.echo("")

    fali_negde = False
    for baza, s in p["baze"].items():
        if s.get("greska"):
            typer.echo(f"  {baza:16} ne mogu: {s['greska']}")
            continue
        red = f"  {baza:16} {'čita' if s['cita'] else 'NE ČITA':8}"
        red += f" INSERT: {', '.join(s['upis']) if s['upis'] else '—'}"
        typer.echo(red)
        if s.get("fali_upis"):
            fali_negde = True
            typer.secho(f"  {'':16} fali INSERT: {', '.join(s['fali_upis'])}", fg="yellow")
        if s.get("suvisno"):
            typer.secho(
                f"  {'':16} suvišno (app ne piše): {', '.join(s['suvisno'])}", fg="yellow"
            )

    if p["suvisan_pristup"]:
        typer.echo("")
        typer.secho(
            "  pristup koji aplikaciji ne treba: " + ", ".join(p["suvisan_pristup"]),
            fg="yellow",
        )

    if not fali_negde and not p["iz_secrets_fajla"]:
        typer.echo("")
        if p.get("razvojni"):
            typer.secho(
                "Sve stoji: razvojni nalog, čita sve a piše samo u test bazu.", fg="green"
            )
        else:
            typer.secho("Sve stoji: nalog je sopstveni i ima tačno što mu treba.", fg="green")

    if novi:
        _ispisi_skriptu(novi, lozinka, sql_fajl, not bez_produkcije)



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
