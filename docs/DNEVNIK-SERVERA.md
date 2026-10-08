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

### 08.10.2026. — šta je ostalo otvoreno

* ✗ `mapiranje --upisi` (2.265 veza čeka)
* ✗ 180 pravila razvrstavanja
* ✗ `#8121` i `#8122` — obaveštenja koja treba poslati
* ✗ `MAG010` nema mejl, a ima dokumente
* ✗ em-crta u `BU_SOURCE_SQL` u serverskom `.env`-u (ime jedinice dvostruko
  kodirano; aplikacija to od 08.10. razmotava, ali izvor ostaje pokvaren)
* ✗ obaveštenje kod **storna** — propis traži, ne pokrivamo (tri slučaja)
* ? zašto je Systemsova `1386/26` (na SEF-u od 30.09.) u panel ušla 07.10. u
  12:57, a Bane tvrdi da je bila na Bogdanovom ekranu na 088. Komanda `dokument`
  je do 08.10. uzimala **prvi** zapis koji se poklopi i nije govorila da ih ima
  više — pa je moguće da postoje dva zapisa iste fakture. Proveriti sa
  `dvojnici`.
