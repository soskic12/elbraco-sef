# Postavljanje panela na server

Panel je Python servis koji sluša na portu 8080. Ne traži IIS — radi sam, kao
Windows servis. Ide na isti server na kom je i logistika, da bi radio 24/7 i da
bi kredencijale čitao iz `web.config`-a koji tamo već postoji.

Sve se radi **na serveru**, kao administrator, preko RDP-a.

---

## Šta ti treba pre početka

| | Kako proveriti | Ako nema |
|---|---|---|
| **Python 3.12 ili 3.13** | `py --version` | [python.org](https://www.python.org/downloads/windows/) — obavezno „Add python.exe to PATH“ |
| **ODBC Driver 18 for SQL Server** | `Get-OdbcDriver -Name "*SQL Server*"` | [Microsoft download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) |
| **NSSM** *(nije obavezan)* | fajl `C:\efakture\alati\nssm.exe` | ne treba ništa — bez njega se koristi zakazani zadatak |
| **SEF API ključ** | — | eFaktura portal → Podešavanja → API management |
| **Gmail app password** | — | onaj koji si napravio za SEF |

Gotovi paketi u `wheelhouse\` su građeni za **3.12 i 3.13**, pa instalacija radi
bez interneta. Ako server ima drugu verziju, skripta to prepozna i pokuša preko
interneta.

**Servis:** ako `nssm.exe` postoji, panel se registruje kao Windows servis. Ako
ne postoji — a na Windows Serveru 2019 nema ni `winget` da se lako nabavi —
koristi se **zakazani zadatak**: pokreće se sa sistemom, radi kao SYSTEM i sam
se diže ako proces padne. Za ovaj posao radi isto.

---

## Postavljanje

**0.** Napravi paket — **na razvojnoj mašini**, iz foldera projekta:

```powershell
powershell -ExecutionPolicy Bypass -File .\napravi-paket.ps1
```

Skripta sklopi `objava\sefsync-paket.zip`: kod, točkove za offline instalaciju
(3.12 i 3.13) i `pravila.json` sa šifarnikom PJ, pravilima razvrstavanja i
mapiranjima artikala. Prethodni paket ne briše nego mu doda datum u ime.

Ručno sklapanje se ne radi — tako je jednom u paket ušao `build\` folder, a pypdf
je ispao, pa bi instalacija bez interneta pukla na serveru.

**1.** Prekopiraj na server (RDP-om, deljenim folderom, svejedno):

```
sefsync-paket.zip   →   C:\objave\sefsync-paket.zip
```

**2.** Otvori PowerShell **kao administrator** i prvo pogledaj šta bi se desilo —
skripta podrazumevano ništa ne menja:

```powershell
powershell -ExecutionPolicy Bypass -File C:\objave\postavi.ps1 -Arhiva C:\objave\sefsync-paket.zip
```

Pročitaj ispis. Ako neka provera pukne (Python, ODBC, NSSM), reši je pa ponovi.

**3.** Stvarno postavljanje:

```powershell
powershell -ExecutionPolicy Bypass -File C:\objave\postavi.ps1 -Arhiva C:\objave\sefsync-paket.zip -Stvarno
```

Skripta: napravi bekap prethodne verzije, zaustavi servis, prekopira novu,
napravi `.venv`, instalira zavisnosti, kreira `.env` iz šablona, napravi tabele,
registruje servis, otvori port 8080 u firewall-u i upali sve. Na kraju proverava
da panel odgovara.

**4.** Popuni dva reda u `C:\efakture\.env` (skripta te podseti):

```ini
SEF_API_KEY=<ključ sa eFaktura portala>
SMTP_PASSWORD=<Gmail app password>
```

Proveri i ovaj red — mora da pokazuje na `web.config` sajta logistike:

```ini
ERP_SECRETS_FILE=C:\inetpub\logistika\web.config
```

Tu su i veza ka bazi i pošta, pa panel ne drži svoju kopiju tih lozinki.

**5.** Restartuj servis i proveri:

```powershell
# NSSM:     nssm restart ElbracoEfakture
# zadatak:  Stop-ScheduledTask ElbracoEfakture; Start-ScheduledTask ElbracoEfakture
C:\efakture\.venv\Scripts\sefsync.exe check        # veza sa SEF-om
C:\efakture\.venv\Scripts\sefsync.exe erp-check    # veza sa bazom
C:\efakture\.venv\Scripts\sefsync.exe ko-moze      # ko može u panel
C:\efakture\.venv\Scripts\sefsync.exe pj-sync      # šifarnik iz ERP-a
```

**6.** Nina otvara `http://<ime-servera>:8080` i prijavljuje se svojim ERP nalogom.

---

## Posle postavljanja

**Prvo preuzimanje** — panel dalje preuzima sam na 15 minuta, ali prvi put
vredi povući ručno i skloniti istoriju iz reda:

```powershell
C:\efakture\.venv\Scripts\sefsync.exe backfill --od 2026-01-01
C:\efakture\.venv\Scripts\sefsync.exe arhiviraj --do <juče> --stvarno
```

**Probni režim** je uključen: dok u `.env` stoji `NOTIFY_OVERRIDE_TO`, nijedno
obaveštenje ne ide u objekte nego samo na tu adresu. Kad se tok uvežba, obriši
vrednost i restartuj servis.

---

## Nova verzija na server

Posle `postavi.ps1 -Stvarno` idu još tri koraka. Skripta ih ne radi sama jer svaki
menja podatke, a ne kod.

```powershell
C:\efakture\.venv\Scripts\sefsync.exe pravila-uvoz C:\efakture\pravila.json
C:\efakture\.venv\Scripts\sefsync.exe osvezi-ubl
C:\efakture\.venv\Scripts\sefsync.exe razvrstaj
```

**`pravila-uvoz`** — pravila razvrstavanja i mapiranja artikala nisu u kodu nego u
bazi. Bez ovoga nova verzija radi po starim pravilima. Uvoz ne duplira, prenosi i
**ugašena** pravila kao ugašena (zato što #44 i #47 treba da prestanu da rade i
ovde, a pravila 93 % svih grešaka u razvrstavanju).

**`osvezi-ubl`** — ponovo čita već preuzete UBL fajlove sa diska, bez SEF-a. Puni
polja koja parser u međuvremenu nauči (veza ka ispravljenoj fakturi, tekst PDF
priloga, žiro račun). Bez ovoga ta polja ostaju prazna za istoriju.

**`razvrstaj`** — namerno **bez** `--sve`: razvrstava samo ono što još nema
poslovnu jedinicu. Sa `--sve` bi promenio PJ i dokumentima koje je operater već
prosledio u objekat.

---

## Svakodnevno

| Šta | Kako |
|---|---|
| Stanje (NSSM) | `nssm status ElbracoEfakture` |
| Stanje (zadatak) | `Get-ScheduledTask ElbracoEfakture \| Get-ScheduledTaskInfo` |
| Logovi | `C:\efakture\data\servis.log` i `data\sefsync.log` |
| Restart (NSSM) | `nssm restart ElbracoEfakture` |
| Restart (zadatak) | `Stop-ScheduledTask ElbracoEfakture; Start-ScheduledTask ElbracoEfakture` |
| Nova verzija | isti `postavi.ps1` sa novim paketom — `.env` i `data\` ostaju |
| Nazad na staru verziju | `postavi.ps1 -Vrati -Stvarno` (čuva se poslednjih pet) |

---

## Šta gde stoji

```
C:\efakture\
  .env              podešavanja i lozinke ovog servera — NE dira se pri nadogradnji
  .venv\            Python okruženje
  src\              kod
  wheelhouse\       gotovi paketi za instalaciju bez interneta
  sql\              upit za šifarnik poslovnih jedinica
  alati\nssm.exe    alat za servis
  data\             baza panela, preuzeti UBL-ovi, logovi — NE dira se
  bekap\            poslednjih pet verzija
```

Bekapuj `data\` zajedno sa ostalim bekapima servera — tu je sav rad operatera
(razvrstavanje, prosleđeno, potvrde prijema, istorija radnji). Kod i paketi se
uvek mogu postaviti ponovo; taj folder ne.
