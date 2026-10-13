# Pravi sefsync-paket.zip za postavljanje na server.
#
# ZASTO SKRIPTA
#   Prvi paket je sklapan rucno i to se videlo: u njemu je zavrsio build\ i
#   egg-info, a kad je dodat pypdf niko nije dopunio wheelhouse - pa bi
#   instalacija na serveru bez interneta pukla. Pakovanje mora da bude jedna
#   komanda koja uvek uzme isto.
#
# STA ULAZI
#   src\sefsync    kod (bez __pycache__)
#   wheelhouse     tockovi za offline instalaciju, za 3.12 i 3.13
#   pravila.json   sifarnik PJ, pravila razvrstavanja i mapiranja artikala
#   postavi.ps1, iis-poddomen.ps1, restart.ps1, sql\, docs\, .env.server
#
# STA NE ULAZI
#   .env       lozinke ovog racunara - na serveru je svoj
#   data\      baza i UBL-ovi - serverski su stariji od svega ovoga
#   tests\     na serveru se ne pokrecu
#
# KAKO SE KORISTI
#   powershell -ExecutionPolicy Bypass -File .\napravi-paket.ps1
#   powershell -ExecutionPolicy Bypass -File .\napravi-paket.ps1 -BezPravila

[CmdletBinding()]
param(
    [string]$Izlaz      = 'objava\sefsync-paket.zip',
    [string[]]$Verzije  = @('312', '313'),
    [switch]$BezPravila
)

$ErrorActionPreference = 'Stop'
$koren = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $koren

function Korak($tekst) { Write-Host "`n== $tekst" -ForegroundColor Cyan }

# --------------------------------------------------------------------------- #

Korak 'Provere'

$py = Join-Path $koren '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "Nema .venv - napravi ga pre pakovanja ($py)" }
Write-Host "   python: $py"

# Paket nosi kod iz radnog foldera, ne iz poslednjeg commita. Ako se to dvoje
# razlikuje, na server ode nesto sto ne postoji ni u jednoj verziji - pa se bar
# kaze naglas.
$prljavo = & git status --porcelain 2>$null
if ($prljavo) {
    Write-Host '   PAZI: ima neispracenih izmena - pakuje se radni folder:' -ForegroundColor Yellow
    $prljavo | ForEach-Object { Write-Host "      $_" -ForegroundColor Yellow }
} else {
    $commit = (& git log -1 --pretty='%h %s' 2>$null)
    Write-Host "   commit: $commit"
}

# --------------------------------------------------------------------------- #

$privremeno = Join-Path $env:TEMP ("paket-" + (Get-Date -Format 'yyyyMMddHHmmss'))
New-Item -ItemType Directory -Force -Path $privremeno | Out-Null

Korak "Kod -> $privremeno"

New-Item -ItemType Directory -Force -Path (Join-Path $privremeno 'src') | Out-Null
Copy-Item 'src\sefsync' (Join-Path $privremeno 'src') -Recurse -Force
# __pycache__ nosi .pyc gradjen za Python ovog racunara; na serveru je smece
# koje ume i da zbuni (stari .pyc pored novog .py).
Get-ChildItem (Join-Path $privremeno 'src') -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force

foreach ($f in @('pyproject.toml', 'README.md', '.env.server',
                 'postavi.ps1', 'iis-poddomen.ps1', 'restart.ps1')) {
    Copy-Item $f $privremeno -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $privremeno 'sql') | Out-Null
Copy-Item 'sql\*.sql' (Join-Path $privremeno 'sql') -Force

New-Item -ItemType Directory -Force -Path (Join-Path $privremeno 'docs') | Out-Null
foreach ($d in @('POSTAVLJANJE.md', 'UPUTSTVO-ZA-OPERATERA.md')) {
    Copy-Item (Join-Path 'docs' $d) (Join-Path $privremeno 'docs') -Force
}

# nssm.exe se ne pakuje (nije nas softver), ali folder i uputstvo idu - da se na
# serveru zna gde ga ocekujemo.
New-Item -ItemType Directory -Force -Path (Join-Path $privremeno 'alati') | Out-Null
@'
Ovde treba da stoji nssm.exe (64-bit).

  winget install NSSM.NSSM
  pa kopiraj nssm.exe iz instalacije ovde

ili ga skini sa https://nssm.cc/download (win64\nssm.exe).

Bez njega postavi.ps1 uradi sve ostalo i samo upozori da servis nije registrovan.
'@ | Set-Content (Join-Path $privremeno 'alati\PROCITAJ.txt') -Encoding utf8

Write-Host ('   kod: {0} fajlova' -f (Get-ChildItem $privremeno -Recurse -File).Count)

# --------------------------------------------------------------------------- #

Korak 'Pravila i mapiranja'

if ($BezPravila) {
    Write-Host '   preskoceno (-BezPravila)'
} else {
    # Pravila razvrstavanja i mapiranja artikala nisu u kodu nego u bazi, pa bez
    # ovoga server ostaje na starim pravilima i posle nove verzije.
    $json = Join-Path $privremeno 'pravila.json'
    & (Join-Path $koren '.venv\Scripts\sefsync.exe') pravila-izvoz --fajl $json
    if ($LASTEXITCODE -ne 0) { throw 'Izvoz pravila nije prosao.' }
    Copy-Item $json (Join-Path $koren 'objava\pravila.json') -Force
}

# --------------------------------------------------------------------------- #

Korak 'Tockovi za offline instalaciju'

$tocak = Join-Path $privremeno 'wheelhouse'
New-Item -ItemType Directory -Force -Path $tocak | Out-Null

# Zavisnosti se citaju iz pyproject.toml, da se spisak ne prepisuje na dva mesta
# (tako je pypdf i ostao van proslog paketa).
$zavisnosti = & $py -c @"
import tomllib
p = tomllib.load(open('pyproject.toml','rb'))['project']
for d in p['dependencies'] + p['optional-dependencies']['mssql']:
    print(d)
"@
if ($LASTEXITCODE -ne 0) { throw 'Citanje zavisnosti iz pyproject.toml nije proslo.' }
Write-Host ('   zavisnosti: {0}' -f ($zavisnosti -join ', '))

# pip, setuptools i wheel idu u paket jer postavi.ps1 prvo njih instalira iz
# wheelhouse-a (server je bez interneta).
$alat = @('pip', 'setuptools', 'wheel')

foreach ($v in $Verzije) {
    Write-Host "   Python $($v[0]).$($v.Substring(1)):" -NoNewline
    & $py -m pip download @zavisnosti @alat --dest $tocak --quiet `
        --only-binary=:all: --implementation cp --platform win_amd64 `
        --python-version $v --abi "cp$v"
    if ($LASTEXITCODE -ne 0) {
        throw "Preuzimanje tockova za Python $v nije proslo."
    }
    Write-Host ' ok'
}

$brojTockova = (Get-ChildItem $tocak -Filter '*.whl').Count
Write-Host "   tockova: $brojTockova"
foreach ($obavezan in @('pypdf', 'pyodbc', 'lxml', 'fastapi')) {
    if (-not (Get-ChildItem $tocak -Filter "$obavezan-*.whl")) {
        throw "U wheelhouse nema $obavezan - instalacija na serveru bi pukla."
    }
}

# --------------------------------------------------------------------------- #

Korak "Arhiva -> $Izlaz"

$putanja = Join-Path $koren $Izlaz
New-Item -ItemType Directory -Force -Path (Split-Path $putanja) | Out-Null
if (Test-Path $putanja) {
    # Prethodni paket se ne brise nego pomera - njime se vracas ako novi ne valja.
    $stari = [IO.Path]::ChangeExtension($putanja, $null) +
             (Get-Item $putanja).LastWriteTime.ToString('yyyyMMdd-HHmmss') + '.zip'
    Move-Item $putanja $stari -Force
    Write-Host "   prethodni: $(Split-Path $stari -Leaf)"
}
Compress-Archive -Path (Join-Path $privremeno '*') -DestinationPath $putanja -Force
Remove-Item $privremeno -Recurse -Force

$mb = [math]::Round((Get-Item $putanja).Length / 1MB, 1)
Write-Host "`nGotovo: $putanja ($mb MB)" -ForegroundColor Green
Write-Host @"

Dalje, na serveru (RDP, kao administrator):
  1. prekopiraj paket u C:\objave\sefsync-paket.zip
  2. raspakuj ga i pokreni postavi.ps1 IZ PAKETA (C:\objave\postavi.ps1 je stari, od 22.09.):
     Remove-Item C:\objave\novo -Recurse -Force -ErrorAction SilentlyContinue
     Expand-Archive C:\objave\sefsync-paket.zip C:\objave\novo
     powershell -ExecutionPolicy Bypass -File C:\objave\novo\postavi.ps1 -Arhiva C:\objave\sefsync-paket.zip -Stvarno
     Na kraju mora da pise "kod u src\ i u .venv isti je kao u paketu" i "Gotovo".
  3. C:\efakture\.venv\Scripts\sefsync.exe pravila-uvoz C:\efakture\pravila.json
  4. C:\efakture\.venv\Scripts\sefsync.exe osvezi-ubl
  5. C:\efakture\.venv\Scripts\sefsync.exe razvrstaj

Korak 5 je namerno bez -sve: razvrstava samo ono sto jos nema poslovnu jedinicu.
Sa -sve bi promenio PJ i dokumentima koje je operater vec prosledio.
"@
