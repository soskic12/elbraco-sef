# Sta zauzima mesto u C:\efakture. Samo cita, nista ne brise.
#
# Pokreni na serveru:
#   powershell -ExecutionPolicy Bypass -File C:\objave\pregled-diska.ps1

param([string]$Koren = "C:\efakture")

function Velicina($putanja) {
    if (-not (Test-Path $putanja)) { return $null }
    $s = Get-ChildItem $putanja -Recurse -File -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    return [pscustomobject]@{ Fajlova = $s.Count; MB = [math]::Round($s.Sum / 1MB, 1) }
}

function Ispisi($naziv, $putanja) {
    $v = Velicina $putanja
    if ($null -eq $v) { Write-Host ("   {0,-28} nema" -f $naziv); return }
    Write-Host ("   {0,-28} {1,10:N1} MB   {2,7} fajlova" -f $naziv, $v.MB, $v.Fajlova)
}

Write-Host "== Disk"
Get-PSDrive C | Select-Object @{n='Zauzeto GB';e={[math]::Round($_.Used/1GB,1)}},
                              @{n='Slobodno GB';e={[math]::Round($_.Free/1GB,1)}} |
    Format-Table -AutoSize | Out-String | Write-Host

Write-Host "== Prvi nivo u $Koren"
Get-ChildItem $Koren -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    Ispisi $_.Name $_.FullName
}
Write-Host ""

$data = Join-Path $Koren "data"
if (Test-Path $data) {
    Write-Host "== Sta aplikacija cuva ($data)"
    foreach ($p in @("ubl", "pdf", "slike")) { Ispisi $p (Join-Path $data $p) }
    $logovi = Get-ChildItem $data -File -Filter *.log* -ErrorAction SilentlyContinue |
              Measure-Object -Property Length -Sum
    Write-Host ("   {0,-28} {1,10:N1} MB   {2,7} fajlova" -f "logovi (*.log*)",
                [math]::Round($logovi.Sum / 1MB, 1), $logovi.Count)
    Write-Host ""

    $slike = Join-Path $data "slike"
    if (Test-Path $slike) {
        Write-Host "== Slike po mesecima"
        Get-ChildItem $slike -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $godina = $_
            Get-ChildItem $godina.FullName -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Ispisi ("$($godina.Name)-$($_.Name)") $_.FullName }
        }
        Write-Host ""
        Write-Host "== Deset najvecih slika"
        Get-ChildItem $slike -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object Length -Descending | Select-Object -First 10 |
            ForEach-Object {
                Write-Host ("   {0,8:N1} MB  {1:dd.MM.yyyy}  {2}" -f ($_.Length/1MB),
                            $_.LastWriteTime, $_.Name)
            }
        $sve = Get-ChildItem $slike -Recurse -File -ErrorAction SilentlyContinue |
               Measure-Object -Property Length -Average
        if ($sve.Count -gt 0) {
            Write-Host ("`n   prosecna slika: {0:N1} MB na {1} slika" -f
                        ($sve.Average/1MB), $sve.Count)
        }
    }
}

Write-Host ""
Write-Host "== Najvece stavke bilo gde ispod $Koren (prvih 15)"
Get-ChildItem $Koren -Recurse -File -ErrorAction SilentlyContinue |
    Sort-Object Length -Descending | Select-Object -First 15 |
    ForEach-Object {
        Write-Host ("   {0,8:N1} MB  {1}" -f ($_.Length/1MB),
                    $_.FullName.Replace($Koren, ""))
    }
