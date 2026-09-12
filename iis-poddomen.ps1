# Pravi IIS sajt koji prosledjuje na panel: efakture.technolandkartica.net -> localhost:8088
#
# ZASTO
#   Panel slusa na 8088, sto znaci da svako mora da pamti port, da taj port bude
#   otvoren ka svakom racunaru, i da nema HTTPS-a. IIS na ovom serveru vec drzi
#   80 i 443 (zato 8080 i nije bio slobodan), pa neka on prima saobracaj i
#   prosledjuje ga panelu - isto kao za logistiku.
#
#   Panel tada ne mora da bude dostupan spolja: dovoljno je da IIS moze do njega
#   na localhost.
#
# STA TREBA PRE
#   1. DNS zapis: efakture.technolandkartica.net -> IP ovog servera
#   2. URL Rewrite + ARR moduli za IIS (skripta proveri i kaze odakle se skidaju)
#
# KAKO SE KORISTI
#   powershell -ExecutionPolicy Bypass -File .\iis-poddomen.ps1
#   powershell -ExecutionPolicy Bypass -File .\iis-poddomen.ps1 -Stvarno
#   powershell -ExecutionPolicy Bypass -File .\iis-poddomen.ps1 -Stvarno -Sertifikat

[CmdletBinding()]
param(
    [string]$Domen     = 'efakture.technolandkartica.net',
    [int]$PortPanela   = 8088,
    [string]$Sajt      = 'efakture',
    [string]$Putanja   = 'C:\inetpub\efakture-proxy',
    [switch]$Sertifikat,
    [switch]$Stvarno
)

$ErrorActionPreference = 'Stop'

function Korak($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Uradi($opis, [scriptblock]$posao) {
    if ($Stvarno) { Write-Host "   $opis"; & $posao }
    else { Write-Host "   [suvo] $opis" -ForegroundColor DarkGray }
}

Korak 'Provere'

$jeAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $jeAdmin) { throw 'Pokreni PowerShell kao administrator.' }

Import-Module WebAdministration -ErrorAction Stop
Write-Host '   IIS: da'

$rewrite = Get-WebGlobalModule -Name 'RewriteModule' -ErrorAction SilentlyContinue
if (-not $rewrite) {
    throw @'
Nema URL Rewrite modula. Skini i instaliraj oba, pa pokreni ponovo:
  URL Rewrite:  https://www.iis.net/downloads/microsoft/url-rewrite
  ARR:          https://www.iis.net/downloads/microsoft/application-request-routing
'@
}
Write-Host '   URL Rewrite: da'

$arr = Get-WebConfiguration -pspath 'MACHINE/WEBROOT/APPHOST' -filter 'system.webServer/proxy' `
    -ErrorAction SilentlyContinue
if (-not $arr) {
    throw @'
Nema ARR-a (Application Request Routing). Bez njega IIS ne ume da prosledjuje.
  https://www.iis.net/downloads/microsoft/application-request-routing
'@
}
Write-Host "   ARR: da (prosledjivanje trenutno: $($arr.enabled))"

# Panel mora da radi, inace se prosledjuje u prazno
try {
    $zdravlje = Invoke-RestMethod "http://localhost:$PortPanela/health" -TimeoutSec 5
    Write-Host "   panel na $PortPanela`: $($zdravlje.status)"
} catch {
    Write-Warning "Panel se ne javlja na http://localhost:$PortPanela - proveri servis pre nego sto pustis saobracaj."
}

$dns = Resolve-DnsName $Domen -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress }
if ($dns) { Write-Host "   DNS $Domen -> $($dns.IPAddress -join ', ')" }
else { Write-Warning "DNS zapis za $Domen ne postoji. Napravi ga (A zapis na IP ovog servera)." }

Korak 'Prosledjivanje na nivou servera'
Uradi 'ukljucujem ARR proxy' {
    Set-WebConfigurationProperty -pspath 'MACHINE/WEBROOT/APPHOST' `
        -filter 'system.webServer/proxy' -name 'enabled' -value 'True'
}

Korak "Sajt $Sajt ($Domen)"
Uradi "pravim folder $Putanja" { New-Item -ItemType Directory -Force -Path $Putanja | Out-Null }

# Sajt nema svoj kod - samo pravilo koje sve prosledjuje panelu.
$webConfig = @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="panel" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://localhost:$PortPanela/{R:1}" />
        </rule>
      </rules>
    </rewrite>
    <!-- Prilozi (UBL, PDF) i preuzimanja ne smeju da udare u podrazumevano ogranicenje. -->
    <security>
      <requestFiltering>
        <requestLimits maxAllowedContentLength="52428800" />
      </requestFiltering>
    </security>
  </system.webServer>
</configuration>
"@
Uradi 'upisujem web.config sa pravilom prosledjivanja' {
    Set-Content -Path (Join-Path $Putanja 'web.config') -Value $webConfig -Encoding utf8
}

Uradi "registrujem sajt na portu 80 za $Domen" {
    if (-not (Get-Website -Name $Sajt -ErrorAction SilentlyContinue)) {
        New-Website -Name $Sajt -PhysicalPath $Putanja -Port 80 -HostHeader $Domen | Out-Null
    }
    Set-ItemProperty "IIS:\Sites\$Sajt" -Name applicationPool -Value 'DefaultAppPool'
    # Sajt je samo posrednik, nema .NET koda
    Set-ItemProperty 'IIS:\AppPools\DefaultAppPool' -Name managedRuntimeVersion -Value ''
}

if ($Sertifikat) {
    Korak 'HTTPS'
    $cert = Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.Subject -match 'technolandkartica' -or $_.DnsNameList -match 'technolandkartica' } |
        Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $cert) {
        Write-Warning 'Nema sertifikata za technolandkartica u Cert:\LocalMachine\My. Preskacem HTTPS.'
    } else {
        Write-Host "   sertifikat: $($cert.Subject), vazi do $($cert.NotAfter.ToString('dd.MM.yyyy'))"
        Uradi 'vezujem 443' {
            if (-not (Get-WebBinding -Name $Sajt -Protocol https -ErrorAction SilentlyContinue)) {
                New-WebBinding -Name $Sajt -Protocol https -Port 443 -HostHeader $Domen -SslFlags 1
            }
            $vezivanje = Get-WebBinding -Name $Sajt -Protocol https
            $vezivanje.AddSslCertificate($cert.Thumbprint, 'My')
        }
    }
}

Korak 'Zavrsna provera'
Uradi 'probam kroz IIS' {
    Start-Sleep 3
    try {
        $odgovor = Invoke-RestMethod "http://$Domen/health" -TimeoutSec 10
        if ($odgovor.status -eq 'ok') { Write-Host "   $Domen odgovara: ok" -ForegroundColor Green }
    } catch {
        Write-Warning "Preko $Domen se jos ne javlja: $($_.Exception.Message)"
        Write-Host '   ako je DNS tek napravljen, sacekaj da se razmnozi pa probaj ponovo'
    }
}

Write-Host ''
if ($Stvarno) {
    Write-Host "Gotovo. Panel: http://$Domen" -ForegroundColor Green
    Write-Host "U C:\efakture\.env postavi:  PUBLIC_BASE_URL=http://$Domen"
    Write-Host 'pa restartuj servis, da linkovi u mejlovima vode na tu adresu.'
    Write-Host ''
    Write-Host 'Port 8088 vise ne mora da bude otvoren ka mrezi:'
    Write-Host '  Remove-NetFirewallRule -DisplayName "ELBRACO e-fakture 8088*"'
} else {
    Write-Host 'Suvi prolaz - nista nije promenjeno. Za stvarno dodaj -Stvarno' -ForegroundColor Yellow
}
