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

## Posle prelaska: razdvajanje ERP naloga

Panel se prijavljuje kao `elbraco_dev_ro` — isti nalog kojim se kačim i sa
razvojne mašine, i po imenu „dev read-only“. Pred prelazak mu je dat INSERT na
pet tabela u `ELBS_2026` (Bane, 27.09., svesna odluka: pet naredbi umesto
diranja `web.config`-a uoči prelaska), a 06.10. još tri za otvaranje šifre i
komitenta.

**Gde je stvarni problem.** Nije u imenu naloga nego u tome što panel **nema
svoju konekciju** — pozajmljuje Logistikinu (`ConnectionStrings:ErpDatabase`
iz .NET user-secrets na razvojnoj mašini, `ConnectionStrings__ErpDatabase` iz
`C:\elbraco-logistika\web.config` na serveru). Dve
posledice: u logovima baze se saobraćaj panela ne razlikuje od Logistikinog, a
razvojna mašina ima pravo upisa u produkciju — pravilo „u produkciju se piše
samo dok je Bane tu“ visi na prekidaču u `.env`-u.

**Šta nalog danas sme** (mereno 07.10. komandom `sefsync erp-nalog`):

| baza | čita | INSERT |
|---|---|---|
| `ELBS_2026` | da | ULKALKUL, PRULKALK, MKALKUL, MPRULK, PDVEVIDX, ARTIKLI, ARTPROD, NAZIVI, **DARTIKLI** |
| `ELBSX_2026` (test) | da | prvih pet; **fali** ARTIKLI, ARTPROD, NAZIVI |
| `ELBS_2025`–`ELBS_2022` | da | — |

Van toga **ničemu nema pristup**: ni `ELBS_Logistika`, ni MobAssist,
Reklamacije, Vozila, Webkroler, ni starije godine, ni `SefSync`. (Ranije sam
rekao da se na njih kači — to je bilo pogrešno čitanje: `42S02` je „nema takvog
objekta“, ne „ušao sam“. Autoritet je `HAS_DBACCESS`, i on kaže ne.) Obim je,
dakle, već uzak; ostaje identitet i pozajmljena konekcija.

Dve stvari koje iz te tabele ispadaju:

* **`DARTIKLI` nalogu ne treba.** Aplikacija je namerno ne dira — puni je noćni
  posao na serveru i duplirani ključ bi ga oborio. Pravo koje se ne koristi je
  pravo koje jednog dana neko iskoristi slučajno.
* **U test bazi fali INSERT na `ARTIKLI`, `ARTPROD` i `NAZIVI`.** Dok je
  `ERP_ALLOW_PRODUCTION_WRITE` isključen, otvaranje šifre i komitenta ide u
  `ELBSX_2026` i tamo puca na dozvolu. Na serveru radi jer je upis u produkciju
  uključen — ali proba pre postavljanja ne bi prošla.

### Kako se radi

Aplikacija sama sastavi naredbe; izvršiti ih ne može i neće moći —
`CREATE LOGIN` traži prava na nivou servera.

```
sefsync erp-nalog                      # samo meri: šta nalog sme, šta mu fali
sefsync erp-nalog --novi sefsync_erp   # sastavi naredbe i red za .env
```

1. **Pusti naredbe** koje ispis da (neko sa pravima na SQL serveru). One prave
   `sefsync_erp`, daju mu `db_datareader` na šest baza i INSERT na osam tabela u
   `ELBS_2026` i `ELBSX_2026`. Ništa drugo: ni UPDATE, ni DELETE, ni EXECUTE, ni
   `DARTIKLI`, ni druge baze.
2. **Dodaj jedan red u `C:\efakture\.env`** — `ERP_DB_URL=...`, isti ispis ga da
   gotovog. Time panel prestaje da čita Logistikin `web.config`: `ERP_DB_URL`
   ima prednost nad `ERP_SECRETS_FILE`.
3. **Restart i provera.**

   ```
   powershell -ExecutionPolicy Bypass -File C:\efakture\restart.ps1
   sefsync erp-nalog
   ```

   Mora da piše `veza : ERP_DB_URL (sopstvena)`, nalog `sefsync_erp`, i
   nijedno žuto „fali INSERT“.

**Stari nalog se ne dira.** `elbraco_dev_ro` koristi drugi projekat (Bane,
06.10.: „ovo elbraco dev ro ne bismo smeli tek tako oduzimati kad ga koristi
drugi projekat“). Kad panel nekoliko dana proradi pod svojim nalogom, odvojeno
se dogovara šta se njemu oduzima — prvi kandidat je INSERT na `DARTIKLI`, koji
ne koristi nijedan projekat.

**Ako nešto pođe naopako:** izbriši red `ERP_DB_URL` iz `.env`-a i restartuj —
panel se vrati na staru vezu, jer `sefsync_erp` ne menja ništa u bazi što bi
stari nalog mogao da promaši.
