# Dnevnik servera — šta je postavljeno i pokrenuto

Zašto postoji: Bane, 08.10.2026.: *„očekivao sam da vodiš ozbiljnu evidenciju o
urađenom, pogotovo što već 20+ dana radimo sve samo sa ove mašine“.* Imao je
pravo. Do sada su zapisi bili o **izmenama u kodu** ([FAZA-2.md](FAZA-2.md)) i o
**sadržaju paketa** ([PAKET.md](PAKET.md)), a nigde nije stajalo **šta je na
serveru stvarno postavljeno i pokrenuto, i kada**. Zbog toga se 08.10. nije
moglo odgovoriti na dva obična pitanja — „koliko pravila treba da ima“ i „je li
ovaj korak ikad pušten“.

Pravilo od sada: **svaki paket i svaka komanda puštena na serveru dobija red
ovde, isti dan.** Red nosi datum, šta je pušteno, i izmerene brojeve ako ih je
komanda dala. Ako nešto nije potvrđeno, piše da nije.

Oznake: ✔ potvrđeno ispisom koji je viđen · ? po prepisci, bez ispisa · ✗ nije
rađeno.

---

## Stanje servera, mereno 08.10.2026.

Izmereno komandama `baza`, `mapiranje` i `provera-prelaska`, ispisi viđeni:

| | na serveru | na razvojnoj | napomena |
|---|---|---|---|
| baza panela | `sqlite:///C:/efakture/data/sefsync.db` | isto | `DB_URL` nije zadat ni na jednom; za produkciju je predviđen MS SQL |
| dokumenata | 8.467 | 7.727 | najstariji u panelu od 11.09. na oba |
| **veza šifara** | **22** | 2.110 | izvođenje daje **2.265** pouzdanih — korak nije pušten, vidi niže |
| **pravila razvrstavanja** | **53** | 233 (231 aktivno) | **180 nije nikad otišlo na server** |
| poslovnih jedinica | 95 | 95 | |
| nacrta kalkulacija | 637 | 0 | razvojna ih ne računa |
| obaveštenja o prethodnom porezu | 84 | 0 | SEF zna za 1.352 |
| ERP nalog | `sefsync_erp` | `sefsync_dev` | razdvojeno 07.10. |

### Dve razlike koje su posledica nevođenja ovog dnevnika

**1. `mapiranje --upisi` nikad nije pušteno na serveru.** Komanda izvodi veze
„dobavljačeva šifra → naša“ iz ERP istorije. U panelu je 22 veze, izvođenje daje
2.265 pouzdanih. Posledica: 312 stavki je 08.10. čekalo šifru pod razlogom
„šifra dobavljača nije viđena“ — Dule je deset dana ručno povezivao ono što ERP
već zna. Korak je sada upisan u [PRELAZAK.md](PRELAZAK.md) kao **3a**.

**2. 180 pravila razvrstavanja nije na serveru.** Na razvojnoj ih je 233, na
serveru 53. Ta 53 su tačno ona koja su na razvojnoj ikad nešto odlučila, pa je
prva pretpostavka bila da ništa ne fali — **pogrešno**: ostalih 180 ima nula
pogodaka na razvojnoj zato što razvojna baza ne sadrži te dokumente (slika je od
11.09.). Među njima su i dva pravila za `DOO SYSTEMS` (PIB 101867722), čije su
fakture 08.10. stajale nerazvrstane:

* `#188 item_text contains 'TONER H W1106A' → OFFICE`
* `#256 attachment_text contains 'DOO SYSTEMS VETERNIK' → OFFICE`

Nepoznato i treba proveriti: **koji `pravila.json` je pušten na serveru i kada.**
Trenutni izvoz (`objava\pravila.json`) ima 233; na serveru stoji
`C:\efakture\pravila.json` i njegov sadržaj nije proveravan.

---

## 07.10.2026. oko 12:50 — panel je promenio bazu, i to niko nije primetio

**Ovo je uzrok gotovo svega što se 08.10. činilo kao niz nepovezanih kvarova.**

Izmereno:

| | MS SQL `SefSync` | SQLite `data\sefsync.db` |
|---|---|---|
| poslednji dokument | **07.10. 12:29** | 08.10., i dalje raste |
| poslednja sinhronizacija | **07.10. 12:44** (`sync_run` 2371) | traje |
| dokumenata | 8.435 | 8.468 |
| veza šifara | **2.176** | 22 (pre 08.10.) |
| pravila | **338** | 53 (pre 08.10.) |
| obaveštenja | 42 | 7 (pre 08.10.) |

Panel je do 07.10. 12:44 radio nad **MS SQL** bazom. Tada je prestao i u 12:54
počeo da puni **SQLite fajl koji je stajao pored, zaostao iz septembra**.
Petnaestominutni ciklus je iz SEF-a dopunio sve što je u njemu falilo — otuda
`document.created` za fakture izdate danima ranije.

`DB_URL` nema ni u `.env`-u, ni u bekapima (`^DB_URL=` ne postoji ni u jednom), ni
u okruženju (`Machine`, `User`, sesija — prazno), ni u zakazanom zadatku (pokreće
`sefsync serve --worker` sa `SEFSYNC_HOME=C:\efakture`). Dakle red je u nekom
trenutku **nestao iz `.env`-a**, a bekapi ne sežu pre 08.10. 09:13 pa se ne vidi
kada ni kako.

Posledice, sve objašnjene istim uzrokom:

* `1386/26` „nestala" sa Bogdanovog ekrana — u MS SQL bazi je na 088, gde ju je i
  video; u SQLite je nov zapis od 07.10., nerazvrstan. **Bogdan je bio u pravu.**
* 81 odobrenje „bez obaveštenja" — zapisi su u MS SQL bazi.
* Duletova gomila od 312 stavki — veze šifara su u MS SQL bazi (2.176).
* 53 pravila umesto 338.
* Nacrti „skoro prazni" u 14 h 07.10.

**Sve što sam 08.10. „popravljao" radio sam nad zaostalom bazom.** Nalazi o tome
šta „nikad nije pušteno na serveru" su netačni: `mapiranje --upisi` i
`pravila-uvoz` **jesu** bili pušteni — u pravoj bazi.

### 09.10.2026. — povratak na pravu bazu ✔

Nalog `sefsync_app` je postojao u `SefSync`; dobio je novu lozinku
(`ALTER LOGIN`), a `DB_URL` je vraćen u `C:\efakture\.env` **pored**
`ERP_DB_URL`-a. Prva prijava je pala (`18456`) dok lozinka nije usaglašena.

`sefsync baza` nad MS SQL-om: **8.435 dokumenata, 2.176 veza šifara, 338
pravila, 95 jedinica, 694 nacrta, 42 obaveštenja**, najnoviji dokument
07.10. 12:29 — tačno tamo gde je stala. Potvrđeno da je to prava baza.

Nova brava je proradila iz prve: pri pokretanju je ispisala
`BAZA PANELA JE PROMENJENA. Ranije: sqlite… Sada: mssql…`.

## Hronologija

### 28.09.2026. — prelazak na rad u aplikaciji ?

Po [PRELAZAK.md](PRELAZAK.md): postavljen paket, uvezena pravila, povučeni i
razvrstani dokumenti, preračunati nacrti, dodeljene dozvole za upis u
`ELBS_2026`, `NOVI_TOK_OD=2026-09-28`, ujutru uključen upis u produkciju i pošta
jedinicama. **Nijedan ispis sa servera nije sačuvan**, pa se ne zna šta je od
koraka stvarno odrađeno — uključujući `pravila-uvoz` i `mapiranje`.

### 29.09.–06.10.2026. — rad bez postavljanja ?

Izmene su se gomilale na razvojnoj mašini (vidi `git log`), server je radio
verziju od 28.09. [PAKET.md](PAKET.md) opisuje dvanaest izmena iz tog perioda.

### 06.10.2026. — disk ✔

`pregled-diska.ps1` pokazao je da je `C:\efakture` zauzimao 12,2 GB na disku sa
7,7 GB slobodnih od 140. Uvedeno arhiviranje UBL-ova starijih od 60 dana
(`arhiviraj-ubl`), uz proveru da se sa SEF-a mogu ponovo skinuti.

### 07.10.2026. — razdvojen ERP nalog ✔

* Izmereno šta `elbraco_dev_ro` sme: `db_datareader` na `ELBS_2022`–`2026` i
  `ELBSX_2026`, INSERT na osam tabela **i na `DARTIKLI`** (nju aplikacija
  namerno ne dira), a u test bazi mu je falio INSERT na `ARTIKLI`, `ARTPROD`,
  `NAZIVI`.
* Napravljen `sefsync_erp` (id 425 na `CLELBRACO`) i `sefsync_dev` (426, bez
  prava upisa u produkciju). `ERP_DB_URL` upisan u `C:\efakture\.env`, pa panel
  više ne pozajmljuje Logistikinu konekciju iz
  `C:\elbraco-logistika\web.config`. `provera-prelaska`: „Dozvole za upis u
  ELBS_2026 (nalog sefsync_erp): sve na broju“.
* `elbraco_dev_ro` **nije dirán** — koristi ga drugi projekat.
* Postavljen paket `f189bc3` (dvanaest izmena od 30.09. do 07.10.).

**Ispravka ranijeg nalaza:** tvrdio sam da `elbraco_dev_ro` ima pristup
Logistikinoj i ostalim bazama. Nema — `HAS_DBACCESS` = 0 za sve njih. `42S02`
koji sam video je „nema takvog objekta“ preko granice baze, ne uspešan ulaz.

### 08.10.2026. — obaveštenja o prethodnom porezu ✔

* Pročitana dokumentacija (poglavlje 1.89, strane 174–181) i zapisana u
  [OBAVESTENJE-PRETHODNI-POREZ.md](OBAVESTENJE-PRETHODNI-POREZ.md). Uzrok greške
  `400` stajao je u tabeli polja: period je **obavezan** kod
  `CreditNoteReferenceToPeriod`, a mi ga nismo slali.
* `uskladi-obavestenja`: SEF zna za **1.352** obaveštenja; **79 od 81** naših
  odobrenja ih već ima (poklopljeno po `PurchaseInvoiceId`), sva u statusu
  `Sent`. Bez obaveštenja ostala su dva: `#8121`, `#8122` (KIM-TEC). Zabeleženo
  79.
* Potvrda u radu: Comtrade `RS2604421` — prvo obaveštenje koje je aplikacija
  poslala sama, posle četiri koja su pala 02.10.
* Postavljani paketi tokom dana; zadnji potvrđen `08.10. 08:35`.

### 08.10.2026. — veze šifara iz ERP istorije puštene ✔

`mapiranje --upisi`, pa `nacrti --osvezi` (279 s), pa `nemapirane`. Ispisi viđeni:

* **upisano 2.245** veza, već postojalo 20 (ništa nije pregaženo);
* nacrti: `spreman 23`, `čeka šifru 60`, `problem 0`, `upisan 532`, `greška 0`;
* stavke koje čekaju šifru: **397 → 134**, a najveća grupa („nema barkod, šifra
  dobavljača nije viđena“) **312 → 98**.

**Traži proveru:** `upisan` je skočio sa 434 na 532. Devedeset osam kalkulacija
je time ili prepoznato kao već postojeće u ERP-u (razrešene veze omogućile su
poklapanje), ili ih je petnaestominutni ciklus dopisao (`dovrsi-upise`, najviše
20 po prolazu). Prvo je bezopasno, drugo je po dogovoru ali vredi pogledati
očima. `izvestaj --dana 1` kaže šta je aplikacija radila danas.

Šta je ostalo u redu, po uzroku: 98 stavki gde samo šifra dobavljača razlikuje
varijante (biciklovi `CSM PONYE LITIJUM` 24/26/28" — naziv na fakturi ne nosi
boju, pa čovek mora jednom po šifri); 17 servisnih redova bez EAN-a i bez šifre
(Smeće, Kanalizacija, Mesečno održavanje); 15 sa barkodom kog nema u šifarniku
(nova šifra, Duletov ekran); 4 koja nisu artikal. Po dobavljaču: CSM 24,
Inženjering Servis 22, Vitel 17, Infocar 11.

### 08.10.2026. — `izvestaj --dana 1`, provera posle upisa veza ✔

* `kalkulacija.upisana 65`, od toga **`dovrsi-upise` 12** — ostalo su ljudi. Skok
  `upisan` sa 434 na 537 je dakle najvećim delom **prepoznavanje kalkulacija koje
  su već u ERP-u**, ne masovno dopisivanje. Pitanje zatvoreno.
* **`obavestenje.poslato 7`** — ispravka zahteva ne radi slučajno jednom nego
  ponovljivo (uz `sef.accept 64`).
* **`document.vracen 18` i `routing.rule.miss 19` u jednom danu**, plus
  `routing.manual 37`. Pedeset pet ručnih intervencija u razvrstavanju dnevno —
  to je cena 180 pravila koja nisu na serveru.
* `document.created 723` u 24 h. Prosek od 11.09. je oko 310 dnevno, pa je ovo
  dvostruko, ali ne vanredno.

### 08.10.2026. — pravila razvrstavanja uvezena ✔

`pravila.json` iz paketa (datum 08.10. 13:14, 233 pravila) uvezen na serveru:

* **nova 187**, postojeća 44, ugašena 2 (duplikati), mapiranja nova 7.

Potvrda da fajl sve vreme dolazi: `postavi.ps1` kopira sve iz paketa u
`C:\efakture`, čuva samo `.env` i `data` — dakle `pravila.json` je bio tu posle
svakog postavljanja, ali `pravila-uvoz` nije puštan. Zato 53 umesto 231.

`razvrstaj`: **28 od 125** razvrstano, 97 čeka operatera. Od tih 97 devet
dobavljača nosi više od polovine: EXPORT-IMPORT (105504820) 11, ROA
(103285506) 8, Dunav osiguranje 7, FORMA PLUS 7, Radmilo Aleksić PR Sky 6, Mali
kućni aparati 4, CSM 2017 4, Linkom 4, Preduzeće za spoljnu i unutrašnju 4.

Dva od njih su **poznate odluke koje čekaju**, ne propust: FORMA PLUS čeka
pravilo po napomeni, Radmilo Aleksić PR Sky isto (Nina).

### 08.10.2026. — Optibox dao spisak objekata ✔

`96785 Optibox doo` (PIB 105504820) na upit je poslao kojoj prodavnici pripada
koja faktura, i obećao da će ubuduće stavljati oznaku **MPO** u napomenu. LANGOS
je obećao isto. Prevod u naše jedinice:

| faktura | njihov opis | jedinica |
|---|---|---|
| `RV-4008-26/1` | Subotica | **MP009** |
| `RV-4015-26/1` | Bačka Palanka | **MP005** |
| `RV-4016-26/1` | Sombor, Staparski put | **MP001** |
| `RV-4017-26/1` | Vrbas | **MP007** |
| `RV-4018-26/1` | Sombor, Vojvođanska | **MP008** |
| `RV-4019-26/1` | Sombor, Staparski put | **MP001** |
| `RV-4020-26/1` | Vrbas | **MP007** |
| `RV-4021-26/1` | Apatin | **MP002** |
| `RV-4022-26/1` | Odžaci | **MP003** |
| `RV-4023-26/1` | Kula | **MP004** |

**`RV-4045-26/1` nije na njihovom spisku**, a čeka u panelu — treba pitati.

Kad počne da stiže oznaka u napomeni, pravilo ide po polju `note` i ovo više
nikad nije ručni posao. **Dotle se ne pravi pravilo po dobavljaču** — Optiboxova
roba ide u svih devet objekata (69 kalkulacija u istoriji), pa bi pravilo po PIB-u
sve slalo na jedno mesto.

### 08.10.2026. — šta je ostalo otvoreno

* ✗ `#8121` i `#8122` — obaveštenja koja treba poslati
* ✗ `MAG010` nema mejl, a ima dokumente
* ✗ em-crta u `BU_SOURCE_SQL` u serverskom `.env`-u (ime jedinice dvostruko
  kodirano; aplikacija to od 08.10. razmotava, ali izvor ostaje pokvaren)
* ✗ **panel radi na SQLite.** Nije greška — `.env.server` je 21.09. to izričito
  odlučio („jedan operater, nekoliko hiljada dokumenata godišnje“) i nijedan
  podatak nije izgubljen. Ali pretpostavka je pala: 8.467 dokumenata u 27 dana,
  95 jedinica, nekoliko ljudi istovremeno, IIS i ciklus. Uputstvo:
  [PREBACI-NA-MSSQL.md](PREBACI-NA-MSSQL.md). Da se ne ponovi: `.env.server` sada
  traži `DB_URL`, `provera-prelaska` to prijavljuje kao grešku kad je upis u
  produkciju uključen, i panel piše red u log pri svakom pokretanju.
* ✗ obaveštenje kod **storna** — propis traži, ne pokrivamo (tri slučaja)
* ? zašto je Systemsova `1386/26` (na SEF-u od 30.09.) u panel ušla 07.10. u
  12:57, a Bane tvrdi da je bila na Bogdanovom ekranu na 088. Komanda `dokument`
  je do 08.10. uzimala **prvi** zapis koji se poklopi i nije govorila da ih ima
  više — pa je moguće da postoje dva zapisa iste fakture. Proveriti sa
  `dvojnici`.
