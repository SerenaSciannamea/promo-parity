# monitor_scraper.ps1 — monitor AFFIDABILE del run (legge i FILE, non la pipe bufferizzata).
# Mostra ogni 5s: store scritti, geohash coperti, da quanto e' fermo l'output, stato processo.
$o = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\output_http_promo"
$prevRaw = -1; $stuckTicks = 0
while ($true) {
    $raw = if (Test-Path "$o\deliveroo_promo_raw.csv") { (Get-Content "$o\deliveroo_promo_raw.csv" | Measure-Object -Line).Lines } else { 0 }
    $gh  = if (Test-Path "$o\deliveroo_sample_status.csv") { (Get-Content "$o\deliveroo_sample_status.csv" | Measure-Object -Line).Lines } else { 0 }
    $mt  = if (Test-Path "$o\deliveroo_promo_raw.csv") { (Get-Item "$o\deliveroo_promo_raw.csv").LastWriteTime } else { Get-Date }
    $idle = [int]((Get-Date) - $mt).TotalSeconds
    $py   = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue).Count
    if ($raw -eq $prevRaw) { $stuckTicks++ } else { $stuckTicks = 0 }
    $prevRaw = $raw
    $state = if ($py -eq 0) { "NESSUN PYTHON" } elseif ($idle -gt 180) { "POSSIBILE STALLO ($idle s)" } else { "OK" }
    $color = if ($state -eq "OK") { "Green" } elseif ($state -like "OK*") { "Yellow" } else { "Red" }
    Write-Host ("[{0}] store={1,-6} geohash={2,-5} ultimo aggiornamento={3}s fa  python={4}  -> {5}" -f (Get-Date -Format 'HH:mm:ss'), $raw, $gh, $idle, $py, $state) -ForegroundColor $color
    Start-Sleep -Seconds 5
}
