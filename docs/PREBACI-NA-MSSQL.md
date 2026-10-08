# Prebacivanje baze panela na MS SQL

## Zašto, i zašto nije bila greška

`.env.server` je 21.09.2026. izričito rekao: *„SQLite u `data\` je dovoljan: jedan
operater, nekoliko hiljada dokumenata godišnje.“* To je bila **svesna odluka pri
instalaciji**, ne promašaj, i **nijedan podatak nije izgubljen** — baza u
`C:\efakture\data\sefsync.db` je ona prava, sa istorijom od 11.09.

Pretpostavka je pala. Mereno 08.10.2026.:

| | pretpostavka 21.09. | stvarno 08.10. |
|---|---|---|
| ljudi | jedan operater | Nina, Bogdan, Dule, Ilijana + poslovođe po objektima |
| dokumenata | nekoliko hiljada godišnje | **8.467 u 27 dana** (~113.000 godišnje) |
| poslovnih jedinica | — | 95 |
| ko još piše | — | IIS i petnaestominutni ciklus, paralelno |

**SQLite pri upisu zaključava celu bazu.** Dok je jedan čovek to se ne vidi; kad
poslovođa potvrđuje prijem, Nina razvrstava, a ciklus u isto vreme dovršava upise
i preračunava nacrte — bore se za istu datoteku. Zato se prebacuje.

Što je ostalo zapisano da se ne ponovi: `.env.server` sada **traži** `DB_URL`,
`provera-prelaska` prijavlja SQLite kao grešku kad je upis u produkciju uključen,
a panel o tome piše u log pri svakom pokretanju.

## Prvo: šta je u `SefSync` na MS SQL-u — **ne preskakati**

Baza `SefSync` **postoji** na `CLELBRACO` (vidi se u spisku baza), ali joj panelovi
ERP nalozi nemaju pristup, pa se iz aplikacije ne zna šta je u njoj. To je jedino
što deli bezbednu seobu od mešane baze:

* **ako je prazna** — seoba ide bez razmišljanja;
* **ako u njoj ima podataka** — seoba se **odbija**. Tabela koja u cilju već ima
  redova bila bi preskočena, pa bi deo podataka bio iz nje a deo iz SQLite, sa
  stranim ključevima koji pokazuju u prazno. To je gore od neurađene seobe i
  teško se prepoznaje.

Od 08.10. aplikacija to **ne dopušta**: `prebaci-bazu` prvo pogleda cilj i staje
ako nije prazan. Suvi prolaz ispiše „Cilj je prazan — seoba može“ ili spisak
tabela koje ga sprečavaju.

Provera u SSMS-u, samo čita:

```sql
USE [SefSync];
SELECT t.name AS tabela, SUM(p.rows) AS redova
FROM sys.tables t
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
GROUP BY t.name
ORDER BY redova DESC;
```

* **Ne vrati ništa** → baza je prazna, idi dalje.
* **Vrati tabele sa nulama** → šema postoji ali podataka nema; isto je u redu.
* **Vrati tabele sa redovima** → stani i javi šta piše. Tada se ne seli u nju
  nego u novu (`SefSync2`), pa se stara preimenuje kad se nova potvrdi.

## Šta treba pre početka

1. **Baza `SefSync` na MS SQL-u** (`CLELBRACO`, `194.146.57.216:1433`). Panelovi
   ERP nalozi je **ne vide** (`HAS_DBACCESS` = 0), pa se ne zna da li već postoji
   — proveriti u SSMS-u. Ako je nema, napraviti je.
2. **Nalog koji sme da pravi tabele u njoj** — panel pri prvom pokretanju sam
   napravi šemu, pa mu treba `db_owner` **u toj bazi** (i ničemu drugom):

   ```sql
   USE master;
   IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'sefsync_panel')
       CREATE LOGIN [sefsync_panel] WITH PASSWORD = '<lozinka>',
           DEFAULT_DATABASE = [SefSync], CHECK_POLICY = ON;
   ELSE
       ALTER LOGIN [sefsync_panel] WITH PASSWORD = '<lozinka>';

   IF DB_ID('SefSync') IS NULL CREATE DATABASE [SefSync];
   GO
   USE [SefSync];
   IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'sefsync_panel')
       CREATE USER [sefsync_panel] FOR LOGIN [sefsync_panel];
   ALTER ROLE db_owner ADD MEMBER [sefsync_panel];
   GO
   -- Provera: mora da vrati CLELBRACO i broj.
   USE master;
   SELECT @@SERVERNAME AS server, SUSER_ID('sefsync_panel') AS nalog;
   ```

   **Kolacija mora da podrži ćirilicu.** Panel ima proveru (`proveri_unicode`)
   koja javlja ako kolone ispadnu `VARCHAR` umesto `NVARCHAR` — dobavljači se
   zovu i ћирилицом (`ЈАВНО КОМУНАЛНО ПРЕДУЗЕЋЕ`).

## Prebacivanje

Panel **mora da stoji** dok se seli — inače se u staru bazu upisuje ono što u
novu neće preći.

```
powershell -ExecutionPolicy Bypass -File C:\efakture\restart.ps1 -Zaustavi
```

Ispisaće „Zaustavljeno. Panel NE radi". Prekidač `-Zaustavi` je dodat 08.10. baš
za ovo — dotle je `restart.ps1` uvek palio natrag.

**1. Suvi prolaz — ništa ne upisuje, samo prebroji:**

```
C:\efakture\.venv\Scripts\sefsync.exe prebaci-bazu --u "mssql+pyodbc://sefsync_panel:<lozinka>@194.146.57.216:1433/SefSync?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
```

Ispiše izvor, cilj i broj redova po tabeli. Očekivano (mereno 08.10.): oko 8.500
dokumenata, 2.267 veza šifara, 231 pravilo, 95 jedinica, ~640 nacrta, 86
obaveštenja. **Ako neki broj izgleda premalo, stani.**

**2. Stvarno prebacivanje** — isti red plus `--stvarno`. Identifikatori se
zadržavaju (`IDENTITY_INSERT`), pa veze između dokumenata, stavki i obaveštenja
ostaju iste. Tabela koja u cilju već ima redove se **ne dira**, da ponovno
pokretanje ne udvostruči podatke.

**3. `DB_URL` u `C:\efakture\.env`**, isti URL kao gore.

**4. Restart i provera:**

```
powershell -ExecutionPolicy Bypass -File C:\efakture\restart.ps1
C:\efakture\.venv\Scripts\sefsync.exe baza
C:\efakture\.venv\Scripts\sefsync.exe provera-prelaska
```

`baza` mora da pokaže `mssql+pyodbc://...` i **iste brojeve** kao suvi prolaz, a
najstariji dokument od 11.09. `provera-prelaska` više ne sme da prijavljuje
SQLite. U logu ne sme biti reda o SQLite.

**5. Pogledaj očima:** otvori panel, jedan dokument sa stavkama, Bogdanov ekran i
jedan objekat. Ako je sve tu — gotovo.

## Ako pođe naopako

Stara SQLite baza **ostaje nedirnuta** — seoba samo čita iz nje. Vraćanje je
brisanje reda `DB_URL` iz `.env`-a i restart. Zato se stari fajl ne briše najmanje
nedelju dana, a najbolje nikad: `C:\efakture\data\sefsync.db` je dokaz šta je bilo.
