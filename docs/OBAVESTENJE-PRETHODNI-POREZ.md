# Obaveštenje primaoca o prethodnom porezu

Ovo je zapis **činjenica iz dokumentacije**, a ne naše pretpostavke. Vraćali smo
se na ovaj problem tri puta i svaki put nagađali, pa je ovde zapisano sve što
piše u *API dokumentacija 31.07.2026*, poglavlje **1.89**, strane **174–181** —
sa brojem pravila, da se može proveriti.

Zašto zapis: 02.10. su četiri obaveštenja pala sa `400`, a ja sam dodao hvatanje
SEF-ove poruke i rekao da čekamo sledeći neuspeh. Nije bilo potrebe da se čeka —
uzrok stoji u tabeli polja na strani 177. Čekali smo nedelju dana nešto što je
sve vreme bilo zapisano.

---

## 1. Za koje dokumente se obaveštenje šalje

**Pravilo 1.** Obaveštenje dostavlja **primalac** dobara/usluga (ili uplatilac
avansa) **isporučiocu**, u slučaju:

* **smanjenja osnovice** za promet dobara/usluga, ili
* **smanjenja avansa** za budući promet.

**Pravilo 2.** Dostavlja se i u slučaju **storniranja** određenih dokumenata
(faktura za promet, avansna faktura, dokument o povećanju), odnosno za
**dokument o smanjenju koji nije storniran** i koji se odnosi na: fakturu za
promet, avansnu fakturu, ili na **vremenski period** u kojem su izdate fakture.

Dakle šest slučajeva, u dve grupe:

| Osnov (`basisOfNotice`) | Izvorni dokument (`documentReferenceType`) | Pokrivamo? |
|---|---|---|
| `CreditNoteIssued` | `CreditNoteReferenceToInvoice` — odobrenje na fakturu | **da** |
| `CreditNoteIssued` | `CreditNoteReferenceToPrepaymentInvoice` — odobrenje na avansnu fakturu | **da, od 08.10.** |
| `CreditNoteIssued` | `CreditNoteReferenceToPeriod` — odobrenje na period | **da, od 08.10. ispravno** |
| `Storno` | `StornoInvoice` — storno fakture | **ne** |
| `Storno` | `StornoPrepayment` — storno avansa | **ne** |
| `Storno` | `StornoDebitNote` — storno dokumenta o povećanju | **ne** |

**Tri slučaja sa stornom ne pokrivamo nikako** — ni u aplikaciji, ni u izveštaju,
ni u proveri. To nije „još nije stiglo na red“, to je dug koji do 08.10. nije bio
ni zapisan. Veličina se meri po `sef_status = Storno` nad dokumentima na kojima
smo odbili prethodni porez; odluka šta sa tim je Banetova, nije tehnička.

### Šta obaveštenje NE traži

* **Odobrenje sa nultim PDV-om.** `totalVatAmount` mora biti veći od nule
  (pravilo 4), pa obaveštenje za takvo odobrenje ne postoji.
* **Dokument izdat van SEF-a** (`DocumentIssuedOutsideSef`) — postoji u API-ju,
  ali mi takve dokumente ne vidimo, pa nas ne tiče.
* **Dokument o smanjenju koji je sâm storniran** (pravilo 2, „који није
  сторниран“).

### Nedokazana pretpostavka, zapisana kao takva

Mi šaljemo samo za odobrenja u statusu **`Approved`**. Dokumentacija to nigde ne
traži — ona govori o smanjenju osnovice, ne o prihvatanju na SEF-u. Pretpostavka
je da se osnovica kod nas ne smanjuje dok odobrenje ne prihvatimo, i dosad nije
proverena ni sa knjigovodstvom ni u propisu. **Ako je pretpostavka pogrešna,
dugujemo obaveštenja i za neprihvaćena odobrenja.**

---

## 2. Šta ide u zahtev

`POST /api/publicApi/recipients-notice-on-input-vat/sender/send`

| Polje | Obavezno | Šta tačno | Odakle kod nas |
|---|---|---|---|
| `noticeNumber` | da, < 200 znakova | **jedinstven broj obaveštenja** (pravilo 3) | broj odobrenja, doslovno |
| `totalVatAmount` | da, > 0 | PDV koji je **ispravljen / nije korišćen** kao prethodni porez (pravilo 4) | PDV sa odobrenja |
| `documentIssueOrigin` | da | `DocumentIssuedViaSef` | konstanta |
| `basisOfNotice` | da | `CreditNoteIssued` ili `Storno` | `CreditNoteIssued` |
| `documentReferenceType` | da | jedna od šest vrednosti iz tabele iznad | `vrsta_veze()` |
| `relatedDocumentNumber` | da | broj **dokumenta o smanjenju** (pravilo 9), odnosno storniranog dokumenta (pravilo 10) | broj odobrenja |
| `relatedDocumentIssueDate` | da | datum izdavanja dokumenta **za koji se obaveštenje izdaje** (pravilo 11) | datum odobrenja |
| `relatedInvoicePeriodStartDate` | **da kad je `…ToPeriod`** | početak perioda | `period_veze()` |
| `relatedInvoicePeriodEndDate` | **da kad je `…ToPeriod`** | kraj perioda | `period_veze()` |
| `relatedDocumentStornoDate` | **da kad je `Storno`** | datum storniranja | — ne radimo storno |
| `notes` | ne, < 2000 | napomena | ne šaljemo |
| `recipient.vatRegistrationCode` | da | PIB primaoca (pravilo 16) | PIB dobavljača |
| `recipient.registrationCode` | **da za korisnika javnih sredstava** (JBKJS), opciono matični za firmu (pravilo 17) | | `supplier_reg_no` |

### Zašto je padalo sa 400

**Pravila 12 i 13 kažu da su polja perioda „опционо“. Tabela polja na strani 177
kaže `Mandatory when DocumentReferenceTypes = CreditNoteReferenceToPeriod`.**
Tabela je u pravu. Sva četiri obaveštenja koja su pala 02.10. bila su odobrenja
preko više faktura, dakle `…ToPeriod`, a period nismo slali.

Drugi uzrok koji je čekao da se desi: **`registrationCode` nismo slali nikad**, a
za javna preduzeća je obavezan. Prvo knjižno odobrenje od JKP-a palo bi isto.

Pouka, i zato ovaj fajl postoji: **kad prozni opis i tabela polja ne govore isto,
tabela je ta koja važi** — i ne čeka se neuspeh za nešto što je zapisano.

### Odakle period

`period_veze()`, po pouzdanosti:

1. `cac:InvoicePeriod/StartDate` i `EndDate` iz UBL-a odobrenja (kolone
   `period_start`, `period_end`, uvedene 08.10.);
2. datumi izdavanja faktura na koje se odobrenje poziva (`cac:BillingReference`),
   nađenih u našem sopstvenom spisku — min i max;
3. datum samog odobrenja za oba polja. Gruba procena, ali **bez perioda zahtev
   sigurno pada**, pa je bolja od odustajanja.

---

## 3. Šta SEF ume da nam kaže

Ovo je najvažnije, i nije se koristilo:

| Poziv | Šta vraća |
|---|---|
| `GET …/sender/date-range?dateFrom=&dateTo=` | **sva obaveštenja koja smo poslali u periodu**, bilo kroz API bilo rukom na portalu |
| `GET …/sender/mistake-status` | obaveštenja u statusu „Greška u slanju“ — SEF ih ne šalje dalje, a mi bismo mislili da su otišla |
| `GET …/sender/{noticeId}` | jedno obaveštenje |
| `GET …/sender/{noticeId}/pdf` | PDF |

Svaki red iz `date-range` nosi `NoticeNumber`, `NoticeDate`, `SendingStatus`,
`PurchaseInvoiceId` i PIB primaoca. **`PurchaseInvoiceId` je dokaz** — direktno
kaže na koji naš dokument se obaveštenje odnosi.

Zbog toga pitanje „da li ovo odobrenje već ima obaveštenje“ **nikad nije trebalo
postavljati čoveku.** Panel vidi samo svoje pokušaje slanja; čovek je godinu dana
radio na portalu. Odgovor je sve vreme bio jedan HTTP poziv.

`sefsync uskladi-obavestenja --od 2025-01-01` pročita šta SEF zna i zabeleži to u
panelu (`--stvarno` da upiše). Poklapanje se traži po `PurchaseInvoiceId`, a kad
njega nema — po broju obaveštenja uz PIB dobavljača.

**Status se gleda.** Obaveštenje u statusu greške SEF **nije poslao dalje** —
dobavljač ga nema — pa se ne broji kao završeno, nego se prijavljuje posebno.
Tačan tekst statusa ne znamo (dokumentacija pominje samo „Greška u slanju“ i
poziv `mistake-status`), zato se prepoznaje po korenu reči, a svi viđeni statusi
se ispisuju da se spisak dopuni kad se pojavi nešto novo.

**Mereno 08.10.2026.** na periodu 01.01.2025.–08.10.2026.: SEF zna za **1.352**
obaveštenja, **79 od 81** naših odobrenja već ima svoje (sva po
`PurchaseInvoiceId`), a bez obaveštenja su ostala **dva**: `#8121` i `#8122`.
Dakle uzbuna oko „81 odobrenja bez obaveštenja“ bila je gotovo cela posledica
toga što panel vidi samo svoja slanja — Bogdan je posao odradio na portalu.

---

## 4. Kada se šalje iz aplikacije

Obaveštenje ide **uz prihvatanje knjižnog odobrenja na SEF-u** i ni u jednom
drugom trenutku (Bane, 30.09.: „to treba da ode prilikom prihvatanja sa sefa, u
tom trenutku za odgovarajuće k.o., ne treba masovna komanda“). Neuspeh **ne**
poništava prihvatanje — ono je već prošlo.

Zato odobrenje prihvaćeno **mimo aplikacije** — na portalu, ili pre prelaska
28.09. — nikad ne dobije obaveštenje od nas. Takvih je 08.10. bilo **81**, i ni
za jedno slanje nije bilo pokušano. Do 08.10. se to **na dokumentu nije videlo**:
cela sekcija stajala je pod `{% if obavestenje %}`, a zapis postoji samo ako je
slanje pokušano. Sada prihvaćeno odobrenje sa PDV-om uvek pokazuje stanje.

**Ponovno slanje je nepovratno prema dobavljaču.** Ako obaveštenje na portalu već
postoji, slanjem bi otišlo **drugo**. Zato postoji dugme „Već sam ga napravio na
portalu“, a pre bilo kakvog slanja ide usklađivanje sa SEF-om.

---

## 5. Šta ostaje nerešeno

1. **Tri slučaja sa stornom** (pravilo 2) ne pokrivamo. Prvo izmeriti koliko ih
   je, pa odlučiti.
2. **Pretpostavka o `Approved`** nije proverena — vidi odeljak 1.
3. **`noticeNumber` mora biti jedinstven.** Koristimo broj odobrenja. Dva
   dobavljača mogu izdati odobrenja istog broja (MISON ima šifre tipa „1“); tada
   drugo obaveštenje pada. Nije se desilo, ali je pitanje vremena.
4. **SEF-ov tekst greške `400`** još nemamo — hvatanje je dodato 07.10. Posle
   popravki iz ovog fajla verovatno ga nikad i nećemo videti, što je dobra vest
   za posao i loša za dijagnostiku.
