# Restart panela, radi i sa NSSM servisom i sa zakazanim zadatkom.
#
#   powershell -ExecutionPolicy Bypass -File C:\efakture\restart.ps1
#
# Zasto skripta: gasenje zakazanog zadatka obori cmd.exe koji ga je pokrenuo,
# ali sefsync.exe ume da ostane ziv - pa stari proces i dalje drzi port i staru
# bazu, a izgleda kao da izmena u .env nije uhvatila.

[CmdletBinding()]
param(
    [string]$Servis    = 'ElbracoEfakture',
    [string]$Odrediste = 'C:\efakture',
    [int]$Port         = 8088,
    # Samo zaustavi, bez paljenja. Treba kad baza mora da stoji - npr. pri
    # prebacivanju na MS SQL: ako panel radi, u staru bazu se upisuje ono sto
    # u novu nece preci.
    [switch]$Zaustavi
)

$ErrorActionPreference = 'Stop'
$nssm = Join-Path $Odrediste 'alati\nssm.exe'

if (Get-Service $Servis -ErrorAction SilentlyContinue) {
    Write-Host 'Zaustavljam servis...'
    & $nssm stop $Servis 2>$null | Out-Null
} elseif (Get-ScheduledTask -TaskName $Servis -ErrorAction SilentlyContinue) {
    Write-Host 'Zaustavljam zakazani zadatak...'
    Stop-ScheduledTask -TaskName $Servis -ErrorAction SilentlyContinue
} else {
    throw "Nema ni servisa ni zadatka pod imenom $Servis."
}

# Zaostali proces se ne trazi po imenu nego po portu: servis se pokrece
# sistemskim Python-om, pa se zove "python", a ne "sefsync". Zbog toga je
# 25.09. ostao ziv, novi se nije mogao vezati i zadatak je pao sa kodom 1.
function Ugasi-Na-Portu($broj) {
    $veze = Get-NetTCPConnection -LocalPort $broj -State Listen -ErrorAction SilentlyContinue
    foreach ($veza in $veze) {
        $proces = Get-Process -Id $veza.OwningProcess -ErrorAction SilentlyContinue
        if (-not $proces) { continue }
        $linija = (Get-CimInstance Win32_Process -Filter "ProcessId = $($proces.Id)" `
                   -ErrorAction SilentlyContinue).CommandLine
        $nas = ($proces.Path -and $proces.Path.StartsWith($Odrediste, [StringComparison]::OrdinalIgnoreCase)) -or
               ($linija -and $linija -like "*$Odrediste*")
        if ($nas) {
            Write-Host "Gasim zaostali proces $($proces.ProcessName) (PID $($proces.Id))..."
            Stop-Process -Id $proces.Id -Force
        } else {
            Write-Warning "Port $broj drzi tudji proces: $($proces.ProcessName) (PID $($proces.Id)). Ne diram ga."
        }
    }
}

Get-Process sefsync -ErrorAction SilentlyContinue | Stop-Process -Force
foreach ($cekanje in 1..10) {
    Ugasi-Na-Portu $Port
    Start-Sleep 1
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
}

if ($Zaustavi) {
    Write-Host 'Zaustavljeno. Panel NE radi - upali ga sa restart.ps1 bez -Zaustavi.' -ForegroundColor Yellow
    exit 0
}

Write-Host 'Palim...'
if (Get-Service $Servis -ErrorAction SilentlyContinue) { & $nssm start $Servis | Out-Null }
else { Start-ScheduledTask -TaskName $Servis }

foreach ($pokusaj in 1..12) {
    Start-Sleep 3
    try {
        if ((Invoke-RestMethod "http://localhost:$Port/health" -TimeoutSec 5).status -eq 'ok') {
            Write-Host "Panel radi (port $Port)." -ForegroundColor Green
            exit 0
        }
    } catch { }
}

Write-Warning "Panel se nije javio na portu $Port ni posle 36 sekundi."
$log = Join-Path $Odrediste 'data\servis.log'
if (Test-Path $log) { Write-Host "`n--- kraj $log ---"; Get-Content $log -Tail 25 }
