# Šta je u paketu — 30.09. do 03.10.2026.

Deset izmena, sve posle poslednjeg postavljanja. Za svaku: **šta se dešavalo**,
**zašto je tako rešeno** i **šta da gledaš posle postavljanja**. Detaljna
merenja su u [FAZA-2.md](FAZA-2.md), ovde je samo ono što treba pri postavljanju.

Redosled na serveru:

```
powershell -ExecutionPolicy Bypass -File C:\objave\postavi.ps1 -Arhiva C:\objave\sefsync-paket.zip -Stvarno
C:\efakture\.venv\Scripts\sefsync.exe pravila-uvoz C:\efakture\pravila.json
C:\efakture\.venv\Scripts\sefsync.exe nacrti --osvezi
```

Šema se dopunjava sama (`nacrt_kalkulacije.napomene`, `.ispravka`,
`.ispravka_zatvorio`, `.ispravka_zatvorena_u`) — ništa se ne radi ručno.

---

## 1. ID PDV evidencije se više ne piše napamet

**Bilo:** aplikacija je u svaku kalkulaciju pisala 1, pa je Bogdan ručno menjao
svako odobrenje, proviziju i račun bez PDV-a. Kolona se zove `LASTUSER` — ime
vara, to nije id korisnika.

**Sada:** 010 → 35, negativno na 088 → 34, račun sa PDV-om → 1, račun bez PDV-a
→ 75 ili 23 po dobavljaču, naučeno iz ERP-ove istorije (prag: bar 3 puta i bar
90 % njegovih zapisa). **21 app nikad ne bira sama** — upiše 1 i ostavi žutu
napomenu sa brojkama.

**Zašto tako:** mereno na 8.939 kalkulacija. Pogađa 94,8 % umesto 71,9 %.
Pravo na odbitak (21) se iz fakture ne vidi — NIS je 88:63, Lukoil 63:54.

**Gledaj:** da Bogdan na prvim novim kalkulacijama vidi tačan ID, i da žute
napomene nisu prečeste (procena: 3–4 dnevno).

## 2. Napomena preživljava upis

Nacrt se posle upisa više ne preračunava, pa bi se žuta napomena izgubila baš
kad zatreba — knjiži se posle upisa. Sada se čuva uz broj kalkulacije.

## 3. Povezana šifra važi i kad se veza ne pamti

**Bilo:** na knjižnom odobrenju se veza pamti samo kad čovek čekira kvačicu.
Bez kvačice **nije nastajalo ništa** — šifra se upisivala na stavku koju
`_nasa_sifra` nije čitala, uz zelenu poruku da je sve povezano i zapamćeno.
Ilijana je tako povezala Vivax 90911 i ostala na istom ekranu.

**Sada:** `_nasa_sifra` prvo uzima ono što je čovek rekao za tu stavku. Poruka
razlikuje „povezana i zapamćena“ od „povezana samo na ovoj fakturi“.

**Gledaj:** da Ilijani odobrenja prolaze iz prve.

## 4. Avans i otpis nisu artikli

Dijagnostika (`razlozi`) je „POVRAT AVANSA“ i „otpis neispravne robe“ svrstavala
pod „čovek mora“. Utiče samo na izveštaj.

## 5. Predlog komitenta kad brojevi promaše

**Bilo:** JKP Komunalac Kula postoji u šifarniku (00709, osam kalkulacija), ali
se nije našao: SEF za javna preduzeća šalje JBKJS (82080) umesto matičnog
(08004790), a PIB mu je u šifarniku bio zapisan sa nulom viška.

**Sada:** kad promaši po svim brojevima, razlog zaustavljanja nosi i predlog —
*„Možda je 00709 Komunalac JKP, Kula — u šifarniku mu stoji matični 08004790, a
na fakturi 82080“*. Ključ je skup reči iz imena i mesta, bez pravnog oblika.

**Zašto tako:** 96 % od 45.105 komitenata ima jednoznačan skup reči. Predlog ide
samo kad je kandidat jedan. App ne bira dobavljača sama i ne dira šifarnik —
peglanje brojeva po dužini bi bilo pogađanje identiteta (2.252 reda ima „PIB“
koji nije devetocifren).

## 6. „Štampaj“ više ne javlja grešku iz prve

**Bilo:** SEF na prvi poziv vrati **status 200 i JSON od 85 bajtova**
(`„Extended PDF has not been generated…“`) i tek tim pozivom pokrene pravljenje.
Panel je to slao pregledaču kao `application/pdf`.

**Sada:** čeka se do 7 s; ruta nikad ne šalje nešto što ne počinje na `%PDF-`.

**Zašto tako:** mereno na pet netaknutih dokumenata — SEF-u treba 2–3 sekunde.

## 7. Ciklus dovršava upise koji su čekali šifru

**Bilo:** potvrda prijema pokuša upis odmah; ako nacrt tada nije spreman, upis
se odbije — i niko ga ne pokrene ponovo. Vitelova R-1382/26VP je tako stajala
dva dana: prijem potvrđen, nacrt spreman, u ERP-u ništa.

**Sada:** petnaestominutni ciklus dovršava takve upise, najviše 20 po prolazu
(`DOVRSI_UPISE_LIMIT`).

**Zašto smemo automatski:** čovek **jeste** kliknuo „potvrdi prijem“ — ovo
dovršava tu nameru, ne donosi novu odluku. Preskaču se fakture od pre prelaska
i one sa otvorenim manjkom, kalkulacija ulazi nezaknjižena.

**Gledaj:** u logu red „Dovršeno upisa: N od M“. Ručni spisak:
`sefsync dovrsi-upise` (bez `--stvarno`).

## 8. Šifra pod kojom stoji mnogo naziva nije šifra artikla

**Bilo:** Eurotehna na svakom odobrenju šalje `Porez 20%` kao „šifru artikla“
(109 naziva pod njom), EWE `USL00027`, Comtrade `R0001`. Jedna veza za takvu
oznaku štampala je isti artikal na svako sledeće odobrenje — 661FN-2026 je
dobilo frižider sa 613FN-2026. Gore: stavka se razreši, nacrt postane spreman,
pa se **predlog iz napomene nikad ne izračuna**, iako pogađa 9 od 9.

**Sada:** na odobrenju se šifra dobavljača ne koristi kad pod njom stoji **tri
ili više različitih naziva**. Na fakturama se ništa ne menja.

**Zašto taj prag:** mereno na 28.310 stavki — prag 3+ izdvaja 13 šifara i 425
stavki, a **nijedna nema vezu**, pa se ničemu što radi ne staje na put. Prag
„više od jednog“ bi dirao 35 veza bez potrebe.

**Grana po nazivu se NE dira:** naziv koji se ponavlja je normalan („LED TV
55PUS7810 PHILIPS“ na 45 dokumenata je jedan artikal); blokiranje bi oborilo
240 veza koje rade.

**Gledaj:** `sefsync opste-sifre` — pre postavljanja kod `Porez 20%` piše
`VEZA -> 92846`; posle toga se ta veza više ne koristi na odobrenjima.

## 9. Ispravka pogrešno povezane šifre posle upisa

**Bilo:** kad se pogrešna šifra otkrije pošto je kalkulacija otišla u ERP, nije
bilo načina da se ispravi iz aplikacije.

**Sada:** operater na dokumentu koji je u ERP-u ima „Pogrešna šifra artikla?“.
App popravi vezu u šifarniku, pročita `PROKNJIZENO` iz ERP-a i javi jedinici
mejlom i crvenim okvirom šta tačno treba. Novi tab **Pretraga** traži kroz sve
uključujući arhivu, i po šifri artikla, barkodu i nazivu stavke.

**Zašto app ne menja kalkulaciju:** nalog ima `db_datareader` i **INSERT** na
pet tabela, bez UPDATE i DELETE. Razmatrano i odbijeno da se traži UPDATE —
dobitak bi bio jedno polje koje poslovođa ionako otvara, a izgubilo bi se
pravilo „app samo dodaje, nikad ne menja“.

## 10. Šifra koje nema u šifarniku ne zaustavlja nacrt

**Bilo:** 3753VP-2026 je stajala kao „zaustavljena“ zbog šifre 87622 koje nema
u šifarniku (omaška — postoje 87620 i 87621). Pod „zaustavljeno“ se forma za
povezivanje ne nudi, pa se faktura nije imala kako odglaviti.

**Sada:** takva stavka čeka šifru, uz razlog, i poveže se kao i svaka druga.
Uz to `povezi_sifru` proverava šifru u `ARTIKLI/099` i odbija omašku odmah.
Kad ERP ćuti, provera se preskače — ona je pomoć, ne uslov.

---

## 11. Otvaranje nove šifre artikla iz aplikacije

Oko **60 mesečno**, do sada ručni posao. Ekran **Šifre** → „otvori šifru“.
Upisuje `ARTIKLI` (099 + 11 magacina) i `ARTPROD` po prodajnim mestima **sa
cenom**, u jednoj transakciji.

* **Barkod je obavezan** i ne izmišlja se. Baza ga traži jedinstvenim
  (`UQ(SIFRAMAG, BARCODE)`). To što ga nema na fakturi ne znači da ga artikal
  nema — za to postoji slikanje. Šifra umesto barkoda ide samo kad čovek
  izričito kaže da ga nema.
* **Maloprodajna cena je uslov**, veća od nule. Predlaže se samo kad je kod tog
  dobavljača marža uska (bar 20 stavki, raspon 25–75 ≤ 10 p.p.), zaokruženo na
  100; inače polje ostaje prazno uz objašnjenje.
* **`DARTIKLI` se ne dira** — puni je noćni posao na serveru.

**Traži dozvole:** `GRANT INSERT ON dbo.ARTIKLI`, `dbo.ARTPROD`.

## 12. Otvaranje novog komitenta iz aplikacije

Dobavljač koga nema u `NAZIVI` potpuno zaustavlja kalkulaciju (3 od 198 u
istoriji). Na dokumentu, ispod razloga, veza „otvori komitenta u šifarniku“.

* Naziv se skraćuje na 30 znakova po pravilu iz samog šifarnika (brend, pa
  oblik, bez tačaka) — **47 % tačno, 44 % skoro**, pa ide na potvrdu; pun naziv
  ostaje u `APRNAZIV`.
* Duplikat: matični i PIB su dokaz (nađen po broju → ne dozvoljava se
  otvaranje), ime i mesto su samo sumnja.
* Upisuje se jedan red; `ANALITIKA` se proverava u trenutku upisa.

**Traži dozvolu:** `GRANT INSERT ON dbo.NAZIVI`.

---

## Šta ostaje otvoreno

* ~~**Razdvojiti ERP nalog.**~~ **Urađeno 07.10.** Server je na `sefsync_erp`,
  razvojna mašina na `sefsync_dev` bez prava upisa u produkciju, a panel više ne
  pozajmljuje Logistikinu konekciju. `elbraco_dev_ro` je nedirnut jer ga koristi
  drugi projekat; ostaje da mu se, kad novi nalog odradi nekoliko dana, oduzme
  INSERT na `DARTIKLI` (ne koristi ga nijedan projekat). Vidi
  [PRELAZAK.md](PRELAZAK.md), merenje u [FAZA-2.md](FAZA-2.md).
* **Obaveštenje o prethodnom porezu kod storna.** Propis ga traži i za storno
  fakture, avansa i dokumenta o povećanju (poslovno pravilo 2) — mi te tri vrste
  **ne pokrivamo nikako**. Prvo izmeriti koliko ih je. Sve činjenice su u
  [OBAVESTENJE-PRETHODNI-POREZ.md](OBAVESTENJE-PRETHODNI-POREZ.md).
* **Automatsko prosleđivanje** — pustiti `analiza-razvrstavanja` posle nekoliko
  dana sa očišćenim pravilima, pa videti gde je app sigurna.
* **Veza po nazivu na odobrenjima.** Ako neko čekira „zapamti vezu“, naziv može
  da odigra istu ulogu kao `Porez 20%` kod EWE-a, čiji se nazivi ponavljaju.
  Nije se desilo, pa se ne dira unapred.
* Dule: maloprodajne cene za 200152 i 2000153 u objektu 071 (Coffee Center).
* Bogdan: ponovo povezati LEXA LX-302 (LX-701 je rešen).
* Nina: pravila po napomeni za Radmilo Aleksić PR Sky.
