# ELBRACO — e-fakture sa SEF-a

Radni panel za ulazne e-fakture sa SEF-a. **Jedna osoba ima sve pred sobom**: dokumenti se
preuzimaju sami i sami se razvrstavaju na poslovnu jedinicu, a operater razrešava ono što
aplikacija nije znala, ispravlja ako je pogrešila, prihvata ili odbija na SEF-u i **klikom
prosleđuje** poslovođi. Poslovođa potvrđuje da je roba stigla.

Ništa ne ide poslovođi samo od sebe — prosleđivanje je potez operatera.

## Ko šta vidi

Prijava ide **istim nalogom kojim se ulazi u ERP** — lozinku proverava SQL server tako što
se njome otvori veza, pa se kod nas ne čuva nijedna. Ko je ko čita se iz `dbo.kontakti_osobe`
po koloni `[User]`. Isto rešenje kao u projektu ELBRACO LOGISTIKA, namerno — jedan nalog
po čoveku.

Uloga: ko je upisan u `WEB_OPERATORS` je **operater**; ko ima objekat
(`SifraPovezanogObjekta`) je **poslovođa** i vidi samo taj objekat; ostali nemaju pristup.
Spisak operatera je imenovan zato što prihvatanje fakture obavezuje firmu prema dobavljaču —
to pravo se ne dobija time što neko ima nalog na serveru. (Bez tog spiska pravo bi imalo
sedam ljudi, uključujući magacionere i vozača.)

Operater sa upisanim objektom **ostaje operater** — provera spiska ide pre provere objekta.
To je slučaj osobe koja fizički sedi u jednom objektu a odgovorna je za celu firmu.

`sefsync ko-moze` ispisuje ko trenutno može da uđe i sa kojom ulogom.

| | Operater | Poslovođa |
|---|---|---|
| Red svih dokumenata | ✓ | |
| Menja poslovnu jedinicu | ✓ | |
| Prihvata / odbija na SEF-u | ✓ | |
| Prosleđuje poslovođi | ✓ | |
| Vidi svoju jedinicu | ✓ | ✓ (samo svoju, samo prosleđeno) |
| Potvrđuje prijem robe | | ✓ |
| Šifarnici i pravila | ✓ | |

---

## Kako radi

```
SEF                          aplikacija                    OPERATER            POSLOVOĐA
───                          ──────────                    ────────            ─────────
/overview  ──▶  zaglavlja u bazu
/xml       ──▶  UBL + stavke
                ▼
                pravila ──▶ predlog poslovne jedinice
                ▼
                ┌──────────── RED ────────────┐ ──────▶  potvrdi ili ispravi PJ
                │ dok i SEF i prosleđivanje   │          prihvati / odbij ──▶ /acceptReject
                │ nisu gotovi, stoji u redu   │          prosledi ────────────▶ mejl
                └─────────────────────────────┘                                  ▼
                                                                            potvrdi prijem
```

Prihvatanje na SEF-u i prosleđivanje su **nezavisni** — operater bira redosled od slučaja
do slučaja.

## Kako aplikacija uči

Razvrstavanje je **predlog**, ne odluka. Operater ga potvrđuje ili menja, i iz toga se uči:

- **Potvrda** (dugme, ili samo prosleđivanje — niko ne šalje dokument u pogrešan objekat)
  broji se kao pogodak pravila.
- **Ispravka** broji se kao promašaj *onog pravila koje je pogrešilo*, pa se na
  [/pravila](http://localhost:8080/pravila) i [/tacnost](http://localhost:8080/tacnost) vidi
  koje pravilo je počelo da greši — objekat se preselio, dobavljač promenio zapis adrese,
  ili je pravilo od početka bilo preširoko.
- **Ručna dodela dokumenta koji je bio nerazvrstan** ne računa se ni kao pogodak ni kao
  promašaj: tu aplikacija nije ništa tvrdila. Zato mera tačnosti ne može da se naduva.
- Uz ispravku se novo pravilo pamti samo od sebe, po **najužem podatku koji taj dokument
  ima**: adresa isporuke → naziv lokacije → narudžbenica → referenca → ugovor → PIB.

Cilj je da se posao operatera s vremenom svede na potvrđivanje, a da ručnog razvrstavanja
bude sve manje. Faza 2 se kači na isti klik „prosledi“: uz obaveštenje ide i ulazna kalkulacija
u ERP-u, koju poslovođa samo zaknjiži.

**Preuzimanje UBL-a menja status na SEF-u.** Izmereno na produkciji: „Seen“ događaj stigne
70–90 ms posle našeg poziva na `/purchase-invoice/xml`. Dok ljudi još prate šta je novo na
portalu, to im remeti posao — zato `SEF_PRESERVE_NEW=true` preskače dokumente u statusu
„Nova“. Oni se u redu vide iz pregleda (dobavljač, iznos, datum) i mogu se razvrstati po
pravilu za dobavljača, a stavke i adresa isporuke stižu čim ih neko otvori na portalu.
Kad ceo tok pređe u aplikaciju, prekidač se gasi.

Sinhronizacija je **idempotentna** — dokument se prepoznaje po `InvoiceId` sa SEF-a, pa ponovno
prolaženje kroz isti period samo osvežava statuse. Prozor se preklapa `SYNC_OVERLAP_DAYS`
dana unazad da bi se pohvatale zakasnele izmene.

---

## Instalacija

Potreban je Python 3.11+ (testirano na 3.14).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"      # dodaj ",mssql" za MS SQL bazu
copy .env.example .env                                      # pa popuni vrednosti
.\.venv\Scripts\sefsync.exe init-db
```

### Podešavanje (.env)

| Ključ | Značenje |
|---|---|
| `SEF_ENV` | `demo` (demoefaktura.mfin.gov.rs) ili `prod` (efaktura.mfin.gov.rs) |
| `SEF_API_KEY` | API ključ sa portala eFaktura: **Podešavanja → API management** |
| `COMPANY_VAT` | PIB naše firme (koristi se za proveru naloga) |
| `DB_URL` | baza aplikacije; podrazumevano SQLite u `data/`, u produkciji MS SQL |
| `ERP_DB_URL` / `ERP_SECRETS_FILE` | ERP baza: ili direktan URL, ili putanja do postojećeg user-secrets skladišta |
| `SEF_PRESERVE_NEW` | ne preuzimaj dokumente u statusu „Nova“ (da im se ne obori status) |
| `AUTO_ACCEPT` | `off` / `routed` / `all` — automatsko prihvatanje na SEF-u |
| `SMTP_*` / `SMTP_SOURCE_FILE` | slanje e-maila: ili ovde, ili iz `web.config`-a projekta koji već šalje (`Mail__*`) |
| `NOTIFY_BCC` | kopija svih obaveštenja; ova adresa dobija i nerazvrstane dokumente |
| `PUSH_*` | Viber/WhatsApp/SMS preko HTTP gateway-a |
| `WEB_USERS` | `korisnik:lozinka,drugi:lozinka` za prijavu na dashboard |

**Prvo proveri vezu:**

```powershell
.\.venv\Scripts\sefsync.exe check
```

---

## Puštanje u rad (redosled)

### 1. Unesi poslovne jedinice

Tri načina — dashboard (`/pj`), CSV, ili sinhronizacija iz postojeće tabele na serveru.

**Iz ERP baze (podešeno):** šifarnik se čita iz produkcione `ELBS_2026`
(`dbo.PRODAV`, `dbo.PRODAV_KONTAKT`, `dbo.MAGACINI`, `dbo.MAGACIN_KONTAKT`, `dbo.kontakti_osobe`).

Lozinka se **ne prepisuje** u ovaj projekat — čita se iz `dotnet user-secrets` skladišta
projekta ELBRACO LOGISTIKA, pa postoji samo jedna kopija i menja se na jednom mestu:

```ini
ERP_SECRETS_FILE=C:/Users/<ti>/AppData/Roaming/Microsoft/UserSecrets/<id>/secrets.json
ERP_SECRETS_KEY=ConnectionStrings:ErpDatabase
BU_SOURCE_SQL=<upit iz sql/poslovne_jedinice.sql, u jednom redu>
```

Alternativa je klasično `BU_SOURCE_DB_URL=mssql+pyodbc://...` ako se konekcija drži ovde.
Upit je u [sql/poslovne_jedinice.sql](sql/poslovne_jedinice.sql).

Nekoliko stvari koje taj upit rešava, a lako se promaše: kolone su `CHAR` pa sve ide kroz
`RTRIM`; magacini se čitaju iz `dbo.MAGACINI` (u `MAGACIN_KONTAKT` su samo tri reda);
šifre se prefiksuju `MP`/`MAG` jer prodavnice i magacini dele brojeve (005 je i B.Palanka
i magacin e-faktura). Od 71 magacina samo tri su fizička mesta — ostali su knjigovodstveni
(INTERNI PRENOSI, IZDATA ROBA, REZERVACIJA…) i dobijaju `routable = 0`.

Prazna vrednost iz izvora **ne briše** ono što je operater ručno dopunio kroz dashboard
(npr. grad za Old Brick Pub, kog u `PRODAV_KONTAKT` nema).

```powershell
.\.venv\Scripts\sefsync.exe pj-sync                        # ili --deaktiviraj-nestale
```

Obavezne kolone su `code` i `name`; ostale su opcione. `kind` je jedno od
`maloprodaja`, `magacin`, `uprava`, `servis`, `ostalo` (nepoznata vrednost daje upozorenje,
ne prekida uvoz). Ponovno pokretanje ažurira postojeće po šifri, ne duplira.

**Iz CSV-a:**

```powershell
.\.venv\Scripts\sefsync.exe pj-import poslovne_jedinice.csv
```

```csv
code;name;kind;erp_code;address;city;emails;phones
MP01;Maloprodaja Centar;maloprodaja;01;Kneza Miloša 5;Beograd;mp01@elbraco.rs;+381641111111
MP02;Maloprodaja Pančevo;maloprodaja;02;Vojvode Mišića 12;Pančevo;mp02@elbraco.rs;
MAG1;Centralni magacin;magacin;90;Industrijska 1;Beograd;magacin@elbraco.rs;
```

### 2. Vidi šta dobavljači zaista šalju

Razvrstavanje zavisi od toga koja polja dobavljači popunjavaju u UBL-u, a to se razlikuje od
dobavljača do dobavljača. Zato prvo uzorak pa analiza:

```powershell
.\.venv\Scripts\sefsync.exe sample --dana 90 --limit 300
.\.venv\Scripts\sefsync.exe analyze --csv data\analiza.csv
```

Izveštaj pokazuje procenat popunjenosti svakog polja (adresa isporuke, broj narudžbenice,
referenca kupca, napomena…), najčešće vrednosti i presek po dobavljaču. CSV se otvara u Excel-u.

### 3. Neka aplikacija predloži pravila

```powershell
.\.venv\Scripts\sefsync.exe predlozi              # samo prikaz
.\.venv\Scripts\sefsync.exe predlozi --primeni    # upiši predloge kao pravila
.\.venv\Scripts\sefsync.exe proba                 # koliko uzorka sada prolazi automatski
```

`predlozi` uparuje adrese isporuke iz uzorka sa šifarnikom PJ, redom po pouzdanosti:
**adresa** (ulica i broj) → **PTT** → **naziv lokacije** → **grad**. Poklapanje ulice se
odbacuje ako se ni PTT ni grad ne slažu (ista ulica postoji u više mesta), a kad u istom
mestu ima više jedinica a adresa ne odgovara nijednoj — prijavljuje se **konflikt** i pravilo
se ne pravi. To je pitanje za čoveka, ne za heuristiku.

### 4. Dopuni pravila ručno

Na strani `/pravila`. Pravilo = *polje* + *operator* + *šablon* → *poslovna jedinica*,
sa prioritetom (manji broj = ranije). Poređenje ignoriše velika/mala slova, **ćirilicu i
dijakritiku** — pravilo `Vojvode Misica 12` hvata i `Војводе Мишића 12`.

Pravilo se može dodatno suziti na **jednog dobavljača** (PIB) i/ili **jednu vrstu dokumenta** —
npr. „sve od Eurotehne što je knjižno odobrenje → uprava“, dok fakture istog dobavljača idu
u objekat po adresi isporuke.

| Situacija | Pravilo |
|---|---|
| Dobavljač šalje adresu isporuke | `delivery_address` · contains · `VOJVODE MISICA 12` → MP02 |
| Objekat je u broju narudžbenice | `order_reference` · regex · `^MP0(\d)-` → odgovarajuća PJ |
| Dobavljač uvek isporučuje u magacin | `supplier_vat` · equals · `100200300` → MAG1 |
| Oznaka objekta u napomeni | `note` · contains · `PANCEVO` → MP02 |

Ako nijedno pravilo ne prođe, dokument ide u **nerazvrstano** i tamo ga operater ručno dodeljuje;
pri dodeli može odmah da se zapamti pravilo (checkbox „napravi pravilo“), pa sledeći put ide samo.
Ako firma ima samo jednu aktivnu PJ, sve ide na nju.

Jedinice označene sa `routable = 0` (troškovni centri „USLUGA“, magacin reklamacija, magacin
izlaznih e-faktura) **nikad ne bira automatika** — vide se u šifarniku i mogu se dodeliti ručno
ili biti cilj pravila, ali se ne uparuju po adresi.

### 5. Pokreni servis

```powershell
.\.venv\Scripts\sefsync.exe serve --worker      # dashboard + sinhronizacija u pozadini
```

Dashboard: `http://localhost:8080` (prijava iz `WEB_USERS`).

Alternativno razdvojeno:

```powershell
.\.venv\Scripts\sefsync.exe worker              # samo sinhronizacija
.\.venv\Scripts\sefsync.exe sync --dana 7       # jednokratno
```

---

## Komande

| Komanda | Šta radi |
|---|---|
| `sefsync check` | provera API ključa i okruženja |
| `sefsync sync [--od --do --dana]` | jedan ciklus preuzimanja i obrade |
| `sefsync worker` | neprekidno, na `POLL_INTERVAL_MINUTES` |
| `sefsync serve [--worker]` | web dashboard |
| `sefsync pregled --dana 30` | sažetak stanja na SEF-u (samo listanje, ne otvara dokumente) |
| `sefsync sample` / `analyze` | uzorak UBL-ova i analiza popunjenosti polja |
| `sefsync predlozi [--primeni]` | automatski predlog pravila iz uzorka |
| `sefsync proba` | proba razvrstavanja nad uzorkom, bez diranja SEF-a |
| `sefsync pj-import fajl.csv` | uvoz poslovnih jedinica iz CSV-a |
| `sefsync pj-sync` | šifarnik PJ iz postojeće tabele na serveru (`BU_SOURCE_SQL`) |
| `sefsync ko-moze` | ko može u panel i sa kojom ulogom (po šifarniku) |
| `sefsync arhiviraj --do <datum>` | sklanja istoriju iz reda (ostaje u arhivi) |
| `sefsync backfill --od <datum>` | preuzimanje dužeg perioda, mesec po mesec |
| `sefsync pravila` | ispis pravila razvrstavanja |
| `sefsync smtp-check [--na adresa]` | prikaz SMTP podešavanja i probna poruka |
| `sefsync notify-test <id>` | pravo obaveštenje za jedan dokument |
| `sefsync subscribe` | pretplata na SEF notifikacije o promenama statusa |
| `sefsync erp-check` / `erp-tables` | provera ERP baze (faza 2) |
| `sefsync kalkulacija <id>` | nacrt ulazne kalkulacije u CSV (faza 2) |

---

## E-mail

Podešavanja se čitaju iz `.env`, a ono što tu nije zadato dopunjava se iz
`SMTP_SOURCE_FILE` — `web.config` projekta ELBRACO LOGISTIKA, gde `Mail__Host`,
`Mail__User`, `Mail__Password` i `Mail__From` već postoje. Tako Gmail app password
ostaje u jednoj kopiji.

```powershell
.\.venv\Scripts\sefsync.exe smtp-check                          # šta je razrešeno i šta fali
.\.venv\Scripts\sefsync.exe smtp-check --na neko@elbraco.rs     # probna poruka
.\.venv\Scripts\sefsync.exe notify-test <id-dokumenta>          # pravo obaveštenje poslovnoj jedinici
```

Gmail traži **app password**, obična lozinka naloga se odbija sa `535 BadCredentials`.
Isto se dobija i kad je u `web.config`-u ostao placeholder umesto prave vrednosti —
`smtp-check` zato ispisuje odakle je šta uzeto.

---

## Prihvatanje na SEF-u

Prihvatanje je pravno relevantna radnja, pa je podrazumevano **ručno** (dugme u dashboardu,
zapisuje se ko je i kada prihvatio). Automatika se uključuje u `.env`:

- `AUTO_ACCEPT=routed` — automatski samo dokumenti koji su uspešno razvrstani na PJ,
- `AUTO_ACCEPT_DELAY_HOURS=24` — tek 24h posle prijema (prostor za reklamaciju),
- `AUTO_ACCEPT_MAX_AMOUNT=200000` — iznad tog iznosa uvek ručno.

Odbijanje uvek traži razlog (SEF ga zahteva).

---

## Instalacija kao Windows servis

Preporuka: [NSSM](https://nssm.cc/).

```powershell
nssm install ElbracoSEF "H:\CLAUDE PROJECTS\elbraco-sef\.venv\Scripts\sefsync.exe" serve --worker
nssm set ElbracoSEF AppDirectory "H:\CLAUDE PROJECTS\elbraco-sef"
nssm set ElbracoSEF AppStdout "H:\CLAUDE PROJECTS\elbraco-sef\data\service.log"
nssm start ElbracoSEF
```

Logovi idu i u `data/sefsync.log` (rotacija 5×5 MB).

---

## Struktura projekta

```
src/sefsync/
  config.py          podešavanja iz .env
  models.py          model podataka (dokumenti, stavke, PJ, pravila, obaveštenja, audit)
  sef/client.py      SEF Public API klijent (retry, prepoznavanje grešaka)
  ubl/parser.py      UBL 2.1 parser (srpski profil, podnosi omot i CreditNote)
  routing/engine.py  pravila razvrstavanja
  routing/analyzer.py analiza realnih UBL-ova
  routing/suggest.py automatsko predlaganje pravila (adresa → PTT → naziv → grad)
  notify/            e-mail (+ SMTP iz tuđeg web.config-a), push, sastavljanje poruke
  services/ingest.py glavni tok sinhronizacije
  services/acceptance.py prihvatanje/odbijanje sa zaštitama
  services/units.py  šifarnik poslovnih jedinica (CSV / tabela na serveru)
  web/               panel: prijava (SQL nalog), red operatera, ekran poslovođe
  services/workflow.py prosleđivanje, potvrda prijema, arhiva — i kuka za fazu 2
  erp/               konekcija na ERP (+ čitanje kredencijala iz user-secrets) i nacrt kalkulacije
tests/               141 test (parser, pravila, klijent, tok, web, kalkulacija)
docs/                zvanična SEF API dokumentacija
```

Testovi:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

---

## Faza 2 — ulazna kalkulacija u ERP-u

Već postoji: stavke se čitaju i čuvaju (`document_line`), tabela mapiranja artikala
(`item_mapping`), i `erp/calculation.py` koji od dokumenta pravi nacrt kalkulacije —
nabavna cena posle rabata, PDV po stavci, negativne količine za knjižno odobrenje,
šifra magacina iz poslovne jedinice.

Ostaje da se dogovori sa ERP-om:

1. tabele ulazne kalkulacije (zaglavlje + stavke) i način dodele broja dokumenta,
2. šifarnik artikala i pravilo mapiranja (barkod / šifra dobavljača / naša šifra na fakturi),
3. šifarnik magacina (`business_unit.erp_code`),
4. da li se piše direktno u bazu ili kroz ERP-ov import.

Do tada `sefsync kalkulacija <id>` daje CSV sa svim izračunatim vrednostima.
