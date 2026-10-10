# Stanje projekta posle nestanka struje — 10.10.2026.

Razvojna mašina (BANEWORKPC) se resetovala zbog strujnog udara, a disk je
počeo delimično da otkazuje. Ovaj zapis beleži **tačno stanje** projekta posle
toga: šta je sačuvano gde, šta je provereno i šta se nije moglo pročitati.

Sadržajno stanje rada (server, brojevi, otvorena pitanja) je u
[PRESEK-09-10-2026.md](PRESEK-09-10-2026.md) i
[DNEVNIK-SERVERA.md](DNEVNIK-SERVERA.md) — od tada nije bilo novog rada.

> Server (`CLELBRACO`, `C:\efakture`, MS SQL `SefSync`) je **druga mašina** i
> ovaj kvar ga ne dotiče. Produkcijski podaci panela su tamo, ne ovde.

---

## 1. Git i GitHub

| | vrednost |
|---|---|
| grana | `main` |
| poslednji commit pre ovog zapisa | `5263c6c` — *Dnevnik: provera upisa i tri stvarna nalaza* (09.10. 14:58) |
| commita ukupno | 234 (prvi 12.09.2026) |
| **nije bilo na GitHub-u pre 10.10.** | **222 commita** — GitHub je stajao na `8a37209` (*Mapiranje artikala iz istorije…*) |
| posle `git push` 10.10. | `origin/main` = `5263c6c`, potvrđeno sa `git ls-remote` |
| `git fsck --full` | bez grešaka; jedan „dangling commit" `b043577` — stari WIP stash od 01.10., čiji je kod već u `kalkulacija.py` (`_nasa_sifra`, red „ručno") |
| radno stablo | čisto, nijedan nesačuvan fajl |
| praćenih fajlova | 133 — **svaki ponovo pročitan sa diska i upoređen sa gitom: svi identični** |
| kod | ~29.300 redova (`src` + `tests`) |

Repo `github.com/soskic12/elbraco-sef` je **javan** (API odgovara bez prijave).
Zato ono što je `.gitignore` namerno isključio **nije** stavljeno na GitHub —
vidi odeljak 3.

---

## 2. Provera čitljivosti diska

Svaki fajl u projektu osim `.venv` i `.git` pročitan je do kraja (`sha256sum`).

* pročitano bez greške: **8.372 fajla**
* **ne može da se pročita (Input/output error): 9 fajlova** — svi su stari
  instalacioni paketi u `objava/`:

| fajl | napravljen |
|---|---|
| `objava/sefsync-paket.20260924-110602.zip` | 24.09. 11:06 |
| `objava/sefsync-paket.20260926-123042.zip` | 26.09. 12:30 |
| `objava/sefsync-paket.20260928-125322.zip` | 28.09. 12:53 |
| `objava/sefsync-paket.20260929-140244.zip` | 29.09. 14:02 |
| `objava/sefsync-paket.20260929-142358.zip` | 29.09. 14:23 |
| `objava/sefsync-paket.20260929-143341.zip` | 29.09. 14:33 |
| `objava/sefsync-paket.20260929-144936.zip` | 29.09. 14:49 |
| `objava/sefsync-paket.20260930-143051.zip` | 30.09. 14:30 |
| `objava/sefsync-paket.20261006-081757.zip` | 06.10. 08:17 |

Gubitak je zanemarljiv: paket se pravi iz koda (`napravi-paket.ps1`), a kod svakog
od tih dana je u gitu. **Poslednji paket `objava/sefsync-paket.zip` (09.10. 14:54)
je čitljiv.**

To što ovi fajlovi ne mogu da se pročitaju znači da disk zaista ima loše
sektore. `C:` i `H:` su particije **istog fizičkog diska** (disk 1, Kingston
SNV3S2000G), iako Windows za njega i dalje kaže „Healthy".

SQLite baze su proverene i iznutra (`PRAGMA integrity_check`): obe `ok`.

---

## 3. Šta postoji samo na ovom disku (van gita)

Ukupno ~4,6 GB. Kontrolne sume (SHA-256) su ovde da bi se svaka rezervna kopija
mogla proveriti.

### Nezamenljivo ili teško zamenljivo

| fajl | veličina | SHA-256 (početak) | šta je |
|---|---|---|---|
| `.env` | 8 KB | `27d6c8a0cdb8…` | **tajne**: SEF API ključ, SMTP lozinka, ERP konekcija, push webhook. Razvojna podešavanja; serverski `.env` je na serveru. Na kraju fajla stoje dva nekomentarisana reda `ERP_DB_URL=`, jedan sa razmakom ispred — zatečeno tako, nije dirano. |
| `poslovne_jedinice.csv` | 4 KB | `7253dfebe15c…` | stvarni šifarnik jedinica sa mejlovima i telefonima zaposlenih; dolazi iz ERP-a (`pj-sync`), može se ponovo povući |
| `objava/pravila.json` | 690 KB | `0727caf20371…` | 237 pravila razvrstavanja, 09.10. — ista su i u bazi servera |
| `data/sefsync.db` | 33 MB | `9d13e986d78a…` | razvojna SQLite kopija: 7.727 dok., 28.310 stavki, 2.110 veza šifara, 237 pravila, 95 jedinica, 15.456 zapisa dnevnika |
| `data/sefsync-pre-billing.db` | 32 MB | `da493fc1d9ba…` | kopija iste baze pre jedne izmene (230 pravila) |
| `data/predlozi-pravila.json` | 484 KB | `77c02f50360c…` | predlozi pravila iz analize |
| `data/izabrana-pravila.json` | 36 KB | `b4be89373351…` | izbor iz predloga |
| `data/pravila-pre-*.json` (3) | 116 KB | — | rezerve pravila od 15.09. |
| `data/analiza.csv` | 72 KB | `463b80d8576b…` | analiza razvrstavanja |
| `data/sefsync.log`, `sefsync-cli.log` | 1,1 MB | — | logovi razvojne mašine |
| `data/poslednja-baza.txt` | 1 KB | — | `sqlite:///H:/CLAUDE PROJECTS/elbraco-sef/data/sefsync.db` (čuvar promene baze) |

### Može se ponovo preuzeti ili napraviti

| šta | veličina | kako se vraća |
|---|---|---|
| `data/ubl/2026/01–09/` — 7.728 UBL faktura (01: 734, 02: 864, 03: 1.155, 04: 976, 05: 956, 06: 1.044, 07: 1.017, 08: 690, 09: 292) | 1,7 GB | ponovo sa SEF-a; preuzimanje obara status „Nova" u „Viđena" — pažnja |
| `data/uzorak/` — 300 UBL XML | 60 MB | isto |
| `data/1000053119.mp4` | 44 MB | snimak ekrana; poreklo nije zapisano |
| `objava/sefsync-paket.*.zip` — 113 paketa, od toga 9 nečitljivih | 2,7 GB | `napravi-paket.ps1` na odgovarajućem commitu |
| `.venv/` | — | `python -m venv .venv` pa `pip install -e .[dev]` |

---

## 4. Van projekta, na istom disku

* **Memorija asistenta** — `C:\Users\banes\.claude\projects\h--CLAUDE-PROJECTS-elbraco-sef\memory\`,
  9 fajlova (server panela, PROKNJIZENO, ažuriranje pomoći, PDV evidencija,
  provera konkretnog slučaja, prava u ERP-u, IZVOREPP, „ne čekaj neuspeh").
  Lozinki nema, ali ima imena naloga i servera, pa **nije stavljena u javni
  repo** dok se ne odluči gde. Zapis `server-panela.md` je od 16.09. i kaže da
  panel radi na MS SQL — između 21.09. i 08.10. je zapravo radio na SQLite (vidi
  presek 09.10.).
* **Transkripti razgovora** — isti folder, tri `.jsonl` fajla (~49 MB). Sadrže
  ispise sa servera i ERP-a, pa nisu za javni repo.

---

## 5. Gde je stao rad (09.10. 14:58)

Iz poslednje sesije, ništa nedovršeno u kodu:

* `provera-upisa` puštena na serveru: 476 nacrta, 473 u redu, 3 stvarna nalaza.
  `#8279` i `#8479` oslobođeni (`zaboravi-upis --stvarno`).
* **Ostalo Banetu na serveru:** `zaboravi-upis 8049 --stvarno` pa
  `nacrti --osvezi` (pubova `IFK-26-18998`, 6 g umesto 6.000). Pri ponovnom
  upisu treba da uđe 6.000 g — prvi upis u pubu sa faktorom pakovanja.
* Proveriti da `#8279` (`kp-2026248-POV`) posle `razvrstaj` ode na MAG010
  (novo pravilo `-POV`).
* Praćenje: `provera-prelaska` dnevno, `provera-upisa` nedeljno.
* Ostala otvorena pitanja: odeljak 4 u [PRESEK-09-10-2026.md](PRESEK-09-10-2026.md).

Testovi posle kvara (10.10.): **496 prošlo**, isto kao 09.10. — i `.venv` je dakle ispravan.
