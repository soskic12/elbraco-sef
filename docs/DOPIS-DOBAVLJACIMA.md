# Dopis dobavljačima

Kratak, jedan ekran, bez tehničkog žargona. Šalje se komercijali; tehnički
dodatak na dnu je za njihovog programera ako ga zatraže.

---

**Predmet: Molba — oznaka objekta na e-fakturi**

Poštovani,

Od ovog meseca u Elbraco Group ulazne e-fakture obrađujemo automatski. Čim
faktura stigne sa SEF-a, naš sistem je pročita, prepozna kom našem objektu
pripada i odmah je prosledi poslovođi tog objekta — pre nego što roba stigne.

Da bi to radilo, iz fakture mora da se vidi **u koji objekat roba ide**.

**Ne tražimo da menjate način na koji radite.** Naš sistem uči od svakog
dobavljača posebno i traži oznaku tamo gde je vi već pišete — u napomeni, u
broju narudžbenice, u referenci ili u prilogu. Svaki od tih načina nam
odgovara.

Molimo samo jedno: **da oznaka objekta bude na svakoj fakturi, i uvek na istom
mestu.** Kad izostane ili se premesti, tu fakturu mora ručno da razvrsta naš
službenik, pa se prijem robe u objektu odloži.

Šta nam je dovoljno — bilo koje od ovoga:

- naziv mesta objekta (Sombor, Apatin, Odžaci, Kula, Bačka Palanka, Bečej,
  Vrbas, Sombor 2, Subotica)
- adresa isporuke objekta, ako je već upisujete
- vaša interna šifra našeg objekta, ako je koristite — nju ćemo jednom upamtiti

Ako vam je zgodnije da to dogovorimo tehnički, naš kontakt je ispod i rado
ćemo se javiti vašoj službi.

Hvala unapred — ovo skraćuje put robe do naših objekata i ubrzava prihvatanje
vaših faktura na SEF-u.

S poštovanjem,
**Elbraco Group d.o.o.**
Srpskih vladara 46, 25260 Apatin
PIB 104220952

---

## Tehnički dodatak (na zahtev)

Poželjno je popuniti standardno UBL polje za mesto isporuke:

```xml
<cac:Delivery>
  <cac:DeliveryLocation>
    <cac:Address>
      <cbc:StreetName>Glavna 18</cbc:StreetName>
      <cbc:CityName>Bečej</cbc:CityName>
      <cbc:PostalZone>21220</cbc:PostalZone>
    </cac:Address>
  </cac:DeliveryLocation>
</cac:Delivery>
```

Ako to nije izvodljivo, prihvatamo oznaku objekta u bilo kom od ovih polja,
pod uslovom da je dosledna:

| Polje | Primer |
|---|---|
| `cbc:Note` | `BEČEJ Po otpremnici: 26-30C-088989` |
| `cac:OrderReference/cbc:ID` | `Becej` |
| `cac:AdditionalDocumentReference/cbc:ID` | `Elbraco - Bečej` |
| PDF prilog | `Poslovnica B000067561 / Glavna 18 / 21220 Bečej` |

**Naši objekti:**

| Objekat | Adresa |
|---|---|
| Sombor | Staparski put S-16, 25000 Sombor |
| Sombor 2 | Vojvođanska 46, 25000 Sombor |
| Apatin | Srpskih vladara 46, 25260 Apatin |
| Odžaci | Somborska 28, 25250 Odžaci |
| Kula | Maršala Tita 149, 25230 Kula |
| Bačka Palanka | Jugoslovenske armije 116, 21400 Bačka Palanka |
| Bečej | Glavna 18, 21220 Bečej |
| Vrbas | Save Kovačevića 1B, 21460 Vrbas |
| Subotica | Braće Radić 43, 24000 Subotica |
| Old Brick Pub | Matije Gupca 55, 25000 Sombor |
| Magacin Sombor | Sivački put 30, 25000 Sombor |

> Napomena: adresa sedišta (Srpskih vladara 46, Apatin) je i adresa objekta
> Apatin. Ako je upisujete kao adresu isporuke po automatizmu, za nas to znači
> „roba ide u Apatin" — pa je molba da je upisujete samo kada roba stvarno ide
> tamo.
