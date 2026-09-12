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
    [int]$Port         = 8088
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

$zaostali = Get-Process sefsync -ErrorAction SilentlyContinue
if ($zaostali) {
    Write-Host "Gasim zaostale procese ($($zaostali.Count))..."
    $zaostali | Stop-Process -Force
}
Start-Sleep 2

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
