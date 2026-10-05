> **Šta je u paketu koji čeka postavljanje:** [PAKET.md](PAKET.md) — deset
> izmena od 30.09. do 03.10., sa razlogom i sa onim što treba pogledati posle
> postavljanja. Ovaj dnevnik je duži i ide hronološki.

# Faza 2 — ulazna kalkulacija iz e-fakture

Nalazi merenja na stvarnim podacima (SEF baza panela + ERP `ELBS_2026`),
stanje 14.09.2026. Sve brojke su izmerene, nijedna nije procenjena.

---

## 1. Kako prepoznati artikal sa dobavljačeve fakture

Mereno na 28.310 stavki ulaznih faktura.

| Identifikator u UBL-u | Pokrivenost |
|---|---:|
| `SellersItemIdentification` (šifra dobavljača) | **94,9 %** |
| `StandardItemIdentification` (EAN) | 65,9 % |
| `BuyersItemIdentification` (navodno naša šifra) | 7,2 % |
| Naziv | 100 % |

**`BuyersItemIdentification` je neupotrebljiv.** Od 2.029 stavki koje ga imaju,
2.008 su banke (`BNKTR:` — šifra naknade za POS terminale), a preostalih 12 je
ALSO Serbia gde je polje doslovna kopija njihove sopstvene šifre. Nijedna od tih
9 vrednosti ne postoji u `ARTIKLI.ARTIKAL`.

**Zaključak:** ključ mapiranja je `(PIB dobavljača + njegova šifra) → naša SKU`,
ne EAN. Šifru dobavljača on ne menja kad promeni tržište ili pakovanje, pa
promena EAN-a za isti artikal ne ruši vezu.

### Barkod u ERP-u

`ARTIKLI` ima **jedno** polje `BARCODE` po šifri. `DARTIKLI.BARCODE` je kopija —
od 43.458 uparenih artikala 43.384 imaju identičnu vrednost, a 74 razlike su
greške u unosu (vodeće nule, prefiks magacina, prebačene cifre), ne varijante za
druga tržišta.

**Posledica:** više EAN-ova po jednoj SKU mora da se čuva u našoj bazi. App ne
upisuje nove EAN-ove u ERP jer bi pregazila postojeći.

### Model u nazivu — mereno, odbačeno kao automatika

Traženje dobavljačeve šifre kao podniza u `ARTIKLI.NAZIV` daje **+101 par
(+3 %)**. Pogađa kod Vox, Elica, Hansa (naš naziv je „marka + model"), ali pravi
i lažne pogotke — npr. CALIX šifra `EL001` → `Beper P102GEL001`, a na fakturi je
pivo. **Koristiti samo kao predlog operateru, nikad automatski.**

---

## 2. Učenje iz zaknjiženih kalkulacija

ERP sadrži kalkulacije za isti period. To su ljudskom rukom potvrđeni odgovori.

### Most SEF ↔ ERP

```
SEF faktura --BROJ_RN_DOB--> ULKALKUL / MKALKUL --> PRULKALK / MPRULK
(šifre dobavljača)              (zaglavlje)          (NAŠE šifre)
```

- **7.223 od 7.727 dokumenata (93 %)** nalazi svoju kalkulaciju po broju fakture.
- Poređenje ide na normalizovan oblik: izbaci sve osim slova i cifara, skini
  vodeće nule. `FA-5543-03/26` == `FA55430326`.
- Veza dobavljača je **matični broj** (`NAZIVI.MATBROJ` ↔ `supplier_reg_no`),
  ne PIB — `NAZIVI.BPG` je prazno.
- `ULKALKUL.DATUM` je Clarion serijski dan: `1800-12-28 + N dana`.
  82184 = 2026-01-01.

### Parovanje stavaka

Unutar sparenog dokumenta, stavke se vezuju po **količini + fakturnoj ceni**.
Rezultat: 13.776 sparenih stavki, 7.335 nesparenih (verovatno rabat).

Izvedeno **2.323 mapiranja**:

| | |
|---|---:|
| sigurnih (isti odgovor iz 2+ faktura) | 1.658 |
| viđeno jednom (za potvrdu operatera) | 621 |
| kolebljivih (više odgovora — odbaciti) | 44 (1,9 %) |

### Unakrsna provera — ključni dokaz

Na **1.756 parova koje rešavaju i EAN i kalkulacije nezavisno — slažu se u
1.751. 99,7 %.** Neslaganja: 5.

Dva različita puta (barkod iz šifarnika / ono što je čovek zaknjižio) daju isti
odgovor. Gde se slažu, mapiranje se upisuje bez ljudske provere.

### Pokrivenost

Uslužni dobavljači (oni čiji dokumenti uvek idu na magacin 088) isključeni iz
imenioca — oni nikad neće imati artikal.

| | Parova | % |
|---|---:|---:|
| Parova robe ukupno | 3.013 | |
| rešava EAN | 2.129 | 71 % |
| rešavaju kalkulacije | 2.279 | 76 % |
| **zajedno** | **2.652** | **88 %** |
| **ostaje za ruke** | **361** | 12 % |

### Kriva ručnog posla

Novi parovi (dobavljač + šifra) po mesecu, od nule:

| Mesec | Stavki | Novih parova | Traži čoveka |
|---|---:|---:|---:|
| 01 | 3.278 | 1.068 | 33 % |
| 02 | 3.549 | 587 | 17 % |
| 03 | 4.191 | 489 | 12 % |
| 05 | 3.008 | 315 | 10 % |
| 07 | 3.401 | 306 | 9 % |
| 08 | 2.282 | 144 | **6 %** |

Sa 33 % na 6 % za osam meseci — i to bez EAN-a, koji reši 57 % i tih novih.
Stvarni ručni posao u avgustu: oko 60 artikala za ceo mesec.

**Zato: mapiranje se puni iz istorije PRE puštanja u rad.**

---

## 3. Knjižna odobrenja

967 od 7.727 dokumenata (12,5 %), oko 3–4 dnevno.
Sparenih sa kalkulacijom: 904 — **555 na magacin 010 (reklamacije), 349 na 088.**

Testirani signali:

| Signal | Rezultat |
|---|---|
| „ima stavke" | **beskorisno** — svih 967 ima stavke |
| poznat EAN među stavkama | 55 % kod 010 naspram 7 % kod 088 — ne deli |
| dobavljač | **ne radi** — 876 od 904 dokumenta od mešanih dobavljača |
| ključne reči | 501 odluka, **98 % tačno** |
| `BillingReference` == 1 | 234 odluke, 85 % tačno |

Obe grupe nabrajaju robu. Razlika nije „ima li robe" nego **povraćaj naspram
finansijskog odobrenja**: 010 govori `SAGLASNOST BROJ`, `OTPIS NEISPRAVNE ROBE`;
088 govori `RABAT`, `PROMET`, `BONUS`. Povraćaj se veže za jednu fakturu
(`BillingReference` = 1), rabat na promet ili nema vezu ili nabraja više.

### Predlog: dva nivoa pouzdanosti

1. **ključna reč → app razvrsta sama** (98 %)
2. **veza ka fakturi → app predloži, operater potvrdi** (85 %)
3. ostalo → nerazvrstano

Zajedno: 735 od 904 odlučeno (81 %) sa 94 % tačnosti; 169 + 234 ide operateru,
oko 1,5 dnevno.

### Za ispraviti u fazi 1

- obrisati catch-all pravilo `CreditNote → OFFICE` — **555 od 904 je pripadalo
  magacinu 010**, preko 60 % pogrešnih
- `billing_references` čuvati u bazi (parser ga već čita, ingest ga baca)

---

## 4. Struktura ERP-a

### Odredište po vrsti dokumenta

| Odredište | Zaglavlje | Stavke | `MAGACIN` |
|---|---|---|---|
| VP kalkulacija | `ULKALKUL` | `PRULKALK` | šifra magacina |
| MP kalkulacija | `MKALKUL` | `MPRULK` | šifra prodavnice |
| Ulazni račun (usluge) | `ULKALKUL` | — | `088` |
| Reklamacija / povraćaj | `ULKALKUL` | `PRULKALK` | `010` |

Ovo je **ista odluka koju faza 1 već donosi** — razvrstavanje na poslovnu
jedinicu bira i tabelu.

### Konvencije u zaglavlju (mereno)

| Polje | Vrednost |
|---|---|
| `VRSTAKNJIZ` | uvek `'U'` |
| `KONTO` | uvek `'435'` |
| `MODEL` | uvek `0` |
| `OZNAKA` | uvek `'EUR'` |
| `BROJ` | brojač po magacinu, 5 cifara (`00001`…) |
| `STAVKE` | `'Da'` kad ima artikala; `''` za 088 |
| `MENJA_CENU` | u `MKALKUL` uvek `'Da'` |
| `PROKNJIZENO` | `0` = nije zaknjiženo, `1` = zaknjiženo, `2` = zaključano (admin) |

### Stavke

- `RBR` = Clarion vreme + **korisničko ime** (`8219653534078121dane`) —
  svaka stavka nosi ko ju je uneo
- `KOLICINA` negativna kod povraćaja (`-1`)
- `MALACENA` == `ARTPROD.CENAMALO` u **80 %** slučajeva → app nudi tekuću
  maloprodajnu cenu objekta, marža (`RUCM_PROC`) se izvodi unazad; onih 20 %
  su promene cena koje poslovođa unosi
- marža nije konstanta (51,8 % / 33,5 % / 36,8 % na uzastopnim stavkama)

### Broj računa dobavljača

`BROJ_RN_DOB` je `varchar(20)`. **Ispravka merenjem 15.09.**: broj se NE skraćuje po
pravilu — od 4.652 broja nijedan nije dužine 20, a 4.185 zadržava crtice i kose crte.
Znači prepisuje se kakav jeste; izbacivanje znakova je izuzetak kad ne stane.

### Otvaranje nove šifre

Tri tabele, sve obavezne:

1. `ARTIKLI` sa `SIFRAMAG='099'` — matična, tu nastaje šifra (43.486 šifara)
2. `ARTIKLI` ostali magacini + `ARTPROD` — kopije po objektima
3. `DARTIKLI` — dopuna (opis, slike, uvoznik, primarni dobavljač, energetski
   razred); 43.466 redova, tačno jedan po artiklu

**Odluka: srednja opcija** — app predloži kompletan artikal, čovek klikne
„otvori". Šifarnik od 43.474 SKU se ne puni bez nadzora.

Detaljan redosled i koja se polja gde pune — nije još razrađeno.

---

## 5. Otvorena pitanja

1. **Upis u živi ERP.** Nema nijedne procedure za upis — sve ide direktnim
   `INSERT`-om. Postoji li kopija baze za probu? Predlog: režim u kom app ispiše
   `INSERT` a ne izvrši ga, pa se uporedi sa ručno napravljenom kalkulacijom za
   isti dokument.
2. **`PROKNJIZENO`** — kojom vrednošću app kreira kalkulaciju da je poslovođa
   vidi kao spremnu za knjiženje: `1` ili `0`?
3. **`BROJ`** — app bi uzimala `MAX(BROJ)+1` po magacinu. Šta ako poslovođa u
   istom trenutku otvara kalkulaciju u ERP-u?
4. **`RBR`** — tačno pravilo građenja, i pod kojim imenom app piše (svojim ili
   imenom operatera).
5. **Rabat** — u `FAK_CENA` ide cena pre rabata (pa rabat u `RABAT_PROC`) ili
   neto cena? Verovatno objašnjava 7.335 nesparenih stavki.
6. **Ostali tipovi** — 16 `DebitNote` i 39 avansa: imaju li svoju kalkulaciju
   ili idu računovodstvu mimo ovoga?

---

## 6. Šta još vredi izmeriti

- **Skraćeni brojevi računa.** 504 dokumenta (7 %) nije našlo kalkulaciju —
  verovatno zato što broj nije stao u `varchar(20)`. Dodati poređenje po početku
  ključa (min. 10 znakova) i izmeriti koliko vrati i koliko lažnih parova napravi.
- **Nesparene stavke.** 7.335 komada — proveriti hipotezu o rabatu.
- **Unazadna provera faze 1.** Za 5.303 sparena dokumenta znamo u koji je magacin
  kalkulacija stvarno otišla. To je nezavisna istina o poslovnoj jedinici, pa se
  tačnost razvrstavanja može izmeriti na hiljadama slučajeva umesto na uzorku.


---

# Dodatak: nalazi od 15.09.2026

## Test baza

`ELBSX_2026` — kopija produkcije od 05.08.2026. Upis kalkulacije ide podrazumevano
tamo; produkcija traži `ERP_ALLOW_PRODUCTION_WRITE=true` (`erp_engine_za_upis`).

## Generator kalkulacije radi

`erp/kalkulacija.py` gradi zaglavlje i stavke iz e-fakture. Provereno naspram
stvarnih kalkulacija koje su ljudi uneli: zaglavlje i svi izvedeni iznosi (marža,
PDV u MP ceni, MP vrednost, rok plaćanja) poklapaju se u paru.

Odgovori koje su dali podaci, bez pitanja:

| Polje | Vrednost |
|---|---|
| `PROKNJIZENO` | **`0` = nije zaknjiženo**, `1` = zaknjiženo, `2` = administrator zaključao stare |
| `RBR` | `clarion_dan(5) + vreme u stotinkama(7) + 4 cifre + korisničko ime(10)` |
| `IZVOREPP` | `'Elektronske fakture'` — 4.612 od 4.652 |
| `LASTUSER` | `1` — 4.631 od 4.652 |
| `VALUTA` | rok plaćanja kao Clarion dan (+45 dana najčešće) |
| `TARIFA` | `ARTIKLI.TARIFA` dopunjeno nulama na 5 → `PTARIFE.STOPA` |
| `SINTPOR` | stopa 20 → `'270'`, 10 → `'271'` |

### Rabat — rešeno

Mereno na 4.425 stavki sa rabatom:

```
FAK_CENA    = bruto cena (pre rabata)
RABAT_PROC  = procenat
IZN_RABATA  = rabat PO KOMADU (ne po stavci)
NAB_CENA    = neto cena
VREDNOST    = neto x količina
```

Marža (`RUCM_PROC`) se računa od **neto** nabavne cene.

## Razvrstavanje: tačnost izmerena naspram ERP-a

Kalkulacije daju nezavisnu istinu o tome kojoj jedinici dokument pripada —
4.862 dokumenta na početku, 6.136 posle dopune podataka.

| | Tačnost | Pokrivenost | Grešaka |
|---|---:|---:|---:|
| Zatečeno stanje | 70,2 % | 79,2 % | 1.449 |
| Posle gašenja #44/#47 | 96,5 % | 55,3 % | 119 |
| **+ 144 pravila iz istorije** | **97,4 %** | **78,5 %** | **124** |

### Dva pravila su pravila 93 % svih grešaka

- **#47** `delivery_address contains 'Srpskih vladara 46'` → MP002. To je **adresa
  sedišta firme**, ne odredišta. Tačnost **23,8 %** na 1.321 dokumentu. Nastalo je
  iz automatskog predloga na uzorku od **39 dokumenata** — pouka: predlozi pravila
  traže proveru na celom skupu, ne na uzorku.
- **#44** `any_text regex '.'` → OFFICE za sva knjižna odobrenja. Tačnost **48,0 %**;
  334 grešaka su `088 → 010`, tj. reklamacije.

Oba su ugašena (ne obrisana — `active=False`, trag ostaje). Zamenjena sa 12 pravila
po ključnim rečima za knjižna odobrenja (redosled: 088 pre 010, mereno bolje).

### Svaki dobavljač piše odredište na svom mestu

Ovo je ključni nalaz. Traženje po jednom polju ne radi:

| Dobavljač | Gde piše | Primer |
|---|---|---|
| EWE COMP | `cbc:Note` | `KULA Po otpremnici:26-30C-088989` |
| BSH | `OrderReference/ID` | `bpalanka`, `Becej` |
| KIM-TEC | `AdditionalDocumentReference/ID` | `Elbraco - Sombor` |
| CANDY HOOVER | samo u PDF prilogu | — |

Parser je dopunjen: `additional_references` (popunjeno na **4.603 dokumenta, 60 %**)
i napomene po stavkama ulaze u `item_text`. Dodato polje `MatchField.ADDITIONAL_REFERENCE`.

### 144 pravila izvedena iz istorije

Rudarenje po dobavljaču kroz sva tekstualna polja, prag: najmanje 5 dokumenata i
97 % čistoće, izraz mora sadržati slovo (poštanski brojevi su krhki). Pohlepan izbor
najmanjeg skupa koji pokriva najviše.

## Ostaje za dobavljače: 1.321 dokument (22 %)

| Dobavljač | PIB | Ne zna | Napomena |
|---|---|---:|---|
| CANDY HOOVER | 104211304 | 217 | podatak samo u PDF prilogu |
| CSM 2017 | 110130228 | 193 | |
| EWE COMP | 100042618 | 172 | (bilo 908 pre pravila) |
| FORMA PLUS | 101717578 | 67 | |
| BSH | 107351003 | 57 | (bilo 324) |
| SKY (Radmilo Aleksić) | 103699024 | 47 | |
| ROAMING | 103285506 | 41 | |
| KIM-TEC | 103574264 | 39 | (bilo 75) |
| ENERGY NET | 101644366 | 38 | |

Preostale greške (124) su sitne i grupisane: LANGOS (usluge knjižene po objektima)
i SAT-TRAKT.

## Sledeće

1. Čitanje PDF priloga — za CANDY i slične (nijedna PDF biblioteka nije instalirana)
2. `billing_references` u bazu (parser ih čita, ingest baca)
3. Punjenje `item_mapping` iz istorije (2.652 para)
4. Skraćeni brojevi računa — 504 dokumenta bez para
5. Odgovori na preostala pitanja: `BROJ` pri istovremenom radu, ime pod kojim app piše


---

## Probni upis u ELBSX_2026 — 15.09.2026

Kalkulacija `MKALKUL 005/U/00450` upisana iz dokumenta #7714 (3 stavke Gorenje,
dobavljač 00142, račun 3184VP-2026).

**Svi iznosi se poklapaju sa onim što ERP sam prikaže:**

| | App | ERP ekran |
|---|---:|---:|
| Za naplatu | 155.584,61 | 155.584,61 |
| Rabat | 15.975,16 | 15.975,16 |
| Nabavna vrednost | 129.653,84 | 129.653,84 |
| PDV 20 % | 25.930,77 | 25.930,77 |
| RUC iznos | 52.012,83 | 52.012,83 |
| Prodajna vrednost | 218.000,00 | 218.000,00 |

### Greška uhvaćena na ekranu: PROKNJIZENO

App je prvi put upisala `PROKNJIZENO=1`, što je ERP prikazao kao **zaknjiženu**
kalkulaciju. To je ozbiljna greška: knjiženje u ERP-u ne menja samo kalkulaciju
nego nastavlja u **glavnu knjigu** i ostale evidencije. Kalkulacija označena kao
zaknjižena izgleda gotovo, a ništa se nije desilo — roba nije zadužena i
poslovođa nema šta da klikne.

Značenje (potvrdio Bane):

| Vrednost | Značenje |
|---|---|
| **`0`** | **nije zaknjiženo — jedino što app sme da upiše** |
| `1` | zaknjiženo u ERP-u |
| `2` | administrator zaključao stare kalkulacije |

Zaštita u kodu: imenovane konstante, `upisi()` odbija svaki nacrt koji nije
`PROKNJIZENO_NIJE`, dva testa to zaključavaju.

### Prava na bazi

Nalog `elbraco_dev_ro` je bio read-only (zato je prvi upis odbijen). Pravo
`INSERT/UPDATE/DELETE` dodeljeno **samo na tabele kalkulacija i samo u
`ELBSX_2026`**. `ARTIKLI`, `ARTPROD` i `DARTIKLI` namerno izostavljeni —
otvaranje šifara još nije razrađeno. Produkcija ostaje read-only.

### Pravilo rada

Nijedno pravilo razvrstavanja i nijedna izmena generatora ne ide u rad dok se ne
izmeri na svim dokumentima sa poznatim odgovorom iz ERP-a. Ovo je 15.09. uhvatilo
tri greške koje bi inače prošle: pravilo #47, veličinu slova u rudarenju, i
prilog u `any_text`.


---

## Mapiranje artikala napunjeno iz istorije — 15.09.2026

`services/mapiranje.py` izvodi veze iz dva nezavisna izvora i upisuje samo ono
u šta se može verovati.

| | |
|---|---:|
| Parova ukupno | 3.745 |
| Iz barkoda | 2.129 |
| Iz zaknjiženih kalkulacija | 2.288 |
| Oba izvora se slažu | **1.759** |
| Oba se NE slažu | 6 |
| Kolebljivih (odbačeno) | 45 |
| **Upisano kao pouzdano** | **2.110** |

Za operatera ostaje 587: 536 viđenih samo jednom, 45 kolebljivih, 6 nesaglasnih.
Onih 6 nesaglasnih vredi pogledati posebno — to su mesta gde barkod vodi na jedan
artikal a čovek je zaknjižio drugi, dakle verovatna greška u podacima.

Efekat: od 357 stavki na 120 dokumenata, **327 se rešava iz mapiranja**, 30 preko
barkoda.

## Dobavljač u ERP-u: ključ je PIB, a stoji u koloni ZIRORACUN

Traženje po matičnom broju nalazi samo 66 % dobavljača:

- `NAZIVI.MATBROJ` je popunjen na 3.351 od 44.949 redova
- kod javnih preduzeća SEF šalje drugi identifikator (Srbijagas: `86132`
  umesto `20084600`)

Kolona **`ZIRORACUN` uprkos imenu sadrži PIB** (Banca Intesa: `100001159`),
popunjena je na 37.411 redova i diže pokrivenost na **97 %**. Preostalih 3 % su
dobavljači koji stvarno još nisu otvoreni u šifarniku.

Uz to je dodato `Document.payment_account` — stvarni žiro račun sa fakture, koji
kalkulaciji ionako treba.

## Faktura bez PIB-a i matičnog broja je neispravna

Pravilo (Bane, 15.09.): takav dokument **ne sme dalje od operatera** i mora biti
označen. Bez oba identifikatora dobavljač se ne može prepoznati ni u šifarniku,
ni u mapiranju artikala, ni u poreskoj evidenciji.

Ugrađeno: `Document.bez_identifikacije`, `Workflow.forward()` odbija takav
dokument, crvena oznaka u redu i objašnjenje na strani dokumenta, test.

U dosadašnjih 7.727 dokumenata nema nijednog takvog — pravilo je postavljeno
unapred.


---

## Veza ka ispravljenoj fakturi u bazi — 16.09.2026

Parser je `BillingReference` čitao od početka, a ingest ga je bacao. Sada se čuva:

| Kolona | Sadržaj |
|---|---|
| `Document.billing_reference` | brojevi faktura koje dokument ispravlja, spojeni sa ` \| ` |
| `Document.billing_reference_count` | koliko ih je — broji se pre skraćivanja teksta na 500 |

Broj veza je drugi nivo pouzdanosti iz odeljka 3: **tačno jedna** veza znači povraćaj
po jednoj fakturi (magacin 010), **nijedna ili više** finansijsko odobrenje na promet
(088). Mereno ranije: 234 odluke, 85 % tačnih. Operater vidi broj na strani dokumenta,
a uz više veza i objašnjenje šta to znači.

Dodato je i polje `MatchField.BILLING_REFERENCE` da pravilo može da ga cilja.

### Namerno NIJE ušlo u `any_text`

Broj tuđe fakture ne može da imenuje odredište — može samo da unese nove cifre u koje
bi se uhvatila postojeća `any_text` pravila. Ista greška koju je napravio prilog
(tačnost 97,4 % → 91,8 %). Polje radi samo kad ga pravilo izričito cilja; dva testa
to zaključavaju.

**Zatečena nedoslednost, nije dirana:** kod operatora `regex` nad `any_text` prilog
i dalje ulazi u tekst, za razliku od ostalih operatora. Ispravka bi promenila odluke
postojećih regex pravila, pa čeka merenje naspram ERP-a.

### `osvezi-ubl` — dopuna istorije bez SEF-a

Nova kolona ostaje prazna za 7.727 već preuzetih dokumenata. Fajlovi su na disku, pa
`sefsync osvezi-ubl` čita njih, bez ijednog poziva ka SEF-u. Isto vredi za svaku
raniju dopunu parsera (`additional_reference`, `attachment_text`, `payment_account`).

Stavke se grade iznova, ali se `erp_item_code` vraća na svoju stavku po rednom broju —
ta šifra nije iz UBL-a i ponovno čitanje je ne sme pojesti. Razvrstavanje se ne dira;
za to postoji `razvrstaj`, posle merenja.

**Pre puštanja:** `sefsync init-db` dodaje dve kolone (`sync_schema`), pa tek onda
`osvezi-ubl`.

### Ostaje

Pravila po broju veza (010/088) još nisu napisana — po pravilu rada od 15.09. idu tek
kad se izmere na svih 904 sparena odobrenja sa poznatim odgovorom iz ERP-a. Sada za to
postoji podatak u bazi.


---

## Veza ka fakturi: signal izmeren na svim odobrenjima — 16.09.2026

Mereno na **923 od 967** knjižnih odobrenja koja nalaze svoju kalkulaciju u ERP-u
(nespareno 44, dvosmislenih 0). Istina je magacin u koji je kalkulacija stvarno
otišla. Alat: `alati/meri_odobrenja.py`.

### Prvo merenje je bilo pogrešno — i kako se to videlo

Prvi prolaz je rekao da pravila razvrstavaju 53 % odobrenja i da je „jedna veza"
bacanje novčića. Greška je bila u merilu, ne u podacima: poslovna jedinica
**UPRAVA nema `erp_code`**, pa je svaka odluka pravila za rabat/promet ispala kao
„pravila ćute". Skoro polovina odluka je brojana kao neodlučeno.

Provera pretpostavke posle ispravke — kuda pravila šalju naspram toga gde je
dokument stvarno završio:

| Pravila kažu | Stvarno | Dokumenata |
|---|---|---:|
| MAG010 | 010 | 444 |
| OFFICE | 088 | 331 |
| OFFICE | 010 | 16 |
| MAG010 | 088 | 6 |

`OFFICE ≡ 088` drži u 95,4 % slučajeva, `MAG010 ≡ 010` u 98,7 %. Tek sa tim
merilom brojke znače nešto.

### Stvarni magacin po broju veza

| Veza ka fakturi | 010 | 088 | ukupno |
|---|---:|---:|---:|
| nema | 221 | 280 | 501 |
| tačno jedna | 346 | 57 | 403 |
| dve ili više | 1 | 18 | 19 |

Ukupnih 85,9 % za „jedna veza → 010" i dalje stoji, ali ta brojka ne znači ništa
sama za sebe: pravila po ključnim rečima već rešavaju većinu tih dokumenata.

### Šta veza dodaje tamo gde pravila ćute

Zatečeno stanje na ovih 923: odlučeno **833 (90,2 %)**, tačnost **93,0 %**, 58
grešaka, neodlučenih **90**. Od tih 90, čak **72 pripadaju magacinu 010** — dakle
ono što ostaje nerazvrstano nisu rabati nego povraćaji.

| Veza | Neodlučenih | Stvarno 010 | Stvarno 088 | „010" bi bilo tačno |
|---|---:|---:|---:|---:|
| nema | 31 | 25 | 6 | 80,6 % |
| **tačno jedna** | **51** | **46** | 5 | **90,2 %** |
| dve ili više | 8 | 1 | 7 | 12,5 % (dakle 088 u 87,5 %) |

**Predlog iz odeljka 3 se potvrđuje**, ali tek na pravom skupu: veza ka jednoj
fakturi znači povraćaj, i to tamo gde ostala pravila nemaju šta da kažu.

Dodatna potvrda: od 58 dokumenata koje današnja pravila pogrešno razvrstaju, 39 ima
tačno jednu vezu i 36 od njih stvarno pripada 010.

### Šta bi koje pravilo donelo

| | Odlučeno | Tačnost | Grešaka | Neodlučeno |
|---|---:|---:|---:|---:|
| Zatečeno | 833 (90,2 %) | 93,0 % | 58 | 90 |
| + „jedna veza → 010" | 884 (95,8 %) | 92,9 % | 63 | 39 |
| + „dve ili više → 088" | 892 (96,6 %) | 92,9 % | 64 | 31 |
| + „nema veze → 010" | 923 (100 %) | 92,5 % | 69 | 0 |

Prva dva pravila dižu pokrivenost za 6,4 procentna poena uz nepromenjenu tačnost.
Treće (80,6 %) je slabo i pitanje je za čoveka, ne za merenje.

### Pouka o merenju

Signal se ne meri na celom skupu nego **na onome što ostaje posle svih pravila
koja već rade**. Ista zamka je bila u pravilu #47: izgledalo je dobro jer je
mereno tamo gde su i druga pravila pogađala. I merilo se proverava pre nalaza —
`OFFICE` bez `erp_code` je tiho pretvorio 347 odluka u „neodlučeno".

### Upisana dva pravila — provereno merenjem posle upisa

| | Odlučeno | Tačnost | Grešaka | Neodlučeno |
|---|---:|---:|---:|---:|
| Pre | 833 (90,2 %) | 93,0 % | 58 | 90 |
| **Posle** | **892 (96,6 %)** | **92,8 %** | 64 | **31** |

Pravila: `billing_reference_kind == 'jedna'` → MAG010 i `== 'vise'` → OFFICE, oba
samo za `CreditNote`. Novo polje `MatchField.BILLING_REFERENCE_KIND` daje pravilima
podelu „nema / jedna / više" — broj sam po sebi pravilu ne znači ništa. Polje ne
ulazi u `any_text`.

### Dve greške uhvaćene tek ponovnim merenjem

**Prioritet.** Pravila su prvo upisana sa prioritetom 50, a rudarena pravila po
dobavljaču stoje na 60 — novo pravilo im je **preotelo** dokumente koje su ona
rešavala tačno. Tačnost je pala na 90,8 % i grešaka je bilo 82 umesto 64. Signal
je meren na onome što ostaje posle **svih** pravila, pa i pravilo mora da ide
poslednje: prioritet 90.

**Spisak polja na dva mesta.** Prvo merenje posle upisa pokazalo je da se ništa
nije promenilo — jer je `alati/meri_odobrenja.py` imao svoju kopiju spiska polja,
bez novog. Pravilo se prosto nije okidalo. Spisak je sada na jednom mestu:
`Document.routing_fields()`, koji koriste i ingest i merenje.


---

## Zašto dokument ne nađe svoju kalkulaciju — 16.09.2026

Mereno na svih 7.727 dokumenata (`alati/meri_uparivanje.py`). Tačno se upari
**7.322 (94,8 %)**, ostaje **405 (5,2 %)** — ne 504, ta brojka je bila sa starijeg
skupa.

### Svih 405 razvrstano po uzroku

| | Uzrok |
|---:|---|
| 131 | neobjašnjeno |
| 105 | januar — kalkulacija verovatno u bazi `ELBS_2025` |
| 65 | dobavljač nema nijednu kalkulaciju (banke, usluge) |
| 48 | našao bi se po početku broja (skraćen u ERP-u) |
| 39 | avans — kalkulacije ni nema |
| 17 | sveža faktura — još nije zaknjižena |

**ERP drži bazu po godini** (`ELBS_2012` … `ELBS_2026`), a `ELBS_2026` počinje
tačno 01.01.2026. Kalkulacija za januarsku fakturu često stoji u prošlogodišnjoj
bazi. Nalog `elbraco_dev_ro` nema pravo pristupa na `ELBS_2025`, pa se to ne može
proveriti — traži jedan `GRANT`.

Onih 131 neobjašnjenih, po tome koliko se taj dobavljač inače knjiži:

| | |
|---:|---|
| 65 | dobavljač se knjiži gotovo uvek (≥90 %) — **stvarni promašaj** |
| 39 | dobavljač se knjiži često (50–90 %) |
| 27 | dobavljač se retko knjiži (<50 %) — naknade, usluge |

Dakle stvarno neobjašnjenih je oko **65 dokumenata, 0,8 %**.

### Skraćivanje broja — potvrđeno, ali retko

Od 60 razlika koje poređenje po početku nalazi, **59 je u smeru „ERP kraći"**:

```
VPRAC/26-000108/VPM1  ->  ERP: VPRAC26000108
7016088538_122025     ->  ERP: 7016088538
K110/2026-835249      ->  ERP: K110202683524
```

Pretpostavka iz faze 2 je bila tačna po mehanizmu, ali pogrešna po veličini: to
nije uzrok za 7 % dokumenata nego za 0,6 %.

### Ugrađeno i izmereno

`mapiranje.py` sada, kad ne nađe tačan broj, pokuša po **prvih 10 znakova** i uzima
kandidata **samo ako je jedan**. Prag od 10 nije proizvoljan: kraći brojevi tipa
`26-301` pripadaju svakome.

Lažni parovi: od 4.641 tačno uparenog dokumenta, njih **3 (0,06 %)** bi imalo i
drugog kandidata. Dobitak: 52 zaglavlja iz ERP-a nađe svoj dokument (48 dokumenata
— neki dokument ima dve kalkulacije).

**Ali mapiranja artikala nisu porasla: i dalje 2.110 pouzdanih, 2.288 iz
kalkulacija.** Tih 48 dokumenata je od dobavljača čiji su artikli već pokriveni iz
drugih faktura. Vrednost je u tome što merenja imaju 48 dokumenata više sa poznatom
istinom, ne u novim šiframa.

### Nova komanda

`sefsync mapiranje` — do sada se izvođenje mapiranja pokretalo iz koda, bez komande.
Podrazumevano samo ispiše šta bi uradilo; `--upisi` stvarno upisuje.

### Prošlogodišnja baza — dozvola dobijena, nalaz izmeren

`elbraco_dev_ro` je 16.09. dobio pravo čitanja na `ELBS_2025`. Pretpostavka se
potvrdila: **84 dokumenta** nalaze svoju kalkulaciju tamo, 83 od njih januarski.
Uparivanje ide sa 94,8 % na **95,8 %** (7.406 od 7.727).

`mapiranje.py` sada čita i prošlogodišnju bazu — naziv se izvodi iz tekućeg
(`ELBS_2026` → `ELBS_2025`), a ako nalog nema pravo na nju, radi se bez nje i to
se samo zabeleži u log.

**Mapiranja artikala ni od ovoga nisu porasla.** Uzrok je pogledan, ne pretpostavljen:
svih 84 dokumenata je knjiženo na **magacin 088** — SOKOJ, banke, Telekom,
Vodokanal. To su decembarske usluge fakturisane u januaru, a takva kalkulacija
nema stavke: od 84 samo jedna ima ijednu stavku u ERP-u.

Dobitak je dakle u **istini o razvrstavanju** (84 dokumenta više za koje se zna da
pripadaju 088), ne u šifarniku.

### Zbir posla oko uparivanja

| | Uparenost |
|---|---:|
| Na početku | 94,8 % |
| + po početku broja | 95,4 % |
| + prošlogodišnja baza | **95,8 %** |

Ostaje 321 dokument: 39 avansa, 65 od dobavljača bez ijedne kalkulacije, oko 65
stvarnih promašaja kod dobavljača koji se knjiže redovno, i ostatak sitno razbacan.
Nijedna od preostalih grupa ne obećava nove šifre artikala.


---

## Prenos pravila na server je gubio pravila — 16.09.2026

Otkriveno proverom na serveru posle žalbe kolega. Uvoz je pravilo prepoznavao po
ključu **(polje, operator, šablon, PJ, vrsta dokumenta)** — bez dobavljača. A od
232 pravila njih **174 važi samo za jednog dobavljača**.

Posledice, obe izmerene:

**41 pravilo nikad nije stiglo na server.** Rudarena pravila za različite
dobavljače sa istim šablonom (npr. `delivery_address contains 'SRPSKIH VLADARA 46'`
za Comtrade, MISON i CHIP CARD) izgledala su kao jedno te isto: prvo bi se upisalo,
ostala bi se prebrojala kao „već postoji".

**Gašenje pravila #47 nikad nije stiglo.** Na serveru je to pravilo (`#5`,
`delivery_address contains 'Srpskih vladara 46'`) i danas **aktivno, sa 1.482
pogotka** — ono sa izmerenih 23,8 % tačnosti. Uvoz je umesto njega nalazio neko od
rudarenih pravila sa istim šablonom i gasio/palio njega, pa je svaki prolaz
ispisivao „ugašena: 1, upaljena: 1" i ništa se nije smirivalo.

### Druga polovina uzroka: velika i mala slova

Lokalno (SQLite) `pattern == 'Srpskih vladara 46'` **ne** hvata
`'SRPSKIH VLADARA 46'`; na MS SQL-u hvata, jer je poređenje neosetljivo na veličinu
slova. Isti kod je na dve baze pravio dva različita ishoda — zato se problem nije
video u razvoju.

Ispravka: ključ sada uključuje dobavljača, a šablon se poredi preko `UPPER()` na obe
strane. To je i ispravno po smislu — razvrstavanje ionako sve diže na velika slova,
pa su ta dva šablona isto pravilo i moraju imati isto stanje.

### Pouka

Razlika između razvojne i produkcione baze nije samo u brzini. `tests/test_mssql_saglasnost.py`
je napravljen posle `IS FALSE` koji SQLite prima a MS SQL odbija; ovo je isti rod
greške, samo tiši — ništa ne puca, nego se tiho radi nad pogrešnim redom.


---

## Usluge nemaju stavke — 17.09.2026

Merenjem koliko bi kalkulacija aplikacija sklopila sama ispalo je da polovina čeka
otvaranje šifre. Pogledom u spisak: „Naknada za transakcije na POS terminalu",
„Saobraćaj", „Provizija" — dakle bankarske naknade i usluge, za koje šifra artikla
nikad neće ni postojati.

Provereno u ERP-u, i to je pravilo a ne običaj:

| Magacin | Zaglavlja | Sa stavkama | `STAVKE` |
|---|---:|---:|---|
| 088 | 3.016 | **0** | `Ne` |
| 010 | 587 | 587 | `Da` |
| 021 | 273 | 273 | `Da` |

`ROBNO_ZADUZ` je **0 u svih 3.016** zaglavlja na 088; iznos nosi `IZNOS_ZA_NAP`, a
PDV `PDV`. U `MKALKUL` je polje `STAVKE` uvek prazno (4.734 reda).

Generator je ispravljen: kad je magacin 088, stavke se uopšte ne čitaju, iznosi se
uzimaju iz zaglavlja fakture, a `STAVKE` dobija `Ne` (do sada je pisalo prazno, što
se u `ULKALKUL` ne javlja nijednom).

### Efekat na spremnost kalkulacija

Mereno na 204 razvrstana dokumenta iz poslednjih 14 dana:

| | Pre | Posle |
|---|---:|---:|
| kalkulacija spremna za upis | 103 (50,5 %) | **188 (92,2 %)** |
| čeka otvaranje šifre | 101 (49,5 %) | **16 (7,8 %)** |

Onih 16 nosi 40 nemapiranih stavki, 34 različite šifre dobavljača — dakle oko
**jedan dokument i dve-tri nove šifre dnevno**. To je posao koji Dušan ionako radi;
razlika je što bi ga video na jednom mestu, sa svime što mu treba.

Alat: `alati/meri_kalkulacije.py [broj_dana]`.


---

## Odakle maloprodajna cena — mereno 17.09.2026

Predlog (Bane): uzeti `DARTIKLI.PMC`, koja je popunjena za veliku većinu artikala.
Popunjenost se potvrdila — **96,1 %** artikala koji se pojavljuju u MP
kalkulacijama ima `PMC`. Ali popunjenost nije isto što i tačnost.

Poređeno sa `MPRULK.MALACENA` — cenom koju je poslovođa **stvarno upisao**, na
20.751 stavci:

| Izvor | Poklapa se |
|---|---:|
| `ARTPROD.CENAMALO` (tekuća cena objekta) | **66,8 %** |
| `DARTIKLI.CENA` | 66,7 % |
| `DARTIKLI.PMC` | **8,7 %** |
| `DARTIKLI.SMC` | 5,4 % |

`PMC` očigledno nije cena po kojoj se prodaje — verovatno preporučena cena
dobavljača. Generator ostaje na `ARTPROD.CENAMALO`.

**Ograda:** poređenje je istorijsko — stara kalkulacija se meri naspram *današnje*
cene u šifarniku, pa je stvarno poklapanje više od izmerenog. Ranijih „80 %" iz
faze 2 mereno je na svežijem uzorku.

### Ali ideja o proveri nelogičnosti je dobra

| Provera | Koliko bi se javljala |
|---|---:|
| prodajna **ispod** nabavne | **86 (0,41 %)** |
| više od 20 % iznad `PMC` | 1.332 (6,4 %) |
| više od 20 % ispod `PMC` | 831 (4,0 %) |

Prva je retka i zato vredna — kad se javi, nešto stvarno ne valja. Druge dve bi se
javljale na svakoj desetoj stavci, što je previše da bi se čitalo.

**Odluka:** cena iz `ARTPROD.CENAMALO` kao predlog, upozorenje samo kad je predložena
prodajna ispod nabavne. `PMC` se ne koristi kao cena.


---

## Faza 2− : app sklapa kalkulaciju, čovek dopunjava — 17.09.2026

Dogovor sa Banetom, umesto skoka na automatsko otvaranje šifara.

**Tok koji već postoji u firmi:** e-faktura na SEF-u → Nina razvrstava → mejl objektu
→ poslovođa proverava da li je roba stigla i pravi kalkulaciju → potvrđuje Nini →
Nina prihvata na SEF-u. Kad naiđe nov artikal, poslovođe slikaju ambalažu u Viber
grupu (EAN i zemlja porekla), Dušan otvara šifru i javi, pa poslovođa nastavi.

**Šta app preuzima:** sklapa kalkulaciju čim je dokument razvrstan. Ako je kompletna,
čeka potvrdu prijema. Ako nije, prikazuje se na ekranu gde piše tačno šta joj fali.

### Urađeno u ovom koraku

| | |
|---|---|
| `NacrtKalkulacije` | stanje kalkulacije po dokumentu (spreman / čeka šifru / problem / upisan) |
| `services/nacrti.py` | računa i pamti stanje; `povezi_sifru` upisuje vezu u `item_mapping` |
| tab **Šifre** | u istom ekranu operatera — Nina i Dule se menjaju, pa oboje vide oba posla |
| `sefsync nacrti [--osvezi]` | isto iz komandne linije |

Nacrt se ne čuva ceo — uvek se može izvesti iznova. Čuva se **ishod** računanja, jer
je to desetak upita ka ERP-u po dokumentu, prespro za ekran. Ono što čovek uradi
čuva se tamo gde vredi i ubuduće: veza šifre ide u `item_mapping`, ne u nacrt.

Generator sada vraća nemapirane stavke kao **podatke** (broj stavke, naziv, šifra
dobavljača, EAN, količina, cena), ne kao rečenicu — ekran na kom se otvara šifra
treba tačno to.

### Odluke (Bane, 17.09.)

1. **Upis u ERP tek kad poslovođa potvrdi prijem robe.** Poslovođe i sada prave
   kalkulacije ručno; dok app upisuje ranije, pretekla bi ih i nastale bi dve
   kalkulacije za istu fakturu.
2. **Nove šifre: app predlaže kompletan artikal**, Dušan klikne „otvori".
3. **MP cena:** `ARTPROD.CENAMALO` kao predlog (mereno 66,8 %, `PMC` samo 8,7 %),
   upozorenje kad je prodajna ispod nabavne (mereno: 0,41 % slučajeva).

### Ostaje za sledeći korak

- upis u ERP na potvrdu prijema, uz proveru da kalkulacija za taj broj računa već
  ne postoji (koristi se isto uparivanje po `BROJ_RN_DOB`)
- predlog kompletnog artikla za `ARTIKLI` / `ARTPROD` / `DARTIKLI`
- pod čijim imenom app upisuje u `RBR`, i šta sa `DebitNote` i avansima

### Predlog nove šifre — izmereno pa napisano, 17.09.2026

U odeljku 4 je stajalo „detaljan redosled i koja se polja gde pune — nije još
razrađeno". Mereno na **300 poslednjih otvorenih šifara**:

| Tabela | Koliko redova po artiklu |
|---|---|
| `ARTIKLI` | matični `099` + red po robnom magacinu |
| `ARTPROD` | red po prodajnom mestu koje nije uslužno |
| `DARTIKLI` | 1 — dopuna (opis, slike, uvoznik) |

**Ispravka 17.09. (Bane):** merenje „18 redova u `ARTIKLI`, 21 u `ARTPROD`" je merilo
pogrešnu stvar — gde artikli **završe posle upotrebe**, ne gde se otvaraju. Dule
otvara šifru samo u `099`; u lokalne cenovnike je ERP kopira sam, kad korisnik krene
da radi kalkulaciju a šifre tamo nema. Vidi se na televizoru `87187`: 6 magacina,
među njima `002`, `012` i `051`, gde ga niko nije otvarao. Stari `16060` ih ima 25,
nakupljenih godinama.

Pošto app upisuje unapred (da poslovođa ne zapne), odredište je:

- **magacini** — imenovani spisak: `001, 002, 005, 010, 012, 021, 023, 026, 029, 071, 099`.
  Tehnički (izdata roba, rezervacije, inventarski, cenovnik, testni, avansni, 088)
  izostavljeni — njih ERP puni sam. `022, 024, 025` izostavljeni jer ta mesta nemaju
  izdvojen magacin nego robu drže na lageru prodajnog mesta (0 artikala u sva tri).
- **prodajna mesta** — čitaju se iz `dbo.PRODAV`, sva osim uslužnih (naziv sadrži
  „USLUGA"): 001–009, 071 pab, 090 internet, 092 nekretnine. Nova prodavnica tako uđe
  sama, bez diranja koda.

Stalne vrednosti, sve 300/300: `DECKOLIC=2`, `DECCENA=2`,
`VLAGA=LOM=PRIMESE=NECISTO=0.00`, `PCENA=0.000`, `PROIZVODJAC='.'` (popuna, 294/300).
`JEDMERE` je „kom" kod 42.843 od 43.491 artikla. Tarifa: 20 % → `002`, 10 % → `001`,
0 % → `003` (`PTARIFE`).

Spisak magacina i prodajnih mesta se **ne piše u kod** nego se čita iz ERP-a — gde su
završili poslednji otvoreni artikli, i to samo ono što se ponavlja kod bar 90 % njih.
Nova prodavnica tako uđe sama, a ugašena ispadne.

### Šifru i dalje dodeljuje čovek

Brojevi šifara ne prate pravilo koje bi se moglo pogoditi: ispod 90.000 ima 33.651
šifara (najveća 89.817), između 90.000 i 999.999 još 9.673, a iznad milion 134 — do
9.999.999. `MAX+1` bi dao deset miliona. Dok se ne sazna po čemu se bira, app šifru
**ne dodeljuje** — predloži sve ostalo, a šifru upisuje Dušan.

Jedna provera je već tu: ako barkod sa fakture **već ima šifru**, app to kaže i
predlaže povezivanje umesto otvaranja nove.

### Upisa nema

`erp/artikal.py` ne upisuje ništa — ispiše `INSERT` naredbe da se vidi šta bi se
desilo. Nalog namerno nema pravo pisanja po šifarniku.

### Rupe u šifarniku — odakle nova šifra (17.09.2026)

Šifre nisu nizale redom pa `MAX+1` ne radi, ali slobodnog mesta ima napretek.
Zauzetost je merena kao **unija sve tri tabele** (`ARTIKLI`, `ARTPROD`, `DARTIKLI`) —
šifra ume da postoji u `DARTIKLI` a da je u matičnom magacinu više nema, i takva se
ne sme dodeliti drugom artiklu.

| | |
|---|---:|
| zauzetih brojčanih šifara | 43.483 |
| rupa ukupno | 872 |
| slobodnih mesta ispod 100.000 | **56.890** |
| rupa od bar 100 mesta | 46 (52.566 mesta) |

Najveće rupe ispod 100.000: `40411–45378` (4.968), `45380–50000` (4.621),
`50019–54284` (4.266), `30299–34301` (4.003).

**Pravilo koje je ugrađeno:** prva slobodna šifra iz **prve rupe od bar 500 mesta**
(odluka: Bane, 17.09.),
uz obaveznu proveru baš te šifre pred dodelu (spisak je snimljen na početku, a
šifru u međuvremenu može otvoriti neko drugi). Prag od 100 postoji da se nove šifre
ne razbacuju po usamljenim rupama među starim artiklima — ima ih preko 800 veličine
jedan; šifre iz istog perioda tako ostaju na okupu i vidi se po samom broju.

Sa pragom 500 ima **26 rupa i 47.039 mesta**, a prva predložena šifra je danas
**14445** (blok `14445–14999`). Pri tempu od oko 60 novih artikala mesečno, samo
najveći blokovi (`40411–45378`, 4.968 mesta) traju godinama.

`sefsync sifre-slobodne` ispisuje mapu rupa i šifru koja bi se predložila.


---

## „Nepregledano" umesto statusa „Nova" — 17.09.2026

Zaštita statusa „Nova" ima cenu koja se do sada nije merila. Sa punim UBL-om
razvrstavanje pogađa **79 %** dokumenata; **samo iz pregleda** (dobavljač, iznos,
datumi, broj) — **15 %**. Mereno na 4.000 dokumenata.

Nijedna odluka nije pogrešna, samo ih skoro nema: adresa isporuke, napomena, nazivi
stavki i `AdditionalDocumentReference` stižu tek sa UBL-om, a on obara „Nova".

**Odluka (Bane, 17.09.):** status „Nova" je kolegama služio samo kao interna oznaka
šta je nepregledano. Tu oznaku sada vodimo mi:

- `Document.pregledano_at` / `pregledano_by`
- oznaka **novo** u redu, i svoj prikaz „Nepregledano" sa brojačem
- dugme „Označi pregledano" (pojedinačno i grupno)
- prosleđivanje poslovnoj jedinici skida oznaku samo po sebi — ko prosledi, taj je i video

Kolege dobijaju nalog u panelu i prate taj prikaz umesto „Nova" na portalu. Kad to
zažive, `SEF_PRESERVE_NEW` se gasi i app preuzima UBL odmah — razvrstavanje ide sa
15 % na 79 %, a nikome se ništa ne pomera.

**Redosled je bitan:** prvo nalozi i navika, pa tek onda gašenje zaštite.

### Nacrti se računaju sami — 17.09.2026

Prvi radni dan tab „Šifre" je odmah pokazao rupu. Operateri su prijavili da artikla
`FOX 32DTV270H` (faktura VIMPORT `FA-2647-001/26`) nema ni u ERP-u ni na spisku u
panelu.

Tačno je oboje, a uzrok je bio u aplikaciji: nacrt je do tada računala **samo**
komanda `nacrti --osvezi`. Faktura je stigla u 12:55, a poslednje računanje bilo je
u 12:08 — 47 minuta ranije. Pet stavki bez naše šifre, i nijedna se nije videla.

Sada se nacrt računa čim ima šta novo: kad se preuzme UBL i kad dokument dobije
poslovnu jedinicu (bilo pravilom bilo ručno). Ne računa se za svih 780 dokumenata u
svakom prolazu, nego samo za promenjene. Nedostupan ERP ne obara sinhronizaciju —
nacrt se tad preskače uz zapis u log.

**Usput potvrđeno čemu ovo služi:** poslovođa je za tu fakturu već napravio
kalkulaciju (`MKALKUL 005/00499`) sa **tri od pet stavki** — fale baš one dve kojima
šifra ne postoji. To je greška koju faza 2− treba da spreči: app kaže da kalkulacija
nije kompletna dok se šifra ne otvori.


---

## Slike sa objekta umesto Viber grupe — 17.09.2026

Podaci za otvaranje nove šifre (barkod, zemlja porekla) do sada stižu kao slike u
Viber grupu. Provereno: **Viber nema API za grupe** — postoji samo Public Account /
bot, u koji obična grupa ne spada, i bot ne može da uđe u nju. Čitanje lokalne baze
Viber Desktopa je jedina druga mogućnost, ali zavisi od upaljenog klijenta i menja se
sa svakom verzijom — rešenje koje pukne kad neko klikne „update".

Zato slika ide tamo gde joj je mesto: **uz stavku kojoj fali šifra**.

- `Slika` (tabela `slika`): dokument, opciono broj stavke, putanja, napomena, ko i kada
- fajl na disku (`STORAGE_DIR/slike/<godina>/<mesec>/`), u bazi samo putanja — kao UBL
- prima se samo slika (JPG, PNG, HEIC, WEBP), najviše 12 MB; ulaz koristi neko van
  kancelarije, pa se ne veruje onome što je poslato
- poslovođa vidi svoje dokumente, tuđi vraćaju 403
- Dušan slike vidi **na tabu „Šifre"**, uz red sa EAN-om i nazivom

### Ekran poslovođe radi na telefonu

Slikanje radi poslovođa na licu mesta, pa ceo njegov deo mora da radi palcem.
Dodat je `@media (max-width: 720px)`: tabele postaju kartice (svaka ćelija dobija
naslov iz `data-naslov`), dugmad i polja imaju najmanje 42 px visine, `dl.kv` prelazi
u jednu kolonu. Polje za sliku koristi `capture="environment"`, pa telefon otvara
kameru umesto galerije.

### Isti artikal stiže na više objekata — 17.09.2026

Pitanje (Bane): ako poslovođa 1 slika artikal, kako poslovođa N zna da ne mora i on?
I šta se dešava sa njegovom fakturom kad Dušan otvori šifru?

Odgovor je u tome čemu slika i šifra **pripadaju**: artiklu, ne fakturi.

**Slika.** Uz sliku se prepisuje barkod, šifra dobavljača i PIB sa stavke. Na strani
dokumenta svaka stavka bez naše šifre nosi oznaku „čeka šifru", a ako je artikal već
neko slikao — makar sa druge fakture i iz drugog objekta — piše **ko je i kada slikao,
sa linkom na sliku**. Traži se prvo po barkodu (isti je svuda), pa po šifri dobavljača
uz njegov PIB.

**Šifra.** Kad Dušan poveže šifru, veza ide u `item_mapping`, a odmah zatim se
**preračunavaju nacrti svih ostalih faktura** koje čekaju isti artikal (po barkodu ili
po šifri dobavljača + PIB). Poslovođa u drugom objektu time dobija spremnu kalkulaciju
bez ijednog poteza — ranije bi mu stajalo „čeka šifru" dok neko ručno ne preračuna.

Dva testa: da otvorena šifra rešava i tuđu fakturu, i da se slika jednog objekta vidi
na fakturi drugog.

**Granica, svesno prihvaćena:** kad dobavljač ne šalje barkod, spajanje ide po
njegovoj šifri i važi samo unutar tog dobavljača — isti artikal nabavljen od dva
različita dobavljača, bez barkoda, ne prepoznaje se kao isti. Bane: „ekstremno je
retko", pa se ne meri niti rešava dok se ne pojavi.


---

## Potvrda razvrstavanja i prosleđivanje su spojeni — 18.09.2026

Ekran poslovođe MP001 bio je prazan iako mu je razvrstano 12 dokumenata. Nije
greška: poslovođa vidi samo ono što je **prosleđeno**, a od 160 razvrstanih
dokumenata prosleđen je bio **jedan**.

Ispod toga je stajala rupa u toku rada: svih 26 dokumenata koji čekaju šifru bilo je
neprosleđeno, a poslovođa neprosleđen dokument ne može ni da otvori — dakle ne može
ni da slika ambalažu, po kojoj Dušan otvara šifru. Posao je stajao na oba kraja.

**Odluka (Bane, 18.09.):** potvrda razvrstavanja i prosleđivanje postaju jedan potez.
Ko kaže čija je faktura, taj ju je i poslao. Dva klika za jednu odluku imala su smisla
dok se nije znalo koliko razvrstavanje pogađa; sada je pokrivenost 77,5 %, a tačnost
tamo gde odluči preko 90 %.

- u redu je sada jedno dugme **„Potvrdi i prosledi"**
- ručna dodela poslovne jedinice odmah i prosleđuje („Sačuvaj i prosledi")
- već prosleđen dokument se ne šalje ponovo sam od sebe — za to postoji „Prosledi ponovo"

Sledeći korak (kad se skupi dovoljno provere): dokument koji razvrsta pravilo bez
ijednog promašaja ide objektu **sam**, bez Nininog klika.

### Mejlovi još ne idu objektima

`NOTIFY_OVERRIDE_TO` na serveru i dalje preusmerava sva obaveštenja na jednu adresu.
Prosleđivanje zato čini dokument vidljivim poslovođi u panelu, ali mu obaveštenje ne
stiže. To se briše kad Bane kaže — i tek tada prosleđivanje ima pun efekat.


---

## Magacin 010: kako povraćaj zaista teče (Bane, 18.09.2026)

Objekat kupcu vrati novac po nekoj **saglasnosti** dobavljača. Artikal koji je kupac
vratio prebacuje se sa stanja objekta u **magacin 010**, gde se „poništava" knjižnim
odobrenjem dobavljača. Zato kalkulacije na 010 **imaju stavke** — za razliku od 088.

**Eurotehna-021 je izuzetak:** na knjižnom odobrenju ne navodi artikal nego vezu ka
saglasnosti („Odobrenje po Saglasnosti broj 277102/2026"). Stavka nema ni EAN ni šifru
dobavljača, pa app traži našu šifru koju ne može da pogodi — da bi se to rešilo,
trebalo bi znati šta stoji na kojoj saglasnosti, a taj podatak ne postoji ni na
fakturi ni u ERP-u.

Kod ostalih dobavljača je jednostavnije: odobrenje navodi artikal.

**Odluka: ne rešava se sada.** Ti dokumenti ostaju na spisku „čeka šifru" i Dušan ih
preskače. Ako počnu da smetaju, najmanji zahvat je dugme „ovo nije artikal", koje
stavku skida sa spiska bez otvaranja šifre.

### „Za slikanje" — kartica poslovođe, i „nije artikal" — dugme za Dušana (18.09.)

**Poslovođa** je do sada morao da otvara fakturu po fakturu da bi video šta je novo.
Sada ima svoju karticu **„Za slikanje"** sa brojačem: spisak artikala sa njegovih
faktura kojima fali naša šifra, sa nazivom, količinom i EAN-om, i dugmetom koje otvara
kameru odmah tu. Ako je artikal već neko slikao, umesto dugmeta stoji slika i „već
slikano — ko".

**Dušan** je dobio dugme **„nije artikal"** na tabu Šifre. Stavka tipa „Odobrenje po
Saglasnosti broj 277102/2026" (Eurotehna) ili troškovi transporta nikad neće imati
našu šifru; dugme je skida sa spiska i iz kalkulacije, uz zapis ko je to rekao.
Poništava se na strani dokumenta („vrati").

### Slike se traže po artiklu i na Dušanovom tabu (18.09.)

Prijavljeno: poslovođa u Somboru slikao „EL.BICIKL MONTURA 250KW/36V BEŽ", a kod
Dušana na fakturi drugog objekta sa istim artiklom nije pisalo ništa.

Uzrok je bio u mom kodu: na tabu „Šifre" slike su se tražile **po dokumentu**
(`Slika.document_id in ...`), a isti artikal stiže na više objekata, pa je slika
najčešće sa tuđe fakture. Na strani dokumenta i u poslovođinoj kartici traženje je
već išlo po artiklu — ovde nije.

Sada se i tu traži po artiklu (barkod, pa šifra dobavljača + PIB), i slika stoji u
svom stupcu **uz stavku**, sa imenom onoga ko ju je poslao. Test pokriva baš taj
slučaj: slika sa jedne fakture mora da se vidi na drugoj.

---

## Jedna dobavljačeva šifra, više naših artikala — 18.09.2026

CSM fakturiše „ponye 20 litium" **bez oznake boje**, a kod nas je svaka boja svoja
šifra. Dušan mora da sačeka potvrdu iz objekta koja je boja stigla — i to je u redu.
Opasno je ono što se dešavalo posle: ručni unos šifre pamtio je vezu trajno, pa bi
**sledeća faktura tiho dobila boju od prošlog puta**.

Provereno u podacima: od 2.110 postojećih mapiranja **nijedno nije dvosmisleno**, jer
izvođenje iz istorije odbacuje kolebljiva (45 takvih je odbačeno). Rupa je bila samo
u ručnom unosu.

Sada:

- ista dobavljačeva šifra vezana za **drugi** artikal → veza se **gasi**
  (`active=False`, `source='kolebljivo'`), a ne prepisuje. Sledeći put opet odlučuje
  čovek. Obe stavke ipak zadrže svoju šifru.
- ista šifra za **isti** artikal → potvrda, uz brojač pogodaka
- čekirano **„ne pamti vezu"** → šifra se upiše samo na tu stavku, šifarnik ostaje
  netaknut (za dobavljače za koje se unapred zna da su ovakvi)

Zapis o gašenju ide u dnevnik (`mapiranje.kolebljivo`), sa obe šifre — da se vidi
zašto je veza nestala.

---

## Podela OFFICE / 010 — gde je stvarno grešila (18.09.2026)

Bane: „podela između office i 010 je još malo problem u praksi". Izmereno na 923
odobrenja sa poznatim odgovorom iz ERP-a.

**Greške nisu bile tamo gde sam mislio.** Od 64 greške, njih **36 je bilo
„poslato u prodavnicu umesto na 010"** — pravilo po adresi isporuke (prioritet 20)
okine pre pravila za knjižna odobrenja (40/45) i pošalje odobrenje objektu iz kog je
roba vraćena.

Presudan nalaz iz istine: od 923 odobrenja **568 je na 010, 355 na 088, nijedno u
prodavnici**. Odobrenje u ERP-u ne završava u objektu — nikad.

To je isto ono što je Bane opisao: kupac vrati robu u objekat, roba pređe sa stanja
objekta u magacin 010, gde je knjižno odobrenje poništava. Adresa isporuke na
odobrenju govori **iz kog objekta je povraćaj**, a ne kome dokument pripada.

### Šta je mereno pre nego što je odlučeno

| Varijanta | Odlučeno | Tačnost | Grešaka |
|---|---:|---:|---:|
| Zatečeno | 892 (96,6 %) | 92,8 % | 64 |
| Pravila za odobrenja idu prva (prioritet 5) | 892 | 93,6 % | 57 |
| Samo pravila za odobrenja, ostalo operateru | 775 (84,0 %) | 94,3 % | 44 |
| Samo pravila za odobrenja, ostatak → 010 | 923 (100 %) | 86,6 % | 124 |
| **Odluka na objekat → 010** | **892 (96,6 %)** | **96,7 %** | **29** |

Poslednja je i ugrađena: **greške prepolovljene, bez ijednog dodatnog ljudskog
poteza**. Prevod radi samo za `CreditNote`; faktura i dalje ide objektu.

---

## Knjižna zaduženja i avansi — izmereno 18.09.2026

Otvoreno pitanje iz odeljka 5 („16 `DebitNote` i 39 avansa: imaju li svoju kalkulaciju
ili idu računovodstvu mimo ovoga?") — sada ima odgovor iz podataka.

**Knjižno zaduženje (`DebitNote`, šifra 383)** je obrnuto od odobrenja: dobavljač
naknadno **poveća** dug. Svih 16 u istoriji su **prefakturisani troškovi** — zakup
(VLAMA), struja (EPS), voda, taksa za medijski servis. **15 od 16 je u ERP-u na
magacinu 088**, dakle ulazni račun bez stavki; jedan nema kalkulaciju.

Razvrstavanje ih već rešava: 15 sa poznatim odgovorom, **12 tačno, nula grešaka**,
3 neodlučena. Ništa se nije menjalo.

**Avansni račun (`Prepayment`, šifra 386) ne ide kroz kalkulaciju uopšte** — od 39
u istoriji **nijedan** nema kalkulaciju u ERP-u. A generator bi im napravio nacrt i
rekao „spremna", pa bi app kasnije upisala dokument koji u knjigama ne postoji.

Ugrađeno: avans se odbija **pre nego što se ERP uopšte dodirne**, a nacrt dobija
stanje `NE_TREBA` — ne stoji na spisku kao da nešto čeka. Dva testa, od kojih jedan
pada ako se za avans uopšte otvori veza ka ERP-u.

---

## `RBR`: ko upisuje stavku — izmereno 21.09.2026

Pretpostavka je bila da `RBR` upisuje klik na **KNJIŽENJE** u ERP-u. **Nije tako.**
Od 23.079 stavki u kalkulacijama **nijedna nema prazan `RBR`**, uključujući i onu na
nezaknjiženoj kalkulaciji (`PROKNJIZENO=0`). Dakle upisuje se **pri unosu stavke**.

Primer sa nezaknjižene: `8244453167141802SuzicNikol` — Clarion dan `82444`
(18.09.2026), vreme `5316714`, četiri cifre `1802`, korisnik `SuzicNikol` (10
znakova). Polje je `varchar(39)`, naših 26 znakova staje.

**Znači app mora sama da upiše `RBR`, i u njemu stoji čije ime.**

**Odluka (Bane, 21.09.): ime aplikacije — `sefsync`.** Kad nešto krene naopako,
`RBR LIKE 'sefsync%'` odmah kaže šta je app napravila i šta treba obrisati. Ko je
pokrenuo upis piše kod nas, u `nacrt_kalkulacije.upisao`.

### Prava u produkciji

Odobren `GRANT INSERT` na `ULKALKUL`, `MKALKUL`, `PRULKALK`, `MPRULK` u `ELBS_2026` —
bez `UPDATE` i `DELETE`, pa app ne može da dira ono što je već uneto. Šifarnik
(`ARTIKLI`, `ARTPROD`, `DARTIKLI`) ostaje netaknut.

Druga brava ostaje u kodu: bez `ERP_ALLOW_PRODUCTION_WRITE=true` upis u produkciju se
odbija bez obzira na prava u bazi.


---

## Upis kalkulacije u ERP — 21.09.2026

Poslednji deo faze 2−. Upis se pokreće **kad poslovođa potvrdi prijem robe** — ranije
nema smisla, jer bi app pretekla poslovođu koji kalkulaciju i dalje pravi rukom.

### Tri brave, svaka za svoju vrstu greške

1. **`ERP_UPIS_KALKULACIJE`** (podrazumevano `false`) — dok se ne uvežba, potvrda
   prijema radi kao i do sada i ne dira ERP.
2. **`ERP_ALLOW_PRODUCTION_WRITE`** (podrazumevano `false`) — upis ide u test bazu
   `ELBSX_2026`; produkcija traži izričitu dozvolu.
3. **Provera duplikata** — pre upisa se traži da li za taj broj računa tog dobavljača
   već postoji kalkulacija (poređenje po normalizovanom broju, `FA-5543-03/26` ==
   `FA55430326`, u `ULKALKUL` i `MKALKUL`). Ako postoji, app **ne pravi drugu**, nego
   nacrt označi kao upisan sa napomenom „ručno u ERP-u". Odluka (Bane): dve
   kalkulacije za istu fakturu gore su nego nijedna.

Uz to ostaju ranije brave: nespremna kalkulacija se ne upisuje, `PROKNJIZENO` sme da
bude samo `0`, a broj se ponovo uzima ako ga neko preotme.

### Šta se beleži

`nacrt_kalkulacije` dobija `stanje=UPISAN`, broj kalkulacije (`002/00450`), vreme i
**ko je pokrenuo** upis. U ERP-u, u `RBR`, stoji `sefsync` — pa se po tome razlikuje
šta je app napravila.

**Neuspeh upisa ne poništava potvrdu prijema**: roba jeste stigla, šta god ERP rekao.
Poslovođa dobija rečenicu šta se desilo (nije spremna, već postoji, ERP nedostupan).

Pet testova, među njima i da se upis ne ponavlja i da se nespremna kalkulacija ne
upisuje ni slučajno.

### Ko pušta kalkulaciju kad nema prijema robe (22.09.2026)

Prvi upis u `ELBSX_2026` prošao je tačno — `MKALKUL 001/U/00529`, Eurotehna, račun
`3476VP-2026`: iznosi, rabat 47 %, marža 45,16 %, `PROKNJIZENO=0`, `IZVOREPP`
„Elektronske fakture", a `RBR` `8244834164430570sefsync`.

Pitanje koje je odmah zatim ispalo: **ko potvrđuje prijem za dokumente na upravi (088)?**

Odgovor je bio — niko. `OFFICE` nema `erp_code`, pa se nijedna osoba iz šifarnika
osoblja ne može povezati s tom jedinicom, a operateri nemaju dugme za potvrdu prijema
(ono postoji samo na ekranu poslovođe).

I logički je tako ispravno: **kod usluga nema prijema robe.** Niko ne „primi" struju,
zakup ni bankarsku naknadu. Takav dokument proveri kancelarija i unese ga u ERP.

Zato operater na strani dokumenta sada ima dugme **„Upiši kalkulaciju u ERP"**, vidljivo
kad je nacrt spreman i kad je upis uopšte uključen. Za robu ostaje redovan put —
potvrda prijema u objektu; ovo je za usluge i za slučaj kad poslovođa ne može.

Uz to, u kartici „Tok" sada piše stanje kalkulacije: spremna, čeka šifru (sa brojem
stavki), zaustavljena, ne ide kroz kalkulaciju, ili **u ERP-u** — sa brojem, ko je i
kada pustio.

### Ko radi u kojoj jedinici — veza stoji u panelu (22.09.2026, ispravljeno)

Ilijana Krnjajić (`ilijana`, funkcija REKLAMACIJE) radi povraćaje. Da bi videla
dokumente magacina 010, treba joj `SifraPovezanogObjekta` — ali **ne `010`**: ta
šifra će jednog dana biti nova prodavnica, pa su to dva različita prostora imena.

Prvo rešenje je bilo da se kod osoblja upiše `110`. **Odbačeno**: `kontakti_osobe`
u istom obliku koristi i projekat **LOGISTIKA**, pa upisivanje naše šeme u tuđu
tabelu znači da im menjamo podatke. (Kolona `Napomena` je prazna na svih 36 redova,
ali prisvajanje tuđeg slobodnog polja puca onog dana kad je oni zauzmu.)

**Veza čovek–jedinica zato stoji kod nas**: `BusinessUnit.logins`, spisak prijava
zarezom, popunjava se na kartici „Poslovne jedinice". Ko nije nigde naveden, nalazi
se po starom — `SifraPovezanogObjekta` → `erp_code`, kako rade prodavnice.

Time se rešava i prijava: Ilijana u šifarniku nema objekat, pa bi je stari kod odbio
pre nego što se do jedinice uopšte dođe. Sada se, kad objekta nema, gleda spisak
prijava u panelu.

Isto važi za magacine (MAG021, MAG023, MAG026, MAG029): nijedan nema čoveka sa
odgovarajućom šifrom objekta — svih 19 ljudi sa popunjenim objektom vodi na prodajna
mesta MP001–MP009. Kad magacini budu radili u panelu, njihove prijave se upisuju
ovde, bez diranja šifarnika osoblja.

### Ilijanin tok nije isti kao kod poslovođe — za kasnije

**Njoj roba ne stiže.** Knjižno odobrenje na 010 služi za **razduženje robe koju joj
je maloprodaja prebacila internim prenosom**, a ne za prijem isporuke. Zato joj dugme
„Roba je primljena" ne odgovara po smislu, iako mehanički radi.

Ostaje da se prođe ceo taj tok (interni prenos → razduženje → knjižno odobrenje) i
napravi šta treba. Zabeleženo da se ne izgubi; Bane: „ima još detalja".

## PDV evidencija: šta piše app, a šta knjiženje

Dule je na prvom pravom upisu prijavio da kod računa za struju „stavke razdvajaju
PDV i da se to kuca ručno". Izmereno gde tačno, jer su u igri dve tabele sličnog
imena:

| | ko piše | ključ |
|---|---|---|
| **`PDVEVIDX`** | **čovek pri unosu** (tab POPDV na formi ulaznog računa) | `DOKUMENT = '088'+broj`, `REDBR`, `TIP` |
| `PDVEVID` | **knjiženje** | `UR88<broj><redbr>`, odnosno `ULK`/`ULM`+magacin+broj |

Od **3.075** računa na 088 **svih 3.075** ima red u `PDVEVID`, a samo **87** ima red
u `PDVEVIDX` — i kod svih 87 se broj redova poklapa red za red (2↔2, 3↔3). Kad
podele nema (2.988 računa), ERP sam izvede jedan red iz zaglavlja.

**Zato app piše `PDVEVIDX`, a `PDVEVID` nikada.** Ranije zabeleženo „PDVEVID ne
treba" je bilo tačno, ali nepotpuno — `PDVEVIDX` je tada promakao.

### Pravilo se čita iz same fakture

Na EPS računu `KP101016682899` stavka „JMS taksa" 349,00 nosi kategoriju **OE**
(nije predmet PDV-a, `PDV-RS-17-4-2`), a osnovica za struju 39.134,26 nosi 20 %.
ERP je to na `088/03039` zapisao kao:

```
REDBR 1  TIP  1  IZNOS 46.961,11  OSNOVA2 39.134,26  PDV2 7.826,85
REDBR 2  TIP 23  IZNOS    349,00  OSNOVA2      0,00  PDV2     0,00
```

Dakle: **neoporezivi deo → TIP 23, oporezivi deo → TIP 1**, zbir daje iznos računa.
`OSNOVA1/PDV1` su posebna stopa (10 %), `OSNOVA2/PDV2` opšta (20 %) — isti raspored
kao `MPVRED1/MPVRED2` u maloprodaji.

### Šta app namerno ne radi

**TIP 21** („bez prava na odbitak") — svih 16 redova nosi PDV, i to su gorivo
(Knez petrol, Lukoil, OMV, Euro petrol) i deo struje. Odbitak zavisi od namene, ne
od fakture, pa to ostaje čoveku.

**TIP 27** („bez evidencije u KPR") — 27 redova, sitni iznosi kod komunalnih.

Po skupu tipova: **51** od 87 podeljenih računa je čisto 1 + 23, dakle app ih pokriva;
ostalih 36 dira 21, 27 ili 34.

**Račun koji bi bio ceo neoporeziv se ne dira.** Takav u istoriji ne postoji, pa nema
po čemu da se proveri šta bi ERP očekivao u zaglavlju.

## Tri nerazvrstana dobavljača, tri različita odgovora (23.09.)

Komanda `razvrstaj` sada ispisuje i **koji** dokumenti ostaju, pa se odmah videlo da
devet nerazvrstanih čine samo tri dobavljača. Nijedan od njih nema pravilo — nisu
dakle propala, nego ih nikad nismo mapirali. Gde im roba stvarno ulazi u ERP:

| dobavljač | u ERP-u | odluka |
|---|---|---|
| **FORMA PLUS** (101717578) | 63 kalkulacije, razbacane po **svim** prodavnicama 001–009 (po ~9 na svaku) | pravilo „dobavljač → jedinica" bi bilo pogrešno |
| **EPS** (103920327) | 37 kalkulacija, **svih 37 na 088** | pravilo #263, prioritet 60, kao ostalih 25 režijskih |
| **Rolostil pr / Predrag Gajić** (100754526) | nijedna kalkulacija | nov dobavljač, ide operateru |

**FORMA PLUS se uči iz napomene.** Postojećih sedam razvrstava operater ručno; od
sledeće fakture dobavljač u napomenu upisuje oznaku objekta, pa se pravilo piše nad
poljem `note` — ono je već podržano i u `MatchField` i u `routing_fields()`, ne treba
nikakva izmena koda.

Pouka za ubuduće: kad jedan dobavljač snabdeva sve objekte, PIB nije dovoljan signal.
Razlika između EPS-a i FORMA PLUS-a je baš u tome — EPS uvek ide na jedno mesto, FORMA
PLUS nikad.

## Kako se proverava da upis liči na ERP-ov (23.09.)

Prva dva probna upisa sam poredio **polje po polje**, i tako našao dve greške —
pa stao. Treći put sam pustio poređenje **svih kolona odjednom**: za svaku kolonu
se uzme šta ERP piše u 200 svojih zapisa na istom magacinu, i ako je tamo jedna te
ista vrednost u bar 90 % slučajeva, to je konvencija — pa se uporedi sa našom.

Izašla je **81 razlika**. Toliko ih polje-po-polje ne bih našao nikad.

| nalaz | šta je bilo |
|---|---|
| `DATUM_PRIJEM` | app ga pisala i kod usluga; na 088 je nula na svih 3.076 zapisa |
| `MENJA_CENU` | app gledala „je li maloprodaja"; ERP piše `Da` svuda gde ima robe |
| `IDPDV` | app ga uopšte nije pisala; na 010 je 35 („Povrat robe") na 690 od 692 |
| **78 kolona sa NULL-om** | Clarion nigde ne ostavlja NULL nego nulu ili prazan string |

Posle ispravki poređenje izlazi prazno, osim `PROKNJIZENO` — naša je 0
(nezaknjižena), a stari uzorci su zaključani (2). Ta razlika **mora** da ostane.

**Pouka o metodu:** kad se upisuje u tuđu šemu, ne poredi se ono čega se setiš nego
sve što se može uporediti. Tabela ima 61 kolonu, app računa 22 — ostalih 39 se ne
vide dok ih neko ne izbroji.

### Sva tri toka su prošla istu proveru

| tok | zapis u `ELBSX_2026` | šta je posebno provereno |
|---|---|---|
| usluga | `ULKALKUL 088/02530` | podela PDV evidencije (TIP 1 + TIP 23), bez stavki |
| veleprodaja | `ULKALKUL 021/00227` | tarifa, RBR, rabat po stavkama, `IDPDV` |
| maloprodaja | `MKALKUL 008/00466` | marža: `MALACENA`, `IZN_RUCM`, `MPVRED1/2`, `UKAL_PDV1/2` |

Kod maloprodaje su sva četiri zbira iz zaglavlja izvedena iz stavki i poklopila se
u dinar, a `MALACENA` je na sve četiri stavke jednaka tekućoj ceni objekta u
`ARTPROD`.

**App ne izmišlja cenu.** Uzima tekuću cenu objekta i iz nje izvodi maržu. Kad
nabavna poskupi, marža ispadne manja — odluka da se cena digne ostaje poslovođi,
kao i pre.

## Magacin 010 — povrat robe (23.09.)

`MAGACINI`: **010 = „Povrat robe"**, 012 = „REZERVACIJA MAGACINI", 011 = „USLUGE",
088 = „BEZ EVIDENCIJE U ROBNOM".

Kako ERP vodi povrate danas — 587 zaglavlja, 692 stavke:

| | |
|---|---|
| sve negativno | osnovica, PDV, ukupno; količina na svih 692 stavke |
| **nabavna cena pozitivna** | minus nosi količina, ne cena |
| rabat | **nijedna stavka ga nema** (0 od 692) |
| `IDPDV` | 35 „Povrat robe" (690), 34 „Finansijsko odobrenje" (1), 1 (1) |
| `KONTO` / `STAVKE` / `MENJA_CENU` | 435 / Da / Da, bez izuzetka |
| tab „Veza robno" (`ZMAG`, `ZARTIKAL`, `BROJ_PRIJEM`) | prazan na svih 587 |

**Cena dolazi sa knjižnog odobrenja, ne iz ranije kalkulacije.** Od 16 uporedivih
slučajeva 9 se poklopilo sa ranijom kupovinom, 7 nije (povrat 15.210 gde je kupovina
bila 14.742). Zato app cenu računa iz dokumenta, kako i radi.

**Rabat se uračunava** (Bane, 23.09.). Da knjižno odobrenje navede rabat, app piše
`FAK_CENA` bruto, `RABAT_PROC`, `IZN_RABATA` i neto `NAB_CENA` — isto kao kod
kupovine. To što ERP na 010 nema nijedan rabat je zato što ih odobrenja ne nose, a
ne zato što se ne smeju upisati.

### Eurotehna: 88 od 587 kalkulacija

Njihova odobrenja ne navode artikal nego vezu ka saglasnosti. Bane: dva puta do
artikla —

1. **kroz stavku**, gde piše na koju se saglasnost odnosi, pa Ilijana sama upiše
   artikal sa te saglasnosti;
2. **kroz napomenu**, gde stoji za koji uređaj je odobrenje — *„to bi možda moglo
   da se automatizuje"*.

Drugi put je kandidat za pravilo nad poljem `note`, isto kao kod FORMA PLUS-a. Pre
bilo kakvog koda treba videti kako ta napomena stvarno izgleda na više odobrenja;
zato `sefsync stavke` sada ispisuje i napomenu, prilog i veze.

### Ilijanin tok — zaseban posao

Njoj roba ne stiže spolja: prebaci je maloprodaja internim prenosom. Baneova ideja
(23.09.): iskoristiti **magacin 012** i logiku iz projekta **LOGISTIKA** — Ilijana
traži prebačaj od objekta, dokument se napravi **do faze knjiženja**, objekat dobije
obaveštenje da ga zaknjiži; isto i za „čišćenje" njenog magacina knjižnim odobrenjem.

Prenos se **ne vodi kroz kalkulacije** — u `ULKALKUL` i `MKALKUL` je `VRSTAKNJIZ`
uvek `'U'`. Kandidati su `PROMETPR` i `PRIRACUN`, ali to se ne pogađa iz tabela:
LOGISTIKA taj tok već ima, pa se kreće od njenog koda.

### Odobrenje bez artikla: predlog iz napomene (24.09.)

Mereno na **967 knjižnih odobrenja** iz panela (arhivirana uključena — bez njih se
gleda dvadesetak najnovijih i ništa se ne vidi).

Prva dva merenja su davala tačne brojeve o pogrešnom skupu:

1. „791 već navodi artikal" — kriterijum je bio spisak ključnih reči u nazivu
   stavke, pa je promašio „KNJIZNO ODOBRENJE 20%" (EWE COMP). Ispravno je pitati
   `_nasa_sifra` da li stavka uopšte dobije šifru.
2. I posle toga je brojka („53 od 574") i dalje merila pogrešno: u njoj su bila i
   **finansijska odobrenja** — rabat, bonus, podrška prodaji — koja idu na 088 i
   kojima artikal uopšte ne treba.

Podela koja nešto znači je po tome **gde odobrenje ide**:

| | |
|---|---|
| ide na **088**, artikal ne treba | **384** |
| povrat na 010, sve stavke imaju šifru | **317** |
| **povrat staje, ima predlog** | **53** |
| **povrat staje, nema predloga** | **182** |
| nerazvrstano | 31 |

Dakle staje **235** povrata, a predlog rešava **53 — nepunu četvrtinu**.

**Tačnost tih 53: 50 od 52 (96 %)**, provereno naspram artikla koji je čovek
stvarno uneo u ERP za to isto odobrenje. Dva promašaja:

- `587FN-2026` — predlog `84655 Gorenje RIU 609 EA1`, čovek `95961 GorenjeRIU609EA1`:
  **isti uređaj pod dve šifre**, nije greška predloga nego dupliran artikal
- `568FN-2026` — predlog `95048`, čovek `95086`: pravi promašaj

Zato predlog **ide čoveku na potvrdu**, ne u kalkulaciju sam od sebe. 96 % štedi
traženje, 4 % je razlog da se ne potpisuje naslepo.

**Dva nalaza usput koja se ne bi videla iz jednog primera:**

1. Broj fakture se traži **kroz sve dostupne godine** — 18 od 53 je nađeno u
   `ELBS_2025`. Bane: „ovakva k.o. čisto stižu za robu kojoj je kalkulacija ulazna
   u prethodnim godinama". Nalog vidi 2026 i 2025; za dublje treba SELECT na starije.
2. Dobavljači ne paze na padež: `587FN-2026` glasi „po **racun** broj 2082 VP-2026".
   Prva verzija regexa je tražila „računu/fakturi" i promašila ih.

### Zapisana odluka zastareva (24.09.)

Poređenje: gde app šalje knjižno odobrenje naspram toga gde ga je čovek stvarno
stavio, na **877** odobrenja koja postoje i u panelu i u ERP-u:

| | |
|---|---|
| **zapisano u bazi panela** | 318 (36,3 %) |
| **današnji motor, preračunato** | **818 (93,3 %)** |

Razlika nisu pravila nego **stare odluke**: dokumenti razvrstani pre ispravke koja
knjižna odobrenja prevodi na 010 i dalje nose maloprodajnu jedinicu. Kod odobrenja
to nije kozmetika — kalkulacija bi otišla u `MKALKUL 002` umesto na 010, pa roba
nikad ne bi sišla sa stanja.

Zato `razvrstaj --odobrenja` preračunava samo tu vrstu. `--sve` i dalje dira sve,
pa i dokumente koje je operater već prosledio; kod odobrenja je rizik obrnut od
uobičajenog, jer pogrešna PJ ostavlja manjak koji se ne vidi kao greška.

Šta i danas promaši (59 od 877): 25 nerazvrstanih koja idu na 010, 16 sa 088 na
010, 11 sa 010 na 088 (finansijska odobrenja bez robe), 6 nerazvrstanih na 088.

**Pouka o merenju:** prvo sam izmerio 36 % i skoro javio da razvrstavanje ne valja.
Broj je bio tačan, ali je merio zatečeno stanje baze, a ne ponašanje koda. Kad se
meri „da li app radi kako treba", mora se pustiti kod, a ne čitati šta je zapisano
od ranije.

### Povrat ili popust — razdvaja ih iznos (24.09.)

Bane: „odobren popust za šporet GK6C62XC … to nije povrat robe, već verovatno
dodatni rabat zato što je uređaj stigao oštećen; takav dokument treba da završi u
088, ali da Ilijana bude obaveštena, jer taj popust dalje prenosi u MPO koji
prodaje takav uređaj. Ovo u slučaju da popust nije u kompletnom iznosu vrednosti
— ako je ceo iznos, onda nije oštećenje nego smo razduženi za ceo aparat."

Mereno na odobrenjima čija napomena imenuje fakturu:

| odnos osnovice i nabavne | šta je | koliko |
|---|---|---|
| **jednako** | razduženi za ceo aparat → povrat | **61** |
| **manje** | oštećenje/popust → 088 | **5** |
| veće | ne odnosi se na taj uređaj | 1 |

**Razvrstavanje ovo već radi kako treba** — sva četiri proverena popusta i app i
čovek šalju na 088 (nemaju vezu ka fakturi). Pravilo o iznosu zato ne dira
razvrstavanje nego **predlog artikla**.

Time je nestao i jedini stvarni promašaj od 52 provere: `568FN-2026`, odobrenje na
25.066,44 za uređaj čija je nabavna 18.108,40. Kod ga je prihvatio jer je faktura
imala jednu stavku — **bez provere iznosa**. Sada se predlog daje samo kad se iznos
poklopi; kad je manji, to je popust i artikal se ne predlaže.

**Ostaje nerešeno:** popust završi na 088, gde ga Ilijana ne vidi, a treba da ga
prenese u objekat koji prodaje oštećen uređaj.

### Popust za prenos — ekran i mejl (24.09.)

Odobrenje na **deo** vrednosti uređaja ide na 088 kao finansijsko i tamo se gubi.
Ilijana je ta koja taj popust prenosi objektu koji oštećen uređaj prodaje, a na
088 ga ne vidi. Bane je tražio oba puta: mejl da sazna, ekran da ostane trag.

Kako radi: kad se nacrt osvežava, odobrenje koje ide na 088 se proverava — ako
napomena imenuje fakturu i odobreni iznos je **manji** od nabavne tog uređaja,
upisuje se u `popust_za_prenos` i magacin povraćaja dobija mejl.

Ekran `/popusti` vidi magacin povraćaja i operateri, ne i poslovođe: njima se
popust prenosi, ne oni njega prenose. Zapis ostaje otvoren dok Ilijana ne upiše
**u koji objekat** je prenet; tada dobija vreme, ime i napomenu.

U navigaciji stoji broj otvorenih, jer ekran koji se ne otvara ne pomaže.

**Šta se namerno ne radi:** app ne odlučuje kom objektu popust pripada. Uređaj se
prodaje u više objekata, a koji ga je preuzeo oštećenog zna Ilijana.

### Četvrti tok proveren: povrat na 010 (24.09.)

`ULKALKUL 010/00494`, knjižno odobrenje `Z-113/26`, jedna stavka. Poređenje svih
kolona naspram 200 zapisa koje je ERP sam napravio na 010 izlazi prazno — osim
`PROKNJIZENO` (naša 0, stari uzorci zaključani 2), što **mora** da ostane različito.

Provere koje važe baš za povrat, sve prolaze:

| | |
|---|---|
| količina negativna, **nabavna pozitivna** | ✓ |
| `IDPDV` 35 („Povrat robe") | ✓ |
| osnovica, PDV i ukupno negativni u zaglavlju | ✓ |
| zbir stavki = zaglavlje; osnovica + PDV = za naplatu | ✓ |

Usput je proveren i rabat, koji je Bane tražio da se uračuna: `FAK_CENA` 330.833,33,
`RABAT_PROC` 15, `NAB_CENA` 281.208,33 — tačno 85 % bruto cene.

**Kako se razlikuje naš zapis od ljudskog:** RBR se završava sa `sefsync` (servis ne
prosleđuje prijavljenog korisnika, `KORISNIK` je konstanta) i `PROKNJIZENO` je 0.
Dva puta smo tokom provere gledali ljudske zapise misleći da su naši — `010/00484`
(Ilijana) i `010/00493` (Bogdan). Zato provera uvek gleda ta dva polja prvo.

### Sva četiri toka

| tok | zapis | posebno provereno |
|---|---|---|
| usluga | `ULKALKUL 088/02530` | podela PDV evidencije (TIP 1 + 23), bez stavki |
| veleprodaja | `ULKALKUL 021/00227` | tarifa, RBR, rabat po stavkama, `IDPDV` |
| maloprodaja | `MKALKUL 008/00466` | marža, `MALACENA`, `MPVRED1/2`, `UKAL_PDV1/2` |
| **povrat** | `ULKALKUL 010/00494` | predznaci, `IDPDV` 35, rabat |

### Starije godine su dodale 17 povrata

Posle grantova za 2024, 2023 i 2022 (Bane, 24.09.) pretraga vidi pet godina:

| | 2 godine | 5 godina |
|---|---|---|
| povrat staje, **ima predlog** | 53 | **70** |
| povrat staje, nema predloga | 182 | 165 |

Pokrivenost 23 % → 30 % od 235 povrata koji stanu.

**Tačnost posle svega: 68 od 69** (provereno naspram artikla koji je čovek uneo).
Jedini neslaganje je dupliran artikal — `84655 Gorenje RIU 609 EA1` i
`95961 GorenjeRIU609EA1` su isti uređaj pod dve šifre. Pravi promašaj
(`568FN-2026`) je nestao kad je uvedena provera iznosa.

Isti uređaj pod dve šifre nije samo kozmetika: stanje se deli na dva mesta, pa ni
jedno ne pokazuje koliko komada stvarno ima. Vredi pogledati koliko je takvih
parova u šifarniku — zaseban posao.

## Barkod koji se ne poklapa (25.09.)

Bane traži rutinu za slučaj kad isti artikal stigne sa drugim barkodom: jedan
barkod po artiklu je pravilo, pa se lager mora prelepiti. Prvo koliko je toga.

Od **15.860** stavki sa barkodom, uz zanemarene vodeće nule (`0616320532451` i
`616320532451` su isti kod):

| | |
|---|---|
| isti barkod | 15.375 |
| kod nas interna šifra umesto EAN-a | 231 stavki, **31 artikal** |
| na fakturi nije EAN | 154 |
| **prava promena** | 100 stavki, **12 artikala** |

**Interna šifra nije greška nego pab.** Svih 31 su pića — Coca-Cola, Next, Stella,
Plantaže Vranac, Rubin Cabernet. Bane: „kod pab-a imaš proizvodnju, od coca cola i
crnog vina proizvodiš Bambus i slično". Proizvod koji sam praviš nema fabrički
barkod, pa artikal nosi internu šifru. Ta grupa ispada iz rutine.

**Pravih promena je 12 godišnje**, i to su uglavnom Xiaomi telefoni, Karcher, TCL,
Gree klima — dakle dobavljač pod istom svojom šifrom šalje drugu varijantu. Za to
već postoji zaštita: kad se ista šifra dobavljača veže za drugi artikal, mapiranje
se **gasi** umesto da se prepiše.

### Predložena rutina (nije napravljena)

Kad Dule poveže šifru, a barkod sa fakture se razlikuje (oba prava EAN-a):

1. **nov barkod je ispravan** → zadatak „prelepiti lager“ svakom objektu koji ima
   stanje tog artikla, sa starim i novim kodom; stoji otvoren dok objekat ne potvrdi
2. **ostaje stari** → zadatak samo objektu kome je roba stigla, za nove komade
3. **nije isti artikal** → ne povezuje se, otvara se nova šifra

**Panel ne bi pisao u `ARTIKLI`.** Upis u ERP je do sada ograničen na kalkulacije,
koje su proverene polje po polje; menjanje šifarnika je druga vrsta rizika. Barkod
menja Dule u ERP-u, panel vodi prelepljivanje i trag.

## Prelazak na novi način rada — 28.09.2026. u 8 h

Odluke (Bane, 25.09.): **svi odjednom**, app **upisuje kalkulacije od prvog dana**,
a ono što je stiglo ranije završavaju ljudi u ERP-u kao i do sada.

### Zašto granica mora da bude u kodu, ne u uputstvu

Posle `nacrti --osvezi` na serveru je stajalo **46 spremnih** kalkulacija za fakture
koje niko nije zaknjižio. Jedan klik i ERP bi dobio kalkulaciju za robu koju je
poslovođa odavno primio i proknjižio na svoj način. Zato `NOVI_TOK_OD=2026-09-28`:
dokument stigao pre tog dana nema ni potvrdu prijema ni upis, ni u prikazu ni u
rukovaocu.

### Šta je već pokriveno

`nacrti --osvezi` je prepoznao **197 kalkulacija koje su ljudi napravili ručno**
(„upisan“ je skočio sa 7 na 204) i označio ih kao urađene. Njih niko neće praviti
ponovo. Brana pri upisu (`postojeca_kalkulacija`) i dalje stoji kao druga linija.

### Redosled

**Nedelja 27.09.** — paket, `nacrti --osvezi`, `provera-prelaska`, grant na
`ELBS_2026`, upisati `NOVI_TOK_OD=2026-09-28`. Probni režim i produkcioni upis
**ostaju isključeni** — panel stoji spreman, ništa se ne dešava.

**Ponedeljak 28.09. u 8 h, dok je Bane tu** — obrisati `NOTIFY_OVERRIDE_TO`,
uključiti `ERP_ALLOW_PRODUCTION_WRITE`, restart preko `restart.ps1`, pa
`provera-prelaska` mora da kaže „Sve stoji“. Prvi upis zajedno, na jednoj uslužnoj
fakturi (088) — nema stavki ni marže, najmanje toga može da zaškripi.

**Isti dan** — Nina i Dušan arhiviraju zaostalo iz reda, osim onoga za šta roba tek
treba da stigne.

## Tri nalaza sa Bogdanovog ekrana (26.09.)

Bogdan je prijavio da mu fakture koje je već zaknjižio i dalje stoje na ekranu.
Mereno na **ELBS_2026** — čitanje, a to je i ekran i provera duplikata, ionako
ide na produkciju; na ELBSX ide samo upis, koji je još zabranjen.

### 1. Broj računa odsečen na 15 znakova

Kolona `BROJ_RN_DOB` je `varchar(20)`, ali ERP-ova forma pušta **15 znakova**:
`K110/2026-835249` stoji kao `k110/2026-83524`. U bazi je **567 zapisa tačno na
toj granici, dužih nema nijedan**, i svih 567 parova (dobavljač, broj) je
jedinstveno — **nula sudara**.

Neograničeno poređenje „po početku“ bi napravilo **9 lažnih pogodaka**
(`7016088538` je početak od `70160885380120`, dve različite fakture istog
dobavljača). Zato uslov „ERP vrednost je tačno na 15 znakova“ mora da ostane, uz
najmanje 10 znakova poklapanja i **tačno jednog kandidata**.

Vraća **51 dokument** koji sada stoji na ekranu.

### 2. Dobavljač otvoren dvaput u komitentima

Šuleks: `04808 Šulex d.o.o` (bez matičnog broja, **4 kalkulacije**) i
`15582 Sulex DOO za trgovinu i usluge` (MB 08734054, **0 kalkulacija**), oba sa
PIB-om 100273149. App po matičnom broju bira 15582 i promaši sve četiri.

Razmere: **1.339 PIB-ova deli 3.075 šifara**, 139 matičnih brojeva deli 295
šifara. Šifarnik se ne dira — definicija tabele u ERP-u nije naša — nego
`sestrinske_sifre` pri proveri gleda celu grupu. Popune (`123456789`, same nule)
se preskaču.

U uzorku od 400 dokumenata sa ekrana našlo **4 stvarna slučaja** (Radmilo
Aleksić PR Sky, kalkulacije pod šifrom 46966), dakle oko 1 %.

### 3. Ljudska greška u broju — ostaje na odluku

Bogdan je uneo `9999` gde faktura ima `999`. Predlog je bio „isti iznos, isti
datum“, ali kao samostalno pravilo ne valja: **482 puta** u istoriji dve
različite fakture istog dobavljača imaju isti datum i isti iznos, a ako se doda
i „broj se razlikuje u jednom znaku“ broj sudara **raste na 1.717** (uzastopni
povrati `263X3000817` ~ `263X3000818`).

Uže: traži se samo među kalkulacijama koje **nisu** sparene ni sa jednim SEF
dokumentom. Tada je od **1.409 grupa dvosmisleno svega 13 (0,9 %)** i pravilo
nalazi **54 dokumenta**. Među njima su prave greške u kucanju —
`IF-2026-1044-001` → `IF-2026-1044-01`, `26-30F-003776` → `20-30F-003776`,
`3086744173` → `9086744173` — ali i promašaj: `BK6164069/26` → `bk6170838/26`,
bankarska naknada od 234 dinara, isti iznos istog dana, druga faktura.

Zato ovo **ne sme da skloni fakturu samo**. Lažan pogodak bi je tiho izbrisao sa
ekrana i niko je ne bi zaknjižio — gore od Bogdanove smetnje.

### Šta je urađeno

`postojeca_kalkulacija` vraća `Pogodak` — kratku oznaku (ide u `erp_broj`,
`varchar(20)`) odvojeno od objašnjenja (`erp_napomena`). Nalazi i odsečen broj i
kalkulaciju pod sestrinskom šifrom.

`moguca_kalkulacija` vraća predlog sa `siguran=False`. Uz njega ide ocena broja
rečima — „isti broj, kraće zapisan“, „razlika u jednom znaku“, „poklapa se N
znakova“, „broj se ne poklapa“ — jer se pokazalo da razlike skoro nikad nisu
greške u kucanju nego skraćen zapis: METRO šalje `0/0(044)0002/143059/26`, u
ERP-u stoji `143059/26`.

Predlog se na ekranu vidi kao žuti okvir sa dva dugmeta, „Jeste, skloni“ i
„Nije to“. Obe odluke ostavljaju trag (`sumnja_odbio`, `sumnja_odbijeno_u`),
odbijen predlog se ne vraća pri sledećem osvežavanju. Komanda `sumnje` ispisuje
sve odjednom.

Provereno na 90 poslednjih Bogdanovih faktura kroz stvarni tok: **84 app sama
prepozna**, 2 dobiju predlog, 4 ostaju za knjiženje.

## Manjak pri isporuci — analiza (26.09.)

Operater prosledi fakturu, poslovođa otvori kamion i vidi da je stigao jedan
aparat manje. Bane je opisao tri slučaja iz prakse. Koliko su česti:

| | broj | udeo |
|---|---:|---:|
| odbijeno na SEF-u, čeka se nova faktura | 7 | 0,09 % |
| dobavljač stornira i odmah šalje ispravljenu | 32 | 0,4 % |
| prihvata se, ostatak stiže sledećom turom | — | ostalo |
| knjižnih odobrenja (kad ostatak ne stigne) | 967 | 12,5 % |

**Polje za napomenu uz prijem je upotrebljeno 0 puta u 7.727 dokumenata.**
Slobodan tekst se ne popunjava, a i da se popuni, ni upis kalkulacije ni
kasnije knjižno odobrenje ne mogu ništa s njim da urade. Zato manjak mora da se
prijavljuje po stavkama, a ne rečenicom.

### Zašto se kalkulacija ipak upisuje za pun iznos fakture

Kalkulacija nosi PDV i obavezu prema dobavljaču. Kalkulacija za manju količinu
ne slaže se ni sa `PDVEVIDX` ni sa saldom dobavljača, a kad ostatak stigne nema
uz šta da se knjiži — nova faktura ne postoji. Cena toga je da zaliha jedan do
dva dana pokazuje aparat kojeg nema. Alternativa kvari tri stvari odjednom.

Kod slučajeva 1 i 2 faktura nestaje, pa se **ne upisuje ništa**.

### Rupa nađena pri analizi

Nijedna provera u putu do kalkulacije ne gleda `sef_status`. Danas je
bezopasno — nijedan od 32 stornirana dokumenta nema potvrđen prijem — ali od
28.09. app upisuje, pa bi storno posle potvrde prijema ostavio kalkulaciju za
poništenu fakturu. Treba dve brane: upis odbija dokument u stanju `Storno` ili
`Rejected`, a već upisana kalkulacija čija faktura naknadno bude stornirana
diže se operateru na ekran. App ništa ne briše iz ERP-a sama.

### Odštampan primerak e-fakture

Bogdanu i drugima ponekad treba faktura na papiru. SEF-ov klijent već ima
`purchase_pdf` (GET /api/publicApi/purchase-invoice/pdf, prošireni prikaz
dokumenta), ali nigde nije izložen. Treba dugme „Štampaj“ na stranici
dokumenta — štampa se SEF-ov PDF, ne naš prikaz.

### Otvoreno pitanje

Rok za prihvatanje na SEF-u se nigde ne prati. Ako dokument stoji dok dobavljač
ne odluči, neko mora da gleda sat.

### PROKNJIZENO — potvrđeno značenje (Bane, 26.09.)

- **0** — kalkulacija nije proknjižena. Jedina vrednost koju aplikacija sme da upiše.
- **1** — proknjižio poslovođa.
- **2** — proknjižena pa je administrator zaključao da ne može unazad da se menja,
  zbog obračuna PDV-a. Dakle i 2 je zaknjiženo, nije „samo zaključano“.

Nule nema u ELBS_2026 (samo 211 jedinica iz poslednjih dvadesetak dana i 8.630
dvojki kroz celu godinu) zato što ljudi knjiže odmah po unosu, a ne zato što je
nula pogrešna. Naših 21 zapis sa nulom u ELBSX-u su ispravni i nema šta da se
proverava na ERP-ovom ekranu pre prelaska.

### Ispravka analize manjka

Ranije je ovde pisalo da zaliha dan-dva pokazuje aparat kojeg nema. **To nije
tačno.** Upis dira tačno tri tabele — zaglavlje, stavke i `PDVEVIDX` — a
`ARTPROD` i `LagerSnapshotDaily` ne dira nikad. Zaliha se pomera tek
knjiženjem, koje radi čovek. Puna kalkulacija upisana odmah zato ne košta ništa
na lageru: poslovođa je prosto ne knjiži dok ostatak ne stigne, a oznaka
„čeka se ostatak“ mu govori kad sme.

### Urađeno pred prelazak

**Brana za povučene fakture.** `upisi_u_erp` odbija dokument u stanju `Storno`
ili `Rejected`. Do sada je ovo bilo bez posledica — nijedan od 32 stornirana
dokumenta nema potvrđen prijem — ali od 28.09. app upisuje, pa bi storno koji
stigne posle potvrde prijema ostavio kalkulaciju za poništenu fakturu.

**Dugme „Štampaj“** na stranici dokumenta. Vraća SEF-ov zvanični prošireni
prikaz (`purchase-invoice/pdf`), ne naš ekran — na papiru treba da stoji ono
što je dobavljač poslao. Vidi ga samo onaj ko sme na taj dokument.

### Manjak pri isporuci — urađeno (26.09.)

Poslovođa ispod „Roba je primljena“ ima **„Stiglo manje nego što piše“**: spisak
stavki sa fakture, polje „stiglo“ već popunjeno fakturisanom količinom, pa tri
dugmeta za ono što je vozač rekao. Upisuju se samo redovi koji se razlikuju;
količina veća od fakturisane se odbija — ovo je manjak, ne višak.

| izbor | kalkulacija | ekran |
|---|---|---|
| dovoze sledećom turom | **upisuje se za pun iznos** | crvena linija „ne knjiži dok ostatak ne dođe“ + kartica „Čeka ostatak“ |
| šalju ispravljenu fakturu | ne upisuje se | čeka se storno |
| ne znam | ne upisuje se | ide operateru u karticu „Prijavljen manjak“ |

Zatvara se na tri načina: poslovođa klikne „Stigao ostatak“, stigne knjižno
odobrenje koje ispravlja tu fakturu (zatvara se samo, provereno po dobavljaču i
normalizovanom broju), ili stigne storno. Sve tri ostavljaju trag u
`prijava_manjka` i u `audit_log`.

Bez podsetnika na mejl — Bane, 26.09.: „za sad je dovoljna crvena linija na
ekranu“.

Oba uputstva su ispravljena; poslovođama je do sada pisalo „ako nešto fali, ne
klikaj, javi operateru“, što bi od prvog dana protivrečilo aplikaciji.

### Pomoć na ekranu (26.09.)

`/pomoc`, veza stoji u zaglavlju svakog ekrana. Sadržaj po ulozi — kancelarija,
objekat, povrati (010), ulazni računi (088) — plus deo koji važi za sve
(štampa, „šta aplikacija nikad ne radi sama“, granica prelaska).

**Pravilo koje je Bane postavio 26.09.:** uz svaku izmenu u načinu rada ide i
ažuriranje pomoći, u istom poslu. Zastarela pomoć je gora nego nikakva, jer joj
čovek veruje — što se već desilo sa uputstvom poslovođama, koje je dan pred
prelazak govorilo suprotno od aplikacije.

**Odštampana uputstva su povučena** (Bane: „neću štampati, nema potrebe ako ga
imaju u app sad“). Sve što je stajalo samo na papiru preneto je u `/pomoc`: šta
poslovođa više ne radi, reklamacije, odakle dolaze dokumenti i oznaka
„nepregledano“, šta aplikacija uradi sama u kalkulaciji, upozorenje da se isti
dokument ne radi dvaput, i tabela „ako nešto zaškripi“. Fajlovi u
`docs/uputstva/` nose baner da se više ne održavaju i ostaju samo kao trag.

## Veza po barkodu (30.09.)

Dule se žalio: neko slika, on otvori šifru i poveže je na jednoj fakturi, a
ostale i dalje stoje sa „čeka šifru“.

Propagacija je postojala — `povezi_sifru` osvežava sve nacrte koji čekaju isti
artikal — ali traženje šifre je gledalo **samo šifru dobavljača**:

```
1. mapiranje po (PIB + šifra dobavljača)
2. ERP-ov šifarnik ARTIKLI po barkodu
```

Kolona `ItemMapping.barcode` se **upisivala a nikad nije čitala**. Zato je veza
vredela samo za istog dobavljača i istu njegovu šifru; ista roba od drugog
dobavljača, ili sa promenjenom šifrom dobavljača, i dalje je čekala.

Dodat je treći put, između ta dva: **mapiranje po barkodu**. Mereno na 2.110
aktivnih veza — 1.872 ima upisan barkod, 1.867 različitih, i **nijedan barkod
ne vodi na dve šifre**, pa je traženje jednoznačno. Ako se ikad pojavi
dvosmislen, app ne bira nego vraća „čeka čoveka“.

## Obaveštenje o prethodnom porezu (30.09.)

Uz svako knjižno odobrenje kupac mora dobavljaču da javi koliko PDV-a nije
koristio kao prethodni porez. Na portalu je to dugme „Kreiraj obaveštenje o
prethodnom porezu“ na samom dokumentu.

**API to pokriva u celosti:** `POST /api/publicApi/recipients-notice-on-input-vat/sender/send`,
uz `GET` za pregled, PDF i spisak onih koja su pala pri slanju.

**Prva analiza je bila pogrešna** i dala 21 % pokrivenosti: „Broj dokumenta o
smanjenju“ sam čitao kao broj originalne fakture. Slike sa portala su pokazale
da je to **broj samog odobrenja**, a datum uz njega datum tog odobrenja —
original se ne traži uopšte. Sa ispravnim čitanjem, od 967 odobrenja može se
poslati **966**; jedino koje ne može je ono sa nultim PDV-om, jer propis traži
iznos veći od nule.

| polje | odakle |
|---|---|
| `noticeNumber` | broj odobrenja, doslovno (Bane: „bukvalno 26-3X3-002855“) |
| `totalVatAmount` | PDV sa odobrenja, uvek pozitivan |
| `documentIssueOrigin` | `DocumentIssuedViaSef` |
| `basisOfNotice` | `CreditNoteIssued` |
| `documentReferenceType` | `...ToInvoice` (947) ili `...ToPeriod` kad pokriva više faktura (20) |
| `relatedDocumentNumber` / `...IssueDate` | broj i datum **odobrenja** |
| `recipient` | PIB dobavljača |

**Šalje se isključivo uz prihvatanje odobrenja na SEF-u** — Bane, 30.09.: „to
treba da ode prilikom prihvatanja sa sefa, u tom trenutku za odgovarajuće k.o.,
ne treba masovna komanda“. Prihvatanjem se PDV i ispravlja, pa je to i pravno
tačan trenutak.

Neuspeh slanja **ne poništava prihvatanje** — ono je već prošlo na SEF-u. Trag
o padu ostaje na dokumentu, crveno, sa uputstvom da se obaveštenje napravi
ručno na portalu. `provera-prelaska` broji prihvaćena odobrenja bez obaveštenja.

## Prva dva radna dana — 29. i 30.09.

### Panel je dvaput pao na 502

Prvo sam podigao pogrešan bazen. Poruka `QueuePool limit of size 5 overflow 10
reached` dolazi od **ERP** veze, koja je išla sa podrazumevanim bazenom, a ja
sam povećao bazen baze panela. Sutradan je palo isto.

Pravi uzrok je nađen tek kad sam prestao da nagađam i ugradio merač: `/health`
sada pokazuje stanje oba bazena i **ko drži vezu** — mesto u kodu i koliko
dugo, bez uzimanja ijedne nove veze, jer mora da radi baš kad je bazen pun.

Merač je odmah pokazao tri zahteva na `/red` koji drže vezu **43–55 sekundi**.
Brojanje je išlo kao `count` nad podupitom koji izvlači **sve kolone svih
redova**, i to sedam puta po otvaranju stranice — jednom za ukupno i jednom za
svaku karticu. Uz to je sinhronizacija držala dve veze odjednom, jer je moje
zatvaranje manjka knjižnim odobrenjem otvaralo svoju sesiju usred ciklusa.

Posle popravke: `u_upotrebi 0`, nijedan upit preko dve sekunde.

### Prebacivanje dokumenata

Mereno 29.09.: **49 dokumenata** je ispravljeno pošto su već bili prosleđeni, 2
i pošto je prijem potvrđen — a nijedna nova jedinica nije o tome obaveštena.

Sada, kad se jedinica stvarno promeni: nova dobija obaveštenje, tuđa potvrda
prijema se poništava, a ako je kalkulacija već upisana ostaje u starom magacinu
uz crveno upozorenje sa njenim brojem. Ponovna dodela **iste** jedinice ne šalje
ništa, pa „zapamti pravilo“ ne spamuje.

Uz to dugme **„Nije za moju jedinicu“**: čovek vraća dokument kancelariji sa
napomenom, umesto da javlja telefonom bez traga.

### Pravila razvrstavanja su se međusobno borila

Kvačica „zapamti kao pravilo“ gledala je postoji li isto pravilo za **istu**
jedinicu, pa je za isti obrazac a drugu jedinicu pravila još jedno. Posledica:
adresa `SRPSKIH VLADARA 46, 25260, APATIN` vodila je na **deset** jedinica, isti
PIB `103699024` na osam. Ko prvi pogodi, taj odlučuje.

Očišćeno 77 mrtvih pravila (`pravila-sukobi`), a **26 pravila po PIB-u za usluge
podignuto na prioritet 30**, iznad adresnih na 50 — jer motor uzima **prvo**
pravilo koje pogodi, pa je račun za struju na adresi prodavnice odlazio u
prodavnicu. `objasni <id>` pokazuje koja pravila pogađaju jedan dokument i koje
odlučuje.

**Greška u analizi:** tvrdio sam da opšte ključne reči („rabat“, „promet“)
odvode fakture za robu u Upravu. Ne odvode — ta pravila već imaju
`document_type = CreditNote` i motor ih na fakturi preskače. Zaključak je došao
iz `objasni`, koja je imala **svoju kopiju uslova** i u njoj ispustila proveru
vrste. Motor sada izlaže `pogodjena()`, koju koriste i odlučivanje i
objašnjenje.

### Fakture koje su tiho stajale

Potvrda prijema pokušava upis odmah. Ako nacrt tada nije spreman — najčešće
stavka bez naše šifre — upis se odbije, a kad se šifra kasnije otvori **niko ne
pokreće upis ponovo**. Taj put je uz to bio jedini bez ikakvog traga.

Sada: razlog se upisuje u dnevnik i ostaje crveno na dokumentu, komanda
`dovrsi-upise` nalazi takve i dovršava ih, a **petnaestominutni ciklus
preračunava zapele nacrte** — šifra se otvara u ERP-u, van panela, pa panel mora
sam da primeti da je stavka odmrznuta.

### Tri greške oko veza šifara

1. **Barkod se upisivao a nikad nije čitao.** Veza je vredela samo za istog
   dobavljača i istu njegovu šifru.
2. **Naziv nije bio ključ.** Vitel šalje stavke bez šifre i bez EAN-a („LEXA BT
   ZVUCNIK LX-302 NITRO“), pa veza nije imala za šta da se zakači.
3. **Prazan ključ je gasio prethodnu vezu.** Ključ je bio `(dobavljač, prazno,
   prazno)` — isti za svaki takav artikal — pa je svako novo povezivanje ubijalo
   prethodno kao „kolebljivo“, a novo nije ni pravilo. Dule je povezao sve LEXA
   zvučnike i ostao bez ijedne žive veze.

Uz to: ponovno povezivanje **iste** šifre sada oživljava ugašenu vezu (ranije je
čovek mogao da klikće bez efekta), a `ozivi-veze` vraća ono što je ugašeno
greškom — samo gde naziv vodi na tačno jednu šifru.

Mereno: od 2.057 parova (dobavljač + naziv) **24 vode na više šifara**, svi od
dobavljača koji šalje opšte nazive tipa „MASINA ZA PRANJE VESA“ (sedam mašina).
Zato se po nazivu odlučuje samo kad je pogodak jedan.

### Slika sa telefona

Odbijali smo fajl po onome što **pregledač kaže** da šalje. Android pošalje
`application/octet-stream` kad se bira iz menadžera fajlova, pa Bečej nije mogao
da pošalje sliku. Vrsta se sada prepoznaje po prvim bajtovima samog fajla.

### Sitnije

* Magacin povrata nije imao mejl **na poslovnoj jedinici**, ali ga ima u
  šifarniku osoblja uz Ilijanu i objekat 010. Dispečer sada, kad jedinica nema
  svoj mejl, čita adrese iz šifarnika po šifri objekta. Šifarnik se i dalje samo
  čita.
* Prazan spisak primalaca više ne prolazi nemo — upisuje se upozorenje sa imenom
  jedinice.
* Naslov mejla je skraćen i bez znakova `—` i `·`: svaki takav znak preseca
  naslov na još jedan `=?utf-8?q?…?=` odsečak, a Gmail na Androidu ih loše
  spaja. Kodiranje je oduvek bilo ispravno.
* Zaustavljene kalkulacije se broje uz **Šifre** u navigaciji; objašnjenje uz njih
  imenovalo je retke uzroke, a sva četiri zatečena slučaja su ista: *artikal
  nema maloprodajnu cenu u tom objektu*.
* `dokument <broj>` ispisuje sve o jednoj fakturi: gde je, dokle je stigla, šta
  drži kalkulaciju i šta se sve sa njom dešavalo.

## ID PDV evidencije (30.09.)

Bogdan je do sada ručno menjao ID PDV evidencije na skoro svakoj kalkulaciji
koja nije običan ulazni račun. Aplikacija je u zaglavlje pisala `LASTUSER: 1` —
napisano je tako još u prvoj verziji generatora, u uverenju da je to *id
korisnika*. Ime kolone vara: **`ULKALKUL.LASTUSER` i `MKALKUL.LASTUSER` drže ID
PDV evidencije**, šifarnik je `dbo.TIPEVID`. Provereno na 088/00011 (OTP banka):
`LASTUSER 75`, a u `PDVEVID` mu odgovara red `ULK880011` TIP 75 „Provizije,
takse“.

Da je vrednost bila 1, a ne 0, otkrilo se tek pri merenju — ranija tvrdnja da
app „ostavlja nulu“ bila je pogrešna. Praktična posledica je ista: pogađala je
samo obične ulazne račune.

### Šta se vidi iz same fakture

Mereno na celoj `ELBS_2026`, 8.939 kalkulacija sa upisanom oznakom:

| pravilo | poklapanje |
|---|---|
| magacin 010 → **35** (povrat robe) | 600 od 603; pozitivne kalkulacije na 010 **ne postoje** |
| negativno na 088 → **34** (finansijsko odobrenje) | 368 od 370, sve bez stavki |
| račun **sa** PDV-om → 1 ili 21 | 1.255 : 408 : 14 (šest); **nikad** 75 ni 23 |
| račun **bez** PDV-a → 75 ili 23 | 921 : 168 : 2; **nikad** 21 |

Poslednja dva reda su ono što nismo videli iz prve: „bez PDV-a“ ne znači 23.
Banke i PTT naplaćuju bez PDV-a i to je 75, a ne 23 — 921 prema 168.

### Šta se uči iz istorije

Za račune bez PDV-a bira se ono što je čovek ranije birao kod tog dobavljača.
Spisak se ne održava rukom; čita se iz `ULKALKUL` pri svakoj kalkulaciji.

Prvi pokušaj je tražio da dobavljač **uvek** bude ista vrsta. Previše krto:
Banca intesa ima 323 puta 75 i po jednom 1 i 23 — dve omaške u 325 zapisa
blokirale bi ceo dobavljač. Sada je prag „bar 3 puta i bar 90 % istorije“:

| prag | pogođenih 75 | ukupno |
|---|---:|---:|
| uvek ista vrsta | 688 / 921 | 92,6 % |
| **≥ 3 i ≥ 90 %** | **887 / 921** | **94,8 %** |
| ≥ 3 i ≥ 85 % | 888 / 921 | 94,8 % |

Ispod 90 % se više ništa ne dobija. Danas app pogađa 71,9 % (jer piše 1 svuda).

Mereno pošteno: svaka kalkulacija se pogađa samo iz onih koje su pre nje po
datumu, pa je 887 donja granica — banke koje su u istoriji već naučene pogađaju
se od prvog dana.

### 21 se nikad ne bira sam

Pravo na odbitak PDV-a zavisi od toga šta je kupljeno i za šta se koristi, a to
se iz fakture ne vidi. Isti dobavljač ide na oba: NIS 88:63, Lukoil 63:54, Euro
petrol 58:40, Sinagoga 15:14. Zato app upisuje 1 i ostavlja žutu napomenu sa
brojkama („ranije 58 puta 21, 40 puta 1“).

Napomena bi stajala na 859 kalkulacija godišnje — oko 3–4 dnevno — i pokriva
**svih 408** stvarnih dvadesetjedinica. Prag „bar dva puta 21“ smanjio bi broj
napomena na 672, ali bi propustio 41 pravu. Zato ostaje prag jedan.

Napomena je žuta i ne zaustavlja upis (crveni okvir „Proveri u ERP-u“ i dalje
znači da je nešto palo). Čuva se u `nacrt_kalkulacije.napomene` i preživljava
uspešan upis — tada i treba da se vidi, jer se knjiži posle.

Komanda za popravku ranijih kalkulacija nije pravljena: Bogdan ih je već ručno
ispravio (Bane, 30.09.).

## Povezivanje šifre na odobrenju nije radilo ništa (01.10.)

Ilijana je na KIM-TEC odobrenju povezala Vivax (naša šifra 90911), dobila
zelenu poruku „Šifra 90911 je povezana i zapamćena“ — i ostala na istom ekranu,
sa „čeka šifru“ i bez dugmeta za upis.

Lanac:

1. Na knjižnom odobrenju veza se pamti **samo kad čovek čekira** „zapamti vezu“.
   Tako je i traženo: EWE šalje „USL00027“ na 109 odobrenja, Eurotehna
   „Porez 20%“ na 107 — zapamćena veza bi svakom sledećem odobrenju tiho dala
   uređaj od prošlog puta.
2. Kvačica je prazna po podrazumevanom, pa `pamti=False`.
3. `povezi_sifru` tada upiše šifru na **stavku** (`DocumentLine.erp_item_code`,
   izvor „rucno“) i ne napravi nijednu vezu.
4. `_nasa_sifra` tu kolonu **nikad nije čitala** — gledala je samo
   `item_mapping`. Osvežavanje nacrta vrati istu stavku među nemapirane.

Dakle: bez kvačice povezivanje nije imalo nikakvo dejstvo, a poruka je tvrdila
da jeste. Ista zamka je čekala i poslovođe — tamo je podrazumevano obrnuto
(`ne_pamti`), pa se nije videla.

Popravljeno na dva mesta:

* `_nasa_sifra` sada **prvo** uzima ono što je čovek rekao za tu stavku
  (`erp_mapping_source == "rucno"`). To je pouzdanije od svake naučene veze i
  vredi i kad se veza ne pamti. Usput rešava i slučaj kad je prethodna veza
  ugašena kao „kolebljiva“ — tada se nova nije ni pravila, pa ni ta faktura
  nije prolazila.
* Poruka razlikuje „povezana i zapamćena za ovog dobavljača“ od „povezana samo
  na ovoj fakturi“.

Test koji to drži: `test_povezana_sifra_bez_pamcenja_ipak_vazi_za_tu_fakturu`
(pada na staroj verziji).

## Dobavljač koji postoji, a ne može da se nađe (01.10.)

Četiri zaustavljene kalkulacije, dva različita uzroka.

**BCE GROUP DOO ČAČAK (MB 21587516)** — stvarno nije otvoren. Nula pogodaka u
sve četiri kolone po kojima se traži (`MATBROJ`, `ZIRORACUN`, `BPG`, `TEKUCI`),
ni po imenu. Dve fakture, 169.214 i 35.253. Otvara ga čovek u ERP-u.

**JKP Komunalac Kula — postoji, analitika `00709`, osam kalkulacija.** Ne nađe
se zato što dva promašaja padaju jedan na drugi:

| gde | na fakturi | u ERP-u |
|---|---|---|
| matični broj | 82080 (JBKJS) | `MATBROJ` 08004790 |
| PIB | 100662387 | `ZIRORACUN` **1000662387** |

SEF za javna preduzeća šalje JBKJS umesto matičnog — to je poznato (Srbijagas
86132 umesto 20084600) i zato lookup ide dalje na PIB. Ali PIB u šifarniku ima
**nulu viška**. Nije usamljen: sedam redova ima desetocifren „PIB“ koji počinje
na `1000` (Elit impex, Gatarić, Inter-hermes, Veropoulos, Magna Pharmacija…), a
jedan ima i tačku na kraju (`100002862.`). Šire: 2.252 reda ima „PIB“ koji je
samo cifre a nije devetocifren.

Šta **ne** vredi: `_analitika_dobavljaca` već prima broj računa i nikad ga ne
koristi. Izgleda kao besplatna dobit, ali `TEKUCI` je popunjen na 4 % redova
(1.696 od 45.105) i prljav — u njemu ima i telefona („064/555-7284 Goran“).

Dogovoreno: sedam redova se ispravlja u ERP-u (rešava Kulu zauvek, bez koda), a
aplikacija ne pegla brojeve sama — pogađanje identiteta dobavljača je tačno ono
što ne smemo.

Usput: „KNJIŽNO ODOBRENJE ZA POVRAT AVANSA“ i „Odobravamo otpis neispravne robe
CJ301BX“ padali su u dijagnostici pod „nema ni barkod ni šifru dobavljača —
čovek“, a nijedno nikad neće imati našu šifru. `avans` i `otpis` su dodati u
uzorak finansijskih stavki.

### Predlog komitenta kad brojevi promaše (01.10.)

Kad se dobavljač ne nađe ni po `MATBROJ` ni po `ZIRORACUN`/`BPG`, razlog
zaustavljanja sada nosi i predlog: *„Možda je 00709 Komunalac JKP, Kula — u
šifarniku mu stoji matični 08004790, a na fakturi 82080.“*

Ključ je **skup reči iz imena**, bez pravnog oblika (doo, ad, jkp, javno
komunalno preduzeće…) i bez reči kraćih od tri slova. SEF u ime po pravilu
stavi i mesto („…KOMUNALAC, KULA“, „BCE GROUP DOO ČAČAK“), pa se poredi sa
imenom **i sedištem** komitenta. Mereno na 45.105 komitenata: 96 % skupova reči
vodi na tačno jednog. Predlog se daje samo kad je kandidat jedan.

Provereno na pravom šifarniku, oba zatečena slučaja:

| sa fakture | predlog |
|---|---|
| JAVNO KOMUNALNO PREDUZEĆE KOMUNALAC, KULA | 00709 Komunalac JKP, Kula |
| BCE GROUP DOO ČAČAK | nema — i stvarno ga nema |

Pretraga ne čita ceo šifarnik: najduža reč ide u `LIKE`, pa se poređenje radi
nad nekoliko redova. Poređenje u ERP-u ne razlikuje ni velika slova ni kvačice,
pa „CACAK“ nalazi „Čačak“.

Aplikacija **ne bira dobavljača sama i ne dira šifarnik** — predlog je putokaz
čoveku. Peglanje brojeva po dužini bi značilo pogađanje identiteta dobavljača, a
2.252 reda ima „PIB“ koji je samo cifre a nije devetocifren.

Prva verzija merne skripte vratila je nulu i za Kulu — `normalize` vraća velika
slova, a spisak pravnih oblika sam napisao malim. Brojevi gore su posle
ispravke.

### „Štampaj“ je prvo javljao grešku (01.10.)

PDF se otvarao tek iz drugog pokušaja. Nije bio pregledač.

`GET /purchase-invoice/pdf` na **prvi** poziv vrati **status 200 i JSON** —
`{"message":"Extended PDF has not been generated for invoice Id N."}`, 85
bajtova — i tek tim pozivom pokrene pravljenje. Panel je tih 85 bajtova slao
pregledaču sa `Content-Type: application/pdf`, pa je ovaj prijavio oštećen
dokument; na osvežavanje je PDF već bio gotov i otvarao se.

Mereno na pet netaknutih dokumenata: SEF-u treba **2–3 sekunde**, odnosno 4–6
pokušaja na pola sekunde. Dokumenti koji su već jednom traženi stižu iz prvog
pokušaja (0,1–0,4 s).

`purchase_pdf` sada čeka — do 14 pokušaja na pola sekunde (7 s) — i diže grešku
sa SEF-ovom porukom ako PDF nikad ne stigne. Ruta uz to nikad ne šalje
pregledaču nešto što ne počinje na `%PDF-`.

### Faktura koja tiho stoji između prijema i šifre (02.10.)

Vitelova R-1382/26VP za Odžake: prosleđena 30.09. u 06:48, prijem potvrđen u
10:42, nacrt `spreman`, a u ERP-u ništa — dva dana, bez ijednog traga na ekranu.

Dnevnik dokumenta pokazuje uzrok crno na belo: **jedanaest redova**
`mapiranje.kolebljivo 100335406/-: 87422 -> 87423; veza ugašena`, između 08:07 i
10:39. Crtica u `100335406/-` je prazan ključ — Vitel ne šalje ni svoju šifru ni
EAN, pa su sve njegove stavke delile ključ `(PIB, prazno, prazno)` i svaki
Dušanov klik je gasio prethodnu vezu. U 10:42, kad je prijem potvrđen, nacrt nije
bio spreman, pa je upis odbijen. Kasnije je nacrt postao spreman — i tu je stao.

Isto se našlo 30.09. na 26-15240-238911, zbog čega je i napisana komanda
`dovrsi-upise`. Ali komanda je bila **ručna**, pa se rupa vraćala.

Od 02.10. isti posao radi i petnaestominutni ciklus (`dovrsi_upise`, najviše 20
po prolazu). Argument da sme automatski: čovek **jeste** kliknuo „potvrdi
prijem“ — ovo dovršava tu nameru, ne donosi novu odluku. Preskaču se fakture od
pre prelaska i one sa otvorenim manjkom, kalkulacija ulazi nezaknjižena, a jedan
pali upis ne obara ciklus.

Spisak se i dalje može videti pre upisa: `dovrsi-upise` bez `--stvarno`.

Kad je komanda puštena, zatečene su bile **dve** — R-1382/26VP i RKA26-01429.
Obe su ušle ispravno; sparivanje LEXA šifara potvrđeno nabavnom cenom, koja je u
celoj istoriji jednoznačna (87422 = 4.490 u 21 kalkulaciji, 87423 = 6.900 u 18,
87424 = 17.900 u 12), a upisano je 87422 po 4.490 i 87423 po 6.900.

## „Uvek isti frižider“ na Ilijaninim odobrenjima (02.10.)

Ilijana je prijavila da aplikacija na različita Eurotehnina odobrenja stavlja
istu stavku. Uhvaćeno na delu: kalkulacija 010/00618 za **661FN-2026** imala je
u 13:20 artikal **92846 Gorenje RF 414 EPS4** (PROKNJ 0), a dvadesetak minuta
kasnije **92841 Gorenje R 4142 PS** (PROKNJ 1) — Ilijana ju je ispravila i
proknjižila. Napomena na 661FN kaže „Odobreno za frizider **R4142PS**“, a 92846
je artikal sa **613FN-2026** od 16.09.

**Predlog iz napomene nije kriv.** Odigran nad pravim ERP-om za svih 14
Eurotehninih odobrenja od juna: 9 ima broj računa u napomeni i **svih 9 pogađa
tačan uređaj** (po iznosu, u dinar), 5 nema i ispravno ne predlaže ništa.

Kriv je **redosled**. `_nasa_sifra` prvo pita vezu po šifri dobavljača, a
Eurotehna na svakom odobrenju šalje istu „šifru artikla“ — `Porez 20%`. Jedna
veza za nju razreši stavku, nacrt postane `spreman`, i predlog iz napomene se
**nikad ne izračuna** — jer se računa samo za stavke koje su ostale bez šifre.

U ERP-u nema štete samo zato što Ilijana svaku ispravi rukom; svih devet
upisanih se slaže sa napomenom.

### Pravilo: šifra pod kojom stoji mnogo naziva nije šifra

Mereno na 28.310 stavki panela, parovi (dobavljač + šifra) koji se javljaju na
odobrenjima — njih 553:

| prag | šifara | stavki odobrenja | od toga ima vezu |
|---|---:|---:|---:|
| više od 1 naziva | 58 | 656 | **35** |
| **3+ naziva** | **13** | **425** | **0** |
| 4+ naziva | 11 | 421 | 0 |

Prag **3+** je izabran jer ne staje na put nijednoj vezi koja danas radi, a
hvata sve sporne: `Porez 20%` (100 naziva, 107 odobrenja), Comtrade `5142` (30),
`R0001` (27 naziva, 90 odobrenja), `R0006` (19), CSM `002` (17), EWE `USL00027`
(13 naziva, 109 odobrenja). Šifre sa tačno jednim nazivom — njih 495, sa 234
veze — ostaju netaknute.

Pravilo važi **samo na knjižnim odobrenjima**; na fakturama je šifra ipak šifra.

**Grana po nazivu se ne dira.** Tu asimetrija: šifra pod kojom stoji mnogo
naziva je nalepnica, ali naziv koji se ponavlja je samo roba koja se često vraća
— „LED TV 55PUS7810 PHILIPS“ stoji na 45 dokumenata i to je jedan artikal.
Blokiranje po nazivu bi oborilo 240 veza koje rade.

Spisak se vidi komandom `opste-sifre`.

Ostaje otvoreno: ako neko na odobrenju čekira „zapamti vezu“, veza po **nazivu**
može da odigra istu ulogu kod EWE-a, čiji se nazivi ponavljaju („BEKO KNJIZNO
ODOBRENJE 20%“). Nije se desilo, pa se ne dira unapred.

## Ispravka pogrešno povezane šifre (02.10.)

Dule pri povezivanju ume da upiše pogrešnu šifru. Sporadično je, ali kad se
desi, kalkulacija je često već u ERP-u.

**Šta aplikacija sme.** Provereno na produkciji: nalog `elbraco_dev_ro` ima
`db_datareader` i **INSERT** na `ULKALKUL`, `PRULKALK`, `MKALKUL`, `MPRULK`,
`PDVEVIDX` — **nema UPDATE ni DELETE**. To je ograda koju držimo namerno, pa
„preknjiži po novom“ ne dolazi u obzir ni tehnički ni po dogovoru.

Zato se radi ovo troje:

1. **Veza u šifarniku se popravi.** To je glavna šteta: dok veza nije tačna,
   svaka sledeća faktura sa tim artiklom ide pogrešno. Ide kroz isti
   `povezi_sifru` kao i obično povezivanje — jedan kod, ne dva.
2. **Pročita se `PROKNJIZENO` iz ERP-a** u tom trenutku i zabeleži se šta tačno
   treba ispraviti: broj kalkulacije, stavka, stara i nova šifra sa nazivima.
3. **Javi se jedinici** mejlom, a na dokumentu stoji crveni okvir dok čovek ne
   klikne „Ispravljeno u ERP-u“.

Poruka zavisi od stanja, jer se po tome razlikuje koga poslovođa mora da zove:

| `PROKNJIZENO` | šta ga čeka |
|---|---|
| 0 | otvori kalkulaciju, promeni artikal, gotovo |
| 1 | prvo otknjiži, pa promeni, pa ponovo proknjiži — zaliha se pomerila |
| 2 | zaključana je baš da se ne menja unazad; ide ispravka preko kancelarije |

Nacrt ostaje `UPISAN` — ponovno računanje ga i dalje ne vraća unazad.

**Pretraga.** Dodat je tab **Pretraga** (`prikaz=svuda`) koji traži kroz sve,
uključujući arhivu, i to i po **šifri artikla**, barkodu i nazivu stavke — ne
samo po broju fakture i dobavljaču. Kad se ispravlja pogrešno povezana šifra,
čovek zna šifru, a ne broj fakture; a takva faktura je odavno sklonjena sa
ekrana.

Razmatrano i odbijeno: tražiti UPDATE na `PRULKALK`/`MPRULK` da app sama
ispravi bar nezaknjižene. Dobitak bi bio jedno polje koje poslovođa ionako
otvara, a izgubilo bi se pravilo „app samo dodaje, nikad ne menja“.

### Šifra koje nema u šifarniku zaglavila je fakturu (03.10.)

Eurotehnina 3753VP-2026 (MAG021, 59.194,80) stajala je kao **zaustavljena**:
*„stavka 1: šifra 87622 ne postoji u ARTIKLI/099“*. Šifre 87622 nema nigde u
šifarniku — postoje 87620 (Samsung S26FE) i 87621 (Beha PV 12 WiFi), dakle
omaška u kucanju.

Zaglavljenje je bilo u tome što se pod stanjem „zaustavljeno“ **forma za
povezivanje ne nudi** — ni na tabu Šifre, ni na dokumentu. Čovek vidi šta ne
valja i nema čime da to popravi.

Dve popravke:

* **Šifra koje nema u šifarniku više ne zaustavlja nacrt** nego stavku vraća
  među one koje čekaju šifru, uz razlog („šifra 87622 ne postoji u šifarniku“).
  To i jeste tačan opis stanja: stavka nema upotrebljivu šifru. Forma se tada
  nudi kao i za svaku drugu nemapiranu stavku.
* **Omaška se hvata pri povezivanju**, a ne tek pri računanju nacrta:
  `povezi_sifru` proveri da li šifra postoji u `ARTIKLI/099` i odbije je sa
  porukom. Kad ERP nije dostupan, provera se preskače — ona je pomoć, ne uslov.

Nemapirana stavka sada nosi i polje `razlog`, pa se na tabu Šifre vidi zašto
čeka, a ne samo da čeka.

## Otvaranje nove šifre artikla iz aplikacije (03.10.)

Do sada: poslovođe slikaju ambalažu, Dušan otvara šifru u ERP-u, javi u grupu.
Oko **60 novih artikala mesečno**. Sada se otvara iz panela, jednim klikom.

### Šta se upisuje, a šta namerno ne

| tabela | šta |
|---|---|
| `ARTIKLI` | matični red (099) + red po robnom magacinu — 11 magacina |
| `ARTPROD` | red po prodajnom mestu, **sa `CENAMALO`** |
| `DARTIKLI` | **ne diramo** |

`DARTIKLI` puni **noćni posao na serveru**: svaku novu šifru iz jedinstvenog
šifarnika sam otvori i u dopuni, a Dule sutradan upiše zemlju porekla (Bane,
03.10.). Tabela **nema nijedan indeks** — dupli red se ne bi ni prijavio kao
greška nego bi tiho ostao, pa bi se artikal duplirao u svemu što iz nje čita.
Dok se taj posao ne pogleda, ne upisujemo u nju.

### Barkod je obavezan, i ne izmišlja se

Baza ga traži jedinstvenim: `UQ(SIFRAMAG, BARCODE)` na `ARTIKLI` i
`UQ(SIFRAPRO, BARCODE)` na `ARTPROD`. Zato su sva 43.531 artikla u 099 i
popunjena, i svih 43.531 barkoda je različito.

Prva verzija je htela da upiše šifru umesto barkoda kad fakture nema EAN. To je
pogrešno: **„nema EAN na fakturi“ nije isto što i „artikal nema barkod“** (Bane,
03.10.) — za to i postoji slikanje ambalaže. Zato se barkod traži, a šifra se
upisuje umesto njega **samo kad čovek izričito kaže** da artikal nema barkod;
tako stoji kod 7.005 postojećih artikala.

### Maloprodajna cena je uslov

Bez `ARTPROD.CENAMALO` kalkulacija u maloprodaji staje sa „artikal nema cenu u
objektu“ — taj zastoj smo već imali. Cena je jedinstvena za sva prodajna mesta,
mora biti veća od nule, i **Dule vodi računa koju upisuje** (Bane, 03.10.).

### Provera da već ne postoji

Po barkodu (tačno), pa po nazivu kad barkoda nema. Mereno: u 099 ima 43.531
artikal i **43.432 različita naziva**, pa je lažnih uzbuna malo. Poklapanje se
meri preko skupa reči (≥ 50 %), a ekran ponudi da se slična šifra poveže umesto
da se otvara nova. Ne blokira — upozorava.

### Pravila upisa

Jedna transakcija, oko 25 redova; ili sve ili ništa. Šifra i barkod se proveravaju
**u trenutku upisa**, ne samo kad je predlog pravljen — između može da prođe i
pola sata. Posle uspeha: stavka se poveže, nacrt preračuna, trag u dnevnik
(`artikal.otvoren`).

### Predlog maloprodajne cene (03.10.)

Mereno na 21.851 maloprodajnoj stavki. **Globalna marža ne valja kao oslonac:**
medijana 45 %, ali srednjih 50 % ide od 34 % do 58 %, a po kategorijama od 33 %
(SHKAT 862) do 95 % (403).

Po dobavljaču je kod nekih vrlo usko, kod drugih nije:

| dobavljač | medijana | 25–75 % |
|---|---:|---|
| Vimport | 32 % | 31–33 |
| Candy Hoover | 34 % | 33–39 |
| CSM 2017 | 39 % | 36–43 |
| BSH | 45 % | 42–49 |
| CTC unit | 68 % | 52–92 |
| Coffee Center | 91 % | 69–134 |
| **Roaming Electronics** | **−11 %** | −17–2 |

Od 33 dobavljača sa 30+ stavki, **13 ih ima raspon 25–75 unutar 10 p.p.**
Roaming je poučan: prodaje ispod nabavne (telefoni uz pretplatu), pa bi globalna
marža tu promašila za pola.

Zato: **predlog samo kad je usko** — bar 20 stavki i raspon ≤ 10 p.p. Tada
`nabavna × (1 + medijana) × (1 + PDV)`, **zaokruženo na 100** (86 % postojećih
maloprodajnih cena je zaokruženo na 100, 11 % na 10). Inače polje ostaje prazno
i piše zašto: *„kod ovog dobavljača marža ide od 69 % do 134 % — ne predlažem“*.
Pogrešan broj u polju je gori od praznog, jer čovek prihvati ono što vidi.

Uz to se, pored sličnih artikala iz provere duplikata, prikazuju i **njihove
cene i marže** — za novi Lexa zvučnik videti „87422 Lexa LX 201: 4.490 → 7.900
(46 %)“ vredi više od formule.

Prag „20 stavki i 10 p.p.“ je procena, nije izmeren na ishodu. Dogovor (Bane,
03.10.): kad bude vremena, napraviće se šema maržiranja po brendu i kategoriji
pa će predlog gledati nju.

## Otvaranje novog komitenta iz aplikacije (03.10.)

Dobavljač koga nema u `NAZIVI` zaustavlja kalkulaciju potpuno — nema šta da se
poveže ni popravi dok ga neko ne otvori. Redak ali potpun zastoj: **3 od 198**
dobavljača u istoriji panela.

### Naziv — jedino što app mora da skrati

`NAZIV1` je **varchar(30)**, a sa SEF-a stiže pun pravni naziv. Pravilo je
pročitano iz šifarnika, na 195 parova (pun naziv ↔ postojeći `NAZIV1`): **brend
prvo, pa skraćeni pravni oblik**, bez opisnog dela i bez mesta; ćirilica se
preslovljava; kod javnih preduzeća oblik ide na kraj.

```
PREDUZEĆE ZA SERVIS I TRGOVINU VITEL DOO NIŠ   → Vitel doo
EKSER PROIZVODNJA EXPORT-IMPORT I TRGOVINA DOO → Ekser doo
ЈАВНО КОМУНАЛНО ПРЕДУЗЕЋЕ ВОДОКАНАЛ СОМБОР     → Vodokanal JKP
```

**Tačke se ne pišu.** Istorijski ih ima (`d.o.o.` 676 puta prema `doo` 1.441),
ali su to greške operatera kroz godine i ne ponavljaju se (Bane, 03.10.).

Izmereno na tih 195: **47 % pogodi tačno**, 44 % pogodi brend a razlikuje se
oblik ili jedna reč, 9 % promaši. Promašaji su ono što nijedna heuristika ne
može — „POŠTA SRBIJE…“ je u šifarniku „JP PTT Saobracaja“, a javno preduzeće za
garaže je „Parking servis JKP“. Zato predlog ide **na potvrdu**, a pun naziv
ostaje ceo u `APRNAZIV` (255).

Usput nađena tri buga u prvoj verziji heuristike, svaki vidljiv tek na pravim
podacima: jednoslovni ostaci tačkastih oblika završavali su u imenu („Raiffeisen
banka **A** ad“), zagrade su se uklanjale posle `normalize` koja ih je već
pretvorila u razmake („Procredit bank **novi** ad“), i sve reči su išle velikim
početnim slovom umesto samo prva.

### Provera da već ne postoji

Prvo brojevi, jer su dokaz: `MATBROJ`, pa `ZIRORACUN` (koji uprkos imenu drži
PIB). Kod fizičkih lica tamo stoji **JMBG od 13 cifara**, pa se PIB od devet ne
poredi sa njim — inače bi uparivalo pogrešne ljude. Tek onda ime i mesto, i to
samo kao sumnja. Nađen po broju → otvaranje se **ne dozvoljava**.

### Upis

Jedan red u `NAZIVI`, jedna transakcija. `ANALITIKA` je pet cifara iz slobodnog
bloka (54.882 slobodnih mesta, 110 rupa dužine bar 100) i proverava se **u
trenutku upisa**. `SH_KOM` dobija `.` kao i svuda. Posle uspeha nacrt se
preračuna i kalkulacija obično odmah prolazi.

### Dugmeta nije bilo zato što se zvalo drugačije (05.10.)

Nina i Dule nisu našli „otvori šifru“. Veza je sve vreme stajala u tabu
**Šifre**, ali pod starim imenom **„predlog artikla“** — iz vremena kad je ta
strana samo prikazivala šta bi se upisalo i ispisivala SQL za ručno izvršavanje.
Sad ta strana otvara šifru, pa je i ime moralo da prati: zove se **„otvori
šifru“** i izgleda kao dugme, a ne kao sitna veza između dve forme.

Greška je moja i tipična: pomoć sam napisao po onome što program **radi**, a
ekran je i dalje govorio ono što je nekad **bio**. Test sada drži i ime i vezu.

Usput, razlog zašto stavka čeka šifru („šifra 87622 ne postoji u šifarniku“)
sada se vidi i na Ilijaninom ekranu, ne samo na tabu Šifre.

### Strana predloga pucala je baš na stavci koju treba samo povezati (05.10.)

„Predlog artikla“ je vraćao Internal Server Error. Uzrok je moja regresija od
03.10.: `redovi()` je namerno postao strog — traži barkod i maloprodajnu cenu,
jer čuva upis — ali **prikaz** je i dalje zvao `kao_sql`, koji ide kroz `redovi`.
Dok cena nije uneta, to je izuzetak, pa je pucala cela strana.

Najgore je bilo gde puca: kad barkod stavke **već ima šifru**, ruta je ipak
tražila ispis SQL-a, iako se tu ništa ne otvara. Lokalno se nije videlo jer
dokument na kom sam probao nema nijednu stavku sa EAN-om — probao sam jednu
varijantu i zaključio da radi.

Popravljeno: `kao_sql` vraća prazno umesto izuzetka kad predlog nije potpun, a
strana u tom slučaju nudi **dugme „Poveži šifru X“** — što je i jedina ispravna
radnja. Provereno na svim varijantama (sa EAN-om koji postoji, sa EAN-om koga
nema, bez EAN-a).

### Ispravka šifre nije smela da zavisi od toga dokle je kalkulacija stigla (05.10.)

Forma „Pogrešna šifra artikla?“ nudila se **samo kad je kalkulacija već u
ERP-u**. Ispostavilo se da je to najuži mogući slučaj: ako Dule pogreši šifru
pre nego što kalkulacija ode, takva stavka **ima** šifru — pa je nema na tabu
Šifre — a na dokumentu se forma nije pokazivala. Nigde se nije mogla promeniti.

Sada forma stoji u redu „Kalkulacija“ za svako stanje osim „ne ide kroz
kalkulaciju“, a šta se dešava zavisi od stanja:

* **nije u ERP-u** → obično povezivanje: veza se popravi, nacrt se preračuna, i
  time je gotovo — nema kome da se javlja, jer u ERP-u još ništa ne stoji;
* **jeste u ERP-u** → kao i do sada: veza, čitanje `PROKNJIZENO`, crveni okvir i
  mejl jedinici.

Uz svaku stavku u izboru piše koju šifru trenutno ima, da se greška vidi pre
nego što se kuca nova.
