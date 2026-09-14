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
| `PROKNJIZENO` | `2` = zaknjiženo (4.211), `1` = čeka (439), `0` (2) |

### Stavke

- `RBR` = Clarion vreme + **korisničko ime** (`8219653534078121dane`) —
  svaka stavka nosi ko ju je uneo
- `KOLICINA` negativna kod povraćaja (`-1`)
- `MALACENA` == `ARTPROD.CENAMALO` u **80 %** slučajeva → app nudi tekuću
  maloprodajnu cenu objekta, marža (`RUCM_PROC`) se izvodi unazad; onih 20 %
  su promene cena koje poslovođa unosi
- marža nije konstanta (51,8 % / 33,5 % / 36,8 % na uzastopnim stavkama)

### Broj računa dobavljača

`BROJ_RN_DOB` je `varchar(20)`. Praksa: izbaciti kose crte, minuse i ostalo da
stane. **App mora da skraćuje na isti način**, inače pravi brojeve kakve ljudi ne
prave i veza puca za ubuduće.

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
