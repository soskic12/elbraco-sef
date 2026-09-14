-- Baza panela za e-fakture na MS SQL-u.
--
-- ZASTO NA MS SQL-U
--   Panel je u pocetku pisao u SQLite fajl u C:\efakture\data\. Radi, ali taj
--   fajl ne ulazi u postojece bekape SQL Servera - neko bi morao posebno da
--   pazi na njega. Ovde je u istom rasporedu bekapa kao i ostale baze.
--
-- STA JE UNUTRA
--   Rad operatera: razvrstavanje, sta je prosledjeno, potvrde prijema,
--   pravila, istorija radnji. Dokumenti se mogu ponovo preuzeti sa SEF-a,
--   ovo ne moze.
--
-- ZASTO LOZINKA NIJE U FAJLU
--   Skripta je sama napravi i ispise jednom, na kraju. Ranije je ovde stajao
--   placeholder koji je svako ko pokrece skriptu menjao pravom lozinkom - a
--   fajl je u git-u, pa je pitanje dana kad bi tako otisla u repozitorijum.
--   Ispisanu vrednost prekopirati u C:\efakture\.env i vise je nigde ne cuvati.
--
-- KOLACIJA
--   Tekstualne kolone aplikacija pravi kao NVARCHAR, pa cirilica u nazivima
--   dobavljaca ("ЈП Водоканал Бечеј") ostaje citljiva bez obzira na kolaciju.
--
-- Pokrenuti u SSMS-u kao sysadmin.

USE master;
GO

IF DB_ID('SefSync') IS NULL
BEGIN
    CREATE DATABASE SefSync;
    PRINT 'Baza SefSync napravljena.';
END
ELSE
    PRINT 'Baza SefSync vec postoji.';
GO

-- Nalog samo za ovu aplikaciju. Ne koristi se ni za sta drugo.
DECLARE @lozinka NVARCHAR(64);
DECLARE @sql NVARCHAR(MAX);

IF SUSER_ID('sefsync_app') IS NULL
BEGIN
    -- Nasumicna lozinka: dva GUID-a bez crtica, plus znaci da zadovolji politiku.
    SET @lozinka = 'Sf' + REPLACE(CONVERT(NVARCHAR(36), NEWID()), '-', '')
                 + '#' + LEFT(REPLACE(CONVERT(NVARCHAR(36), NEWID()), '-', ''), 8);

    SET @sql = 'CREATE LOGIN sefsync_app WITH PASSWORD = ' + QUOTENAME(@lozinka, '''')
             + ', CHECK_POLICY = ON, DEFAULT_DATABASE = SefSync;';
    EXEC sp_executesql @sql;

    PRINT '';
    PRINT '=================================================================';
    PRINT ' Nalog sefsync_app je napravljen. Lozinka (prekopiraj je odmah):';
    PRINT '';
    PRINT '   ' + @lozinka;
    PRINT '';
    PRINT ' Ovo je jedini put da se vidi. Ide u C:\efakture\.env kao deo DB_URL.';
    PRINT '=================================================================';
END
ELSE
BEGIN
    PRINT 'Nalog sefsync_app vec postoji - lozinka se ne menja.';
    PRINT 'Ako ti treba nova:  ALTER LOGIN sefsync_app WITH PASSWORD = ''...'';';
END
GO

USE SefSync;
GO

IF USER_ID('sefsync_app') IS NULL
    CREATE USER sefsync_app FOR LOGIN sefsync_app;
GO

-- Aplikacija sama pravi i dopunjava tabele, pa joj treba i pravo na izmenu seme.
ALTER ROLE db_owner ADD MEMBER sefsync_app;
GO

PRINT '';
PRINT 'U C:\efakture\.env upisati (LOZINKA je ona ispisana gore):';
PRINT 'DB_URL=mssql+pyodbc://sefsync_app:LOZINKA@localhost/SefSync?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes';
PRINT '';
PRINT 'Ako lozinka sadrzi @ ili / , zameniti ih sa %40 odnosno %2F.';
GO
