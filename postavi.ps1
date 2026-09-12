# Postavlja panel za e-fakture na server: bekap, raspakivanje, servis, provera.
#
# ZASTO SKRIPTA
#   Postavljanje ima sest koraka i svaki se lako preskoci: zaustaviti servis pre
#   kopiranja (inace fajlovi ostanu zakljucani i posao stane na pola), sacuvati
#   .env (u njemu su lozinke i on ostaje na serveru izmedju verzija), sacuvati
#   bazu (u njoj je sav rad operatera), napraviti okruzenje, otvoriti port i tek
#   onda upaliti. Zato ide odjednom.
#
#   Podrazumevano samo ISPISE sta bi uradila. Stvarno menja tek sa -Stvarno.
#
# GDE SE POKRECE
#   NA SERVERU, kao administrator. Paket (sefsync-paket.zip) se prethodno
#   prekopira na server bilo kako - RDP-om, deljenim folderom, kako vec ide.
#
# KAKO SE KORISTI
#   # sta bi se desilo, nista se ne menja:
#   powershell -ExecutionPolicy Bypass -File C:\alati\postavi.ps1 -Arhiva C:\objave\sefsync-paket.zip
#
#   # stvarno postavljanje:
#   powershell -ExecutionPolicy Bypass -File C:\alati\postavi.ps1 -Arhiva C:\objave\sefsync-paket.zip -Stvarno
#
#   # nazad na prethodnu verziju:
#   powershell -ExecutionPolicy Bypass -File C:\alati\postavi.ps1 -Vrati -Stvarno
#
# STA NE DIRA
#   .env    - u njemu su lozinke i podesavanja ovog servera
#   data\   - baza panela, preuzeti UBL-ovi i logovi

[CmdletBinding()]
param(
    [string]$Arhiva,
    [string]$Odrediste = 'C:\efakture',
    [string]$Servis    = 'ElbracoEfakture',
    [int]$Port         = 8080,
    [switch]$Vrati,
    [switch]$Stvarno
)

$ErrorActionPreference = 'Stop'
# Aplikacija trazi .env i data\ u ovom folderu. Bez ovoga bi uzela radni folder
# onoga ko je pokrenuo skriptu, pa bi baza zavrsila pored zipa.
$env:SEFSYNC_HOME = $Odrediste
$bekapKoren = Join-Path $Odrediste 'bekap'
$cuvaj = @('.env', 'data')

function Korak($tekst) { Write-Host "`n== $tekst" -ForegroundColor Cyan }
function Uradi($opis, [scriptblock]$posao) {
    if ($Stvarno) { Write-Host "   $opis"; & $posao }
    else { Write-Host "   [suvo] $opis" -ForegroundColor DarkGray }
}

# --------------------------------------------------------------------------- #
# provere pre svega
# --------------------------------------------------------------------------- #

Korak 'Provere'

$jeAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $jeAdmin) { throw 'Pokreni PowerShell kao administrator (servis i firewall to traze).' }
Write-Host '   administrator: da'

# PowerShell 5.1 na serveru nema ?? operator, pa ide izricito
$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) {
    throw @'
Python nije instaliran. Instaliraj Python 3.12 ili noviji, pa pokreni ponovo:
  winget install --id Python.Python.3.12 --scope machine
ili sa https://www.python.org/downloads/windows/ (obavezno "Add python.exe to PATH").
'@
}
$verzija = & $python.Source -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "   python: $verzija ($($python.Source))"
if ([version]$verzija -lt [version]'3.11') { throw "Potreban je Python 3.11+, nadjen $verzija." }
if ($verzija -notin @('3.12', '3.13')) {
    Write-Warning "Gotovi paketi su gradjeni za 3.12 i 3.13, server ima $verzija - instalacija ce pokusati preko interneta."
}

$odbc = Get-OdbcDriver -Name '*SQL Server*' -ErrorAction SilentlyContinue
if (-not ($odbc | Where-Object Name -match 'ODBC Driver (17|18)')) {
    Write-Warning @'
Nema "ODBC Driver 18 for SQL Server". Bez njega nema ni prijave ni sifarnika.
Instalirati sa https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server
'@
} else {
    Write-Host '   ODBC drajver: da'
}

# Port ume da bude zauzet (IIS) ili u rezervisanom opsegu (Hyper-V, WinNAT).
# Bolje da se sazna ovde nego da servis tiho ne moze da se veze.
$slusac = $null
try {
    $slusac = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $Port)
    $slusac.Start()
    Write-Host "   port ${Port}: slobodan"
} catch {
    Write-Warning @"
Port $Port se ne moze zauzeti: $($_.Exception.Message)
Ko ga drzi:            netstat -ano | findstr :$Port
Rezervisani opsezi:    netsh interface ipv4 show excludedportrange protocol=tcp
Pa pokreni sa drugim portom, npr:   -Port 8088
"@
} finally {
    if ($slusac) { $slusac.Stop() }
}

$nssm = Join-Path $Odrediste 'alati\nssm.exe'
$nssmUPaketu = $false

# --------------------------------------------------------------------------- #
# vracanje prethodne verzije
# --------------------------------------------------------------------------- #

if ($Vrati) {
    Korak 'Vracanje prethodne verzije'
    $poslednji = Get-ChildItem $bekapKoren -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $poslednji) { throw "Nema nijednog bekapa u $bekapKoren." }
    Write-Host "   bekap: $($poslednji.FullName)"

    Uradi 'zaustavljam servis' {
        if (Get-Service $Servis -ErrorAction SilentlyContinue) { & $nssm stop $Servis 2>$null | Out-Null }
        else { Stop-ScheduledTask -TaskName $Servis -ErrorAction SilentlyContinue }
        Get-Process sefsync -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep 2
    }
    Uradi 'vracam fajlove' {
        Get-ChildItem $Odrediste -Exclude 'bekap', 'data', '.env', 'alati' |
            Remove-Item -Recurse -Force
        Copy-Item (Join-Path $poslednji.FullName '*') $Odrediste -Recurse -Force
    }
    Uradi 'palim servis' {
        if (Get-Service $Servis -ErrorAction SilentlyContinue) { & $nssm start $Servis | Out-Null }
        else { Start-ScheduledTask -TaskName $Servis }
    }
    Write-Host "`nGotovo." -ForegroundColor Green
    return
}

# --------------------------------------------------------------------------- #
# postavljanje
# --------------------------------------------------------------------------- #

if (-not $Arhiva) { throw 'Nedostaje -Arhiva (putanja do sefsync-paket.zip).' }
if (-not (Test-Path $Arhiva)) { throw "Nema arhive: $Arhiva" }

Korak "Paket: $Arhiva"
$privremeno = Join-Path $env:TEMP ("sefsync-" + (Get-Date -Format 'yyyyMMddHHmmss'))
Uradi "raspakujem u $privremeno" { Expand-Archive -Path $Arhiva -DestinationPath $privremeno -Force }
if ($Stvarno -and (Test-Path (Join-Path $privremeno 'alati\nssm.exe'))) { $nssmUPaketu = $true }

$postoji = Test-Path (Join-Path $Odrediste 'src')
Korak ("Odrediste: $Odrediste " + $(if ($postoji) { '(nadogradnja)' } else { '(prva instalacija)' }))

if ($postoji) {
    $oznaka = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bekap = Join-Path $bekapKoren $oznaka
    Uradi "bekap postojece verzije u $bekap" {
        New-Item -ItemType Directory -Force -Path $bekap | Out-Null
        Get-ChildItem $Odrediste -Exclude 'bekap' | Copy-Item -Destination $bekap -Recurse -Force
        Get-ChildItem $bekapKoren -Directory | Sort-Object Name -Descending |
            Select-Object -Skip 5 | Remove-Item -Recurse -Force   # cuvamo poslednjih pet
    }
    Uradi 'zaustavljam servis' {
        if (Get-Service $Servis -ErrorAction SilentlyContinue) {
            & $nssm stop $Servis 2>$null | Out-Null
        } elseif (Get-ScheduledTask -TaskName $Servis -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $Servis -ErrorAction SilentlyContinue
            Get-Process sefsync -ErrorAction SilentlyContinue | Stop-Process -Force
        }
        Start-Sleep 2
    }
}

Korak 'Kopiranje'
Uradi 'kopiram novu verziju (.env i data ostaju netaknuti)' {
    New-Item -ItemType Directory -Force -Path $Odrediste | Out-Null
    # data\ mora da postoji pre pokretanja: u njega ide preusmeren ispis servisa,
    # a preusmeravanje pada ako foldera nema (i zadatak tiho ne uradi nista).
    New-Item -ItemType Directory -Force -Path (Join-Path $Odrediste 'data') | Out-Null
    Get-ChildItem $privremeno | Where-Object { $cuvaj -notcontains $_.Name } |
        Copy-Item -Destination $Odrediste -Recurse -Force
}

Korak 'Python okruzenje'
$venv = Join-Path $Odrediste '.venv'
# Ide se preko "python -m pip", ne preko pip.exe: pip ne moze da zameni
# sopstveni exe dok radi, pa nadogradnja pukne sa "To modify pip...".
$pyVenv = Join-Path $venv 'Scripts\python.exe'
$sefsync = Join-Path $venv 'Scripts\sefsync.exe'
Uradi 'pravim .venv' {
    if (-not (Test-Path $venv)) { & $python.Source -m venv $venv }
}
Uradi 'instaliram zavisnosti (iz paketa, bez interneta)' {
    $tocak = Join-Path $Odrediste 'wheelhouse'
    & $pyVenv -m pip install --quiet --no-index --find-links $tocak --upgrade pip setuptools wheel
    & $pyVenv -m pip install --quiet --no-index --find-links $tocak "$Odrediste[mssql]"
    if ($LASTEXITCODE -ne 0) {
        # Tockovi su gradjeni za Python 3.12. Ako server ima drugu verziju,
        # ne odgovaraju mu, pa se pokusava preko interneta.
        Write-Warning 'Paket ne odgovara ovoj verziji Pythona - pokusavam sa interneta.'
        & $pyVenv -m pip install --quiet "$Odrediste[mssql]"
        if ($LASTEXITCODE -ne 0) {
            throw 'Instalacija zavisnosti nije prosla ni offline ni online.'
        }
    }
}

Korak 'Podesavanja'
$env_ = Join-Path $Odrediste '.env'
if (Test-Path $env_) {
    Write-Host '   .env vec postoji - ne diram ga'
    # Port mora da bude isti na tri mesta (app, firewall, adresa u mejlovima).
    # Ako se razlikuje, servis slusa jedno a sve ostalo gadja drugo - pa se
    # ovde uskladjuje, uz jasan ispis sta je promenjeno.
    $uEnv = (Get-Content $env_ | Where-Object { $_ -match '^WEB_PORT=' }) -replace 'WEB_PORT=', ''
    if ($uEnv -and $uEnv.Trim() -ne "$Port") {
        Write-Host "   u .env je bio WEB_PORT=$($uEnv.Trim()), menjam na $Port" -ForegroundColor Yellow
        Uradi 'uskladjujem port u .env' {
            (Get-Content $env_) -replace '^WEB_PORT=.*$', "WEB_PORT=$Port" |
                Set-Content $env_ -Encoding utf8
            (Get-Content $env_) -replace '^PUBLIC_BASE_URL=.*$', "PUBLIC_BASE_URL=http://$env:COMPUTERNAME`:$Port" |
                Set-Content $env_ -Encoding utf8
        }
    }
} else {
    Uradi 'pravim .env iz sablona' {
        Copy-Item (Join-Path $Odrediste '.env.server') $env_
        # Kljuc za kolacice mora biti razlicit na svakoj instalaciji.
        $nasumicno = -join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
        (Get-Content $env_) -replace '^WEB_SECRET=$', "WEB_SECRET=$nasumicno" |
            Set-Content $env_ -Encoding utf8
        (Get-Content $env_) -replace '^PUBLIC_BASE_URL=.*$', "PUBLIC_BASE_URL=http://$env:COMPUTERNAME`:$Port" |
            Set-Content $env_ -Encoding utf8
        (Get-Content $env_) -replace '^WEB_PORT=.*$', "WEB_PORT=$Port" | Set-Content $env_ -Encoding utf8
    }
    Write-Host '   !! Otvori .env i popuni SEF_API_KEY i SMTP_PASSWORD pre pokretanja' -ForegroundColor Yellow
}

Korak 'Baza'
Uradi 'kreiram/dopunjavam tabele' { & $sefsync init-db }

Korak "Servis $Servis"
$imaNssm = (Test-Path $nssm) -or $nssmUPaketu
if (-not $imaNssm) {
    # Bez NSSM-a ide zakazani zadatak: ugradjen u Windows, pokrece se sa
    # sistemom i sam se dize ako proces padne. Za nas posao radi isto.
    Write-Host '   nema nssm.exe - koristim zakazani zadatak (ugradjen u Windows)'
    Uradi 'registrujem zakazani zadatak' {
        # Kroz cmd, da bi ispis (i eventualni pad pri pokretanju) zavrsio u logu.
        # Bez toga zadatak tiho ne radi i nema gde da se vidi zasto.
        $log = Join-Path $Odrediste 'data\servis.log'
        $komanda = "/c `"set SEFSYNC_HOME=$Odrediste&& `"$sefsync`" serve --worker >> `"$log`" 2>&1`""
        $radnja = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $komanda `
            -WorkingDirectory $Odrediste
        $okidac = New-ScheduledTaskTrigger -AtStartup
        $podesavanja = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
        Register-ScheduledTask -TaskName $Servis -Action $radnja -Trigger $okidac `
            -Settings $podesavanja -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
    }
} else {
    Uradi 'registrujem/azuriram servis' {
        if (-not (Get-Service $Servis -ErrorAction SilentlyContinue)) {
            & $nssm install $Servis $sefsync serve --worker
        }
        & $nssm set $Servis AppDirectory $Odrediste
        & $nssm set $Servis DisplayName 'ELBRACO e-fakture (SEF)'
        & $nssm set $Servis Description 'Panel za ulazne e-fakture sa SEF-a i periodicno preuzimanje.'
        & $nssm set $Servis Start SERVICE_AUTO_START
        & $nssm set $Servis AppStdout (Join-Path $Odrediste 'data\servis.log')
        & $nssm set $Servis AppStderr (Join-Path $Odrediste 'data\servis.log')
        & $nssm set $Servis AppRotateFiles 1
        & $nssm set $Servis AppRotateBytes 10485760
        & $nssm set $Servis AppEnvironmentExtra "SEFSYNC_HOME=$Odrediste"
    }
}

Korak "Firewall (port $Port)"
$pravilo = "ELBRACO e-fakture $Port"
Uradi 'otvaram port u lokalnoj mrezi' {
    if (-not (Get-NetFirewallRule -DisplayName $pravilo -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $pravilo -Direction Inbound -Protocol TCP `
            -LocalPort $Port -Action Allow -Profile Domain,Private | Out-Null
    }
}

Korak 'Pokretanje'
Uradi 'palim servis' {
    if ($imaNssm) { & $nssm start $Servis | Out-Null }
    else { Start-ScheduledTask -TaskName $Servis }
    Start-Sleep 6
}
Uradi 'provera da odgovara' {
    # Prvo dizanje ume da potraje - uvoze se biblioteke i prave tabele.
    $javio = $false
    foreach ($pokusaj in 1..12) {
        try {
            $odgovor = Invoke-RestMethod "http://localhost:$Port/health" -TimeoutSec 5
            if ($odgovor.status -eq 'ok') { $javio = $true; break }
        } catch { Start-Sleep 5 }
    }
    if ($javio) {
        Write-Host '   panel odgovara: ok' -ForegroundColor Green
    } else {
        Write-Warning "Panel se nije javio na portu $Port ni posle minut."
        $log = Join-Path $Odrediste 'data\servis.log'
        if (Test-Path $log) {
            Write-Host "`n--- kraj $log ---" -ForegroundColor Yellow
            Get-Content $log -Tail 25
        } else {
            Write-Host "   nema ni loga ($log) - zadatak verovatno nije ni krenuo" -ForegroundColor Yellow
            Write-Host '   probaj rucno:' -ForegroundColor Yellow
            Write-Host "     cd $Odrediste; .\.venv\Scripts\sefsync.exe serve"
        }
    }
}

Write-Host ''
if ($Stvarno) {
    Write-Host "Gotovo. Panel: http://$env:COMPUTERNAME`:$Port" -ForegroundColor Green
    Write-Host "Logovi: $Odrediste\data\servis.log"
} else {
    Write-Host 'Ovo je bio suvi prolaz - nista nije promenjeno.' -ForegroundColor Yellow
    Write-Host 'Za stvarno postavljanje dodaj -Stvarno'
}
