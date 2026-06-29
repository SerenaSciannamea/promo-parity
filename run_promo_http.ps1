# ===========================================================================
# run_promo_http.ps1  —  scraper UFFICIALE Promo Parity (HTTP/GraphQL)
# Sostituisce lo scraper Selenium come fonte dati. Il vecchio resta sul disco.
#
# 1) gira lo scraper HTTP su TUTTI i poligoni (Polygons.csv), griglia 3 km,
#    collection=offers, badge-filter (salta consegne-gratis), resume attivo;
# 2) se completa, copia gli output in output/ (con backup) e lancia la pipeline.
# Log live: data\promo_http_log.txt  (+ a video).
# ===========================================================================
$ErrorActionPreference = "Continue"
$proj      = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena"
$scraper   = "$proj\promo_http_scraper.py"
$outHttp   = "$proj\output_http_promo"
$python    = "$proj\.venv\Scripts\python.exe"
$out       = "$proj\output"
$log       = "$proj\data\promo_http_log.txt"

$env:PYTHONUNBUFFERED = "1"; $env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Force -Path "$proj\data" | Out-Null

function Log($m) {
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

Set-Content -Path $log -Value "=== run_promo_http $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding UTF8

# Run pulito: se l'output esistente NON e' di oggi, archivialo e riparti da zero.
# (Stesso giorno = resume dopo un eventuale blocco -> mantieni i progressi.)
$todayStr = Get-Date -Format "yyyy-MM-dd"
$ssPath   = "$outHttp\deliveroo_sample_status.csv"
$resume   = $false
if (Test-Path $ssPath) {
    if ((Get-Item $ssPath).LastWriteTime.ToString("yyyy-MM-dd") -eq $todayStr) { $resume = $true }
}
if (-not $resume -and (Test-Path $outHttp)) {
    $arch = "${outHttp}_archive\$(Get-Date -Format yyyyMMdd_HHmmss)"
    New-Item -ItemType Directory -Force -Path (Split-Path $arch) | Out-Null
    Move-Item $outHttp $arch -Force
    Log "Output precedente (non di oggi) archiviato in $arch -> run PULITO."
} elseif ($resume) {
    Log "Trovato output di oggi -> RESUME dai geohash mancanti."
}

# Auto-resume / watchdog: se lo scraper si interrompe (crash, blocco IP, rete, sospensione)
# viene rilanciato e riprende automaticamente dai geohash gia' coperti (sample_status).
$maxAttempts = 6
$scrapeExit  = 1
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    Log "Avvio scraper HTTP (tentativo $attempt/$maxAttempts, tutti i poligoni, 3 km, collection=offers)..."
    & $python $scraper --collection offers 2>&1 | ForEach-Object {
        $l = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
        Write-Host $l
        Add-Content -Path $log -Value $l -Encoding UTF8
    }
    $scrapeExit = $LASTEXITCODE
    if ($scrapeExit -eq 0) { Log "Scraper COMPLETATO al tentativo $attempt."; break }
    if ($attempt -lt $maxAttempts) {
        Log "Interruzione rilevata (exit $scrapeExit). Riprendo tra 180s dal punto di interruzione (resume)..."
        Start-Sleep -Seconds 180
    } else {
        Log "Ancora interrotto dopo $maxAttempts tentativi (exit $scrapeExit). Stop: il prossimo run schedulato riprendera' dal sample_status."
    }
}

if ($scrapeExit -eq 0) {
    Log "Scraper completato. Aggancio alla pipeline..."
    $files = @("deliveroo_promo_raw.csv","deliveroo_promo_deduped.csv","deliveroo_promo_products.csv","deliveroo_sample_status.csv")
    $bdir  = "$out\_backup_pre_http_$(Get-Date -Format yyyyMMdd_HHmmss)"
    New-Item -ItemType Directory -Force -Path $bdir | Out-Null
    foreach ($f in $files) { if (Test-Path "$out\$f")     { Copy-Item "$out\$f" "$bdir\$f" -Force } }
    foreach ($f in $files) { if (Test-Path "$outHttp\$f") { Copy-Item "$outHttp\$f" "$out\$f" -Force; Log "  copiato $f -> output/" } }
    Log "Backup output precedente in: $bdir"
    Log "Avvio pipeline (run_friday.ps1)..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$proj\run_friday.ps1"
    Log "Pipeline terminata (exit $LASTEXITCODE)."
    # Marker di completamento per il watchdog: run di oggi andato a buon fine.
    Set-Content -Path "$proj\data\promo_http_last_success.txt" -Value (Get-Date -Format 'yyyy-MM-dd') -Encoding UTF8
} else {
    Log "Scraping non completato dopo i tentativi (exit $scrapeExit). Pipeline NON avviata; riprende al prossimo run schedulato."
}
Log "FINE."
