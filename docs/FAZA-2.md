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

| Tabela | Koliko redova po artiklu | Gde |
|---|---|---|
| `ARTIKLI` | 18 (matični `099` + kopija po magacinu) | u 5 magacina (`001, 006, 012, 029, 099`) je preko 90 % novih |
| `ARTPROD` | 21 | 21 prodajno mesto |
| `DARTIKLI` | 1 | dopuna (opis, slike, uvoznik) |

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

**Pravilo koje je ugrađeno:** prva slobodna šifra iz **prve rupe od bar 100 mesta**,
uz obaveznu proveru baš te šifre pred dodelu (spisak je snimljen na početku, a
šifru u međuvremenu može otvoriti neko drugi). Prag od 100 postoji da se nove šifre
ne razbacuju po usamljenim rupama među starim artiklima — ima ih preko 800 veličine
jedan. Po tom pravilu prva predložena šifra je danas **9015**.

Prag je jedan parametar (`najmanja_rupa`), pa se lako pomeri ako se odluči da nove
šifre idu u veliki blok umesto u prvu rupu.

`sefsync sifre-slobodne` ispisuje mapu rupa i šifru koja bi se predložila.
