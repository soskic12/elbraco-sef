# E-fakture — uputstvo za rad

`http://efakture.technolandkartica.net` · prijava **istim nalogom kojim ulaziš u ERP**

---

## Šta aplikacija radi sama

Svakih 15 minuta povuče sa SEF-a sve nove ulazne dokumente — fakture, knjižna
odobrenja, avanse. Pročita ih i **predloži** kojoj poslovnoj jedinici pripadaju:
po adresi isporuke, po oznaci u narudžbenici, po dobavljaču.

**Predlog nije odluka.** Aplikacija ne šalje ništa nikome dok ti ne klikneš.

---

## Šta radiš ti

Ceo posao je na jednom ekranu, u četiri kartice.

### „Traži potez“ — tvoj red

Ovde stoji sve što još nije završeno. Dokument izlazi iz reda tek kad su **obe**
stvari gotove: rešen na SEF-u (prihvaćen ili odbijen) i prosleđen poslovnoj
jedinici. Zato ništa ne može da ti promakne.

Za svaki dokument:

**1. Pogledaj poslovnu jedinicu.** U koloni stoji šifra i oznaka odakle je
predlog došao — `pravilo`, `dobavljač`, ili zelena kvačica ako je već provereno.

- **Predlog je tačan** → označi dokument i klikni **Potvrdi razvrstavanje**.
  (Ako ga odmah prosleđuješ, ne moraš — prosleđivanje se računa kao potvrda.)
- **Predlog je pogrešan** → izaberi pravu jedinicu iz liste i klikni ✓ pored nje.
  Aplikacija tada **zapamti pravilo** i sledeći put takav dokument razvrsta sama.
  Usput zabeleži da je staro pravilo pogrešilo, pa se vidi koje treba popraviti.

**2. Reši na SEF-u.** Označi dokumente i **Prihvati na SEF-u** — može i više
odjednom. Odbijanje ide pojedinačno, otvori dokument i upiši razlog (SEF ga
traži).

**3. Prosledi poslovnoj jedinici.** Označi i klikni **Prosledi**. Poslovođa
dobija mejl sa zaglavljem, stavkama i UBL-om u prilogu, i link ka dokumentu.

Prihvatanje i prosleđivanje su **nezavisni** — radi ih kojim redom odgovara tom
dokumentu. Nekad prvo prihvatiš pa prosleđuješ, nekad obrnuto.

### „Nerazvrstano“ — aplikacija nije znala

Dokumenti bez adrese isporuke i bez pravila koje ih prepoznaje. Dodeliš jedinicu,
ostavljeno je čekirano „napravi pravilo“, i taj dobavljač se više ne pojavljuje
ovde.

### „Čeka potvrdu prijema“ — lopta je kod objekta

Prosleđeno, a poslovođa još nije potvrdio da je roba stigla. Kad potvrdi, dobijaš
mejl i dokument izlazi iz ovog spiska.

### „Arhiva“ — završeno i istorija

Sve pre puštanja aplikacije u rad, plus ono što si sklonila. Pretraživo, ne traži
ništa od tebe.

> **Pretraga radi unutar kartice na kojoj si.** Ako tražiš stariji dokument,
> prvo otvori „Arhiva“.

---

## Šta poslovođa vidi

Samo svoju jedinicu i samo ono što mu je prosleđeno. Ne vidi tuđe dokumente,
ne vidi šifarnike, ne može ništa da menja. Jedino dugme koje ima je **„Roba je
primljena“**, uz napomenu ako nešto ne valja.

---

## Zašto neki dokument nema stavke

Ako piše **„nije otvarana“** umesto adrese isporuke: dokument je na SEF-u još u
statusu *Nova*, a aplikacija ga namerno ne otvara — otvaranje bi mu oborilo status
i poremetilo rad na portalu. Vidiš dobavljača, iznos i datum, i možeš ga
prihvatiti. Stavke i adresa stižu čim ga neko otvori na portalu.

Ako ti trebaju odmah: otvori dokument → **Osveži sa SEF-a**. Time se status menja
u *Viđena*, što je isto kao da si ga otvorila na portalu.

---

## Šta je rezultat tvog rada

| Šta uradiš | Šta od toga ostaje |
|---|---|
| Potvrdiš ili ispraviš predlog | Svaki dokument ima tačnu poslovnu jedinicu |
| Ispraviš pogrešan predlog | Novo pravilo — taj slučaj se više ne ponavlja |
| Prihvatiš ili odbiješ | Pravni odgovor dobavljaču, zapisan sa tvojim imenom |
| Prosleđuješ | Poslovođa zna šta mu stiže, pre nego što roba dođe |
| — | Merljiva tačnost razvrstavanja, i spisak pravila koja greše |

Cilj je da ti posao vremenom bude sve manje ručan: što više ispravki uneseš, to
aplikacija više pogađa sama. Na kartici **Tačnost** se vidi koliko trenutno
pogađa i koja pravila prave problem.

Sve što uradiš nosi tvoje ime i vreme — na dokumentu, pod „Istorija“.

---

## Probni režim

Dok je uključen, **mejlovi ne idu u objekte** nego samo na jednu adresu, a u
naslovu piše `[PROBA → MP002]` — kome bi otišlo u redovnom radu. Slobodno vežbaj,
niko u objektima ništa neće dobiti.

---

## Šta još ne radi (faza 2)

Ulazna kalkulacija se još ne pravi u ERP-u. Sledeći korak je da isti klik
„Prosledi“ poslovođi spremi i kalkulaciju sa stavkama, tako da mu ostane samo
da je zaknjiži.
