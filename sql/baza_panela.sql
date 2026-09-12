-- Baza panela za e-fakture na MS SQL-u.
--
-- ZASTO NA MS SQL-U
--   Panel je do sada pisao u SQLite fajl u C:\efakture\data\. Radi, ali taj
--   fajl ne ulazi u postojece bekape SQL Servera - neko bi morao posebno da
--   pazi na njega. Ovde je u istom rasporedu bekapa kao i ostale baze.
--
-- STA JE UNUTRA
--   Rad operatera: razvrstavanje, sta je prosledjeno, potvrde prijema,
--   pravila, istorija radnji. Dokumenti se mogu ponovo preuzeti sa SEF-a,
--   ovo ne moze.
--
-- Pokrenuti u SSMS-u kao sysadmin. Lozinku promeniti pre pokretanja.

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
IF SUSER_ID('sefsync_app') IS NULL
BEGIN
    CREATE LOGIN sefsync_app
        WITH PASSWORD = 'PROMENI-OVU-LOZINKU',
             CHECK_POLICY = ON,
             DEFAULT_DATABASE = SefSync;
    PRINT 'Nalog sefsync_app napravljen.';
END
ELSE
    PRINT 'Nalog sefsync_app vec postoji.';
GO

USE SefSync;
GO

IF USER_ID('sefsync_app') IS NULL
    CREATE USER sefsync_app FOR LOGIN sefsync_app;
GO

-- Aplikacija sama pravi i dopunjava tabele, pa joj treba i pravo na izmenu seme.
ALTER ROLE db_owner ADD MEMBER sefsync_app;
GO

PRINT 'Gotovo. U C:\efakture\.env upisati:';
PRINT 'DB_URL=mssql+pyodbc://sefsync_app:LOZINKA@localhost/SefSync?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes';
GO
