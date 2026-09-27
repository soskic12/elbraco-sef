# Prelazak na rad u aplikaciji — 28.09.2026. u 8 h

Dva dela: **večeras** se sve postavi i proveri, a aplikacija i dalje ne dira ni
pravu bazu ni poštu. **Ujutru**, dok si tu, prebace se dva prekidača.

Sve komande idu na serveru, u RDP-u, kao administrator.
Skraćeno: `SEF = C:\efakture\.venv\Scripts\sefsync.exe`.

---

## Večeras (nedelja 27.09.)

### 1. Postavi paket

```
copy  objava\sefsync-paket.zip  C:\objave\sefsync-paket.zip
powershell -ExecutionPolicy Bypass -File C:\objave\postavi.ps1 -Arhiva C:\objave\sefsync-paket.zip -Stvarno
```

> `postavi.ps1` pokreni **iz raspakovanog paketa**, ne onaj stari iz `C:\objave\`.

Pri prvom pokretanju panel sam dopuni šemu — nova tabela `prijava_manjka` i
kolone `sumnja`, `sumnja_odbio`, `sumnja_odbijeno_u`, `erp_napomena`. Ništa se
ne unosi ručno.

### 2. Pravila i jedinice

```
SEF pravila-uvoz C:\efakture\pravila.json --i-jedinice
```

`--i-jedinice` je ono što popravlja pobrkano ime „UPRAVA — FINANSIJE“ koje je
Bogdan video.

### 3. Povuci i razvrstaj

```
SEF osvezi-ubl
SEF razvrstaj
```

Namerno **bez `--sve`**: sa njim bi promenio poslovnu jedinicu i dokumentima
koje je operater već prosledio.

### 4. Preračunaj nacrte

```
SEF nacrti --osvezi
```

Traje duže nego ranije — uz svaki dokument sada ide i traženje mogućeg
duplikata. Očekivano: broj „upisanih“ poraste za oko **50** (fakture čiji je
broj u ERP-u odsečen na 15 znakova) plus još nekoliko koje su zavedene pod
drugom šifrom istog dobavljača.

### 5. Dozvole u produkcionoj bazi

```
SEF provera-prelaska
```

Red o dozvolama ispisuje **ime naloga** i tačnu naredbu. Za svaku od pet tabela:

```sql
USE ELBS_2026;
GRANT INSERT ON dbo.ULKALKUL  TO [<nalog iz ispisa>];
GRANT INSERT ON dbo.PRULKALK  TO [<nalog iz ispisa>];
GRANT INSERT ON dbo.MKALKUL   TO [<nalog iz ispisa>];
GRANT INSERT ON dbo.MPRULK    TO [<nalog iz ispisa>];
GRANT INSERT ON dbo.PDVEVIDX  TO [<nalog iz ispisa>];
```

Aplikacija ne traži ništa više od INSERT-a na te tabele — ne briše, ne menja i
ne dira šifarnike.

### 6. Granica prelaska

U `C:\efakture\.env`:

```
NOVI_TOK_OD=2026-09-28
```

Time svaka faktura pristigla pre ponedeljka ostaje po starom: nema ni dugmeta
za prijem ni upisa, ni poslovođi ni operateru.

> Pazi da menjaš **onaj `.env` koji server stvarno čita** — to se već dešavalo.

### 7. Restart i provera

```
powershell -ExecutionPolicy Bypass -File C:\efakture\restart.ps1
SEF provera-prelaska
```

Večeras je ispravno stanje **tačno dva upozorenja**:

- `ERP_ALLOW_PRODUCTION_WRITE je isključen` — upis ide u test bazu;
- `PROBNI REŽIM: sve ide na ...` — poslovođe još ne dobijaju poštu.

Sve ostalo mora biti `ok`. Ako je bilo šta drugo žuto, **ne nastavljaj ujutru
dok se ne reši.**

### 8. Otvori panel i pogledaj očima

- `/pomoc` — proveri da se otvara i da je to tvoj deo.
- Bogdanov ekran — koliko faktura je ostalo posle koraka 4.
- `SEF sumnje --magacin 088` — spisak predloga „možda je već u ERP-u“. Nemoj ih
  rešavati sam; to je posao za ujutru, sa Bogdanom, jer on zna šta je uneo.

---

## Ujutru (ponedeljak 28.09. u 8 h, dok si tu)

### 1. Dva prekidača

U `.env`:

- **obriši** `NOTIFY_OVERRIDE_TO` (ili ga isprazni) — pošta kreće jedinicama;
- postavi `ERP_ALLOW_PRODUCTION_WRITE=true`.

```
powershell -ExecutionPolicy Bypass -File C:\efakture\restart.ps1
SEF provera-prelaska
```

Mora da piše **„Sve stoji“**. Ako ne piše, stani.

### 2. Prvi upis zajedno

Jedna **uslužna faktura na 088** — nema stavki ni marže, najmanje toga može da
zaškripi. Bogdan klikne „Upiši kalkulaciju“, pa zajedno u ERP-u proverite:

- kalkulacija postoji, u pravom magacinu i sa pravim dobavljačem;
- `PROKNJIZENO = 0` — otvorena, čeka njega;
- iznos, datum prometa i valuta se slažu sa fakturom;
- red u `PDVEVIDX` je tu.

Tek kad to vidi svojim očima, Bogdan knjiži.

### 3. Pređite predloge

`SEF sumnje --magacin 088`, pa na ekranu „Jeste, skloni“ ili „Nije to“ za svaki.
Ovo radi Bogdan, ne ti — on zna koje je unosio.

### 4. Čišćenje — Nina i Dušan

Arhiviraju zaostale fakture od pre prelaska koje niko više neće dirati.
**Ne arhiviraju** ono za šta roba tek treba da stigne: poslovođa i za to pravi
kalkulaciju u ERP-u, ali fakturu treba da vidi.

### 5. Reci ljudima

Jedna rečenica svima: od jutros se radi u aplikaciji, a **gore desno stoji
„Pomoć“** gde piše šta ko radi. Nema odštampanog uputstva — namerno, da ne bi
zastarelo.

---

## Ako nešto pođe naopako

| Šta se desilo | Šta radiš |
|---|---|
| Panel ne diže posle restarta | `restart.ps1` ubija onoga ko drži 8088; ako i dalje stoji, pogledaj zakazani zadatak `ElbracoEfakture` |
| Kalkulacija upisana greškom | `SEF zaboravi-upis <id> --stvarno`, pa obriši red u ERP-u. Ništa se nije pomerilo — upis ne dira zalihu |
| Hoćeš sve da zaustaviš | `ERP_ALLOW_PRODUCTION_WRITE=false` i restart. Panel radi dalje, samo prestaje da piše u pravu bazu |
| Neko je primio robu a fali komad | To više nije greška — „Stiglo manje nego što piše“, opisano u Pomoći |

---

## Posle prelaska, kad nema pritiska

**Razdvojiti nalog ka ERP-u.** Panel se prijavljuje kao `elbraco_dev_ro` — isti
nalog koji koristi i razvojna mašina, i po imenu „dev read-only“. Pred prelazak
mu je dat INSERT na pet tabela u `ELBS_2026` (Bane, 27.09., svesna odluka: pet
naredbi umesto diranja `web.config`-a uoči prelaska).

Posle prelaska napraviti zaseban nalog samo za panel, npr. `sefsync_erp`, dati
mu INSERT isključivo na `ULKALKUL`, `PRULKALK`, `MKALKUL`, `MPRULK`, `PDVEVIDX`,
upisati ga u `web.config` na serveru i **oduzeti INSERT nalogu `elbraco_dev_ro`**.
Time razvojno okruženje ostaje bez ikakvog prava upisa u produkciju, pa pravilo
„u produkciju se piše samo dok je Bane tu“ ne zavisi samo od prekidača u `.env`.
