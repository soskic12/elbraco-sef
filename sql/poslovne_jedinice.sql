-- Izvor šifarnika poslovnih jedinica za `sefsync pj-sync`.
-- Vrednost se upisuje u .env kao BU_SOURCE_SQL (u jednom redu).
--
-- Sve tabele su u ERP bazi (dev: ELBSX_2026 na 194.146.57.216:1433).
-- Šeme preuzete iz ELBRACO LOGISTIKA / ElbracoErpConnector.cs:
--   dbo.PRODAV            maloprodajni objekti, ima ADRESA
--   dbo.PRODAV_KONTAKT    Region (mesto), PTTBroj, kontakti  (SIFRAPRO)
--   dbo.MAGACINI          svi magacini (pun spisak)
--   dbo.MAGACIN_KONTAKT   adresa/mesto/PTT/kontakti magacina (SIFRAMAG, samo 3 reda)
--   dbo.kontakti_osobe    odgovorne osobe; [User] je rezervisana reč
--
-- Kolone su CHAR, pa se svuda ide sa RTRIM — inače šifre nose repove razmaka
-- i JOIN-ovi tiho ne rade.
--
-- Šifre se prefiksuju (MP001, MAG021) jer prodavnice i magacini dele brojeve
-- (005 je i B.Palanka i magacin izlaznih e-faktura); erp_code ostaje sirov.
--
-- ROBNO = 'Ne', nazivi sa "USLUG" i tehnički magacini nisu odredište isporuke,
-- pa idu kao routable = 0: vide se u šifarniku i mogu se dodeliti ručno,
-- ali ih automatsko razvrstavanje nikad ne bira.
--
-- Napomena: STRING_AGG traži SQL Server 2017+. Na starijem serveru zameniti sa
-- FOR XML PATH('') konstrukcijom.

SELECT
    'MP' + RTRIM(p.SIFRA)                            AS code,
    RTRIM(p.NAZIV)                                   AS name,
    'maloprodaja'                                    AS kind,
    RTRIM(p.SIFRA)                                   AS erp_code,
    RTRIM(p.ADRESA)                                  AS address,   -- rep "tel.421 783" čisti aplikacija
    RTRIM(k.Region)                                  AS city,
    RTRIM(k.PTTBroj)                                 AS postal_code,
    NULLIF(CONCAT_WS(',', k.EmailProdavnice, k.EmailPoslovodje, o.Emails), '')   AS emails,
    NULLIF(CONCAT_WS(',', k.TelefonProdavnice, k.TelefonFiksni, o.Telefoni), '') AS phones,
    ISNULL(k.Aktivan, 1)                             AS active,
    CASE WHEN RTRIM(p.ROBNO) = 'Ne' OR p.NAZIV LIKE '%USLUG%' THEN 0 ELSE 1 END AS routable
FROM dbo.PRODAV p
LEFT JOIN dbo.PRODAV_KONTAKT k ON RTRIM(k.SIFRAPRO) = RTRIM(p.SIFRA)
LEFT JOIN (
    -- poslovođa i zamenik po objektu -> primaoci obaveštenja
    SELECT RTRIM(SifraPovezanogObjekta) AS Sifra,
           STRING_AGG(NULLIF(RTRIM(Email), 'nepoznato'), ',')          AS Emails,
           STRING_AGG(NULLIF(RTRIM(MobilniTelefon), 'nepoznato'), ',') AS Telefoni
    FROM dbo.kontakti_osobe
    WHERE Aktivan = 1
      AND SifraPovezanogObjekta IS NOT NULL
      AND RTRIM(SifraPovezanogObjekta) <> ''
      AND RTRIM(Funkcija) IN ('POSLOVODJA', 'ZAMENIK POSLOVODJE')
    GROUP BY RTRIM(SifraPovezanogObjekta)
) o ON o.Sifra = RTRIM(p.SIFRA)

UNION ALL

SELECT
    'MAG' + RTRIM(m.SIFRA),
    RTRIM(m.NAZIV),
    'magacin',
    RTRIM(m.SIFRA),
    RTRIM(k.Adresa),
    RTRIM(k.Mesto),
    RTRIM(k.PTT),
    RTRIM(k.EmailOpsti),
    NULLIF(CONCAT_WS(',', k.TelefonOpsti, k.TelefonFiksni), ''),
    ISNULL(k.Aktivan, 1),
    -- Od 71 magacina samo tri su fizička mesta (021, 026, 029) — ostalo su
    -- knjigovodstveni magacini: INTERNI PRENOSI, ULAZNI, OŠTEĆENA ROBA,
    -- IZDATA ROBA, REZERVACIJA, 005 MAGACIN EF, 010 Povrat robe...
    -- Odredište isporuke je onaj koji ima upisanu adresu u MAGACIN_KONTAKT.
    CASE
        WHEN RTRIM(m.SIFRA) IN ('005', '010') THEN 0
        WHEN k.Adresa IS NULL OR RTRIM(k.Adresa) = '' THEN 0
        ELSE 1
    END
FROM dbo.MAGACINI m
LEFT JOIN dbo.MAGACIN_KONTAKT k ON RTRIM(k.SIFRAMAG) = RTRIM(m.SIFRA)

UNION ALL

-- Uprava/finansije: fakture za usluge (banke, Telekom, komunalije, Sokoj)
-- i rabatna knjižna odobrenja idu ovde.
SELECT
    'OFFICE',
    'UPRAVA — FINANSIJE',
    'uprava',
    NULL,
    NULL,
    NULL,
    NULL,
    STRING_AGG(RTRIM(o.Email), ','),
    STRING_AGG(RTRIM(o.MobilniTelefon), ','),
    1,
    1
FROM dbo.kontakti_osobe o
WHERE o.Aktivan = 1 AND RTRIM(o.Funkcija) = 'FINANSIJE';
