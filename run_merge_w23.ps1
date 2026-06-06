# ===========================================================================
# run_merge_w23.ps1
# Run ONE-TIME per W23: riscrapa tutti i geohash accumulando i dati di ieri.
#
# Differenze rispetto a run_scrape.ps1:
#   - NON cancella i CSV esistenti (raw, products, deduped)
#   - Cancella SOLO il checkpoint (sample_status) → riscrapa tutto dall'inizio
#   - skip-city-after-same-results = 0 (nessuno skip anticipato)
#   - NON avvia run_friday.ps1 al termine (lo fa l'utente manualmente)
# ===========================================================================

$ErrorActionPreference = "Stop"
$proj   = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena"
$python = "$proj\.venv\Scripts\python.exe"
$script = "$proj\deliveroo_promo_parity.py"
$output = "$proj\output"

Write-Host "[merge-w23] Cancello solo il checkpoint (sample_status)..."
$statusFile = "$output\deliveroo_sample_status.csv"
if (Test-Path $statusFile) {
    Remove-Item $statusFile -Force
    Write-Host "[merge-w23] Rimosso: deliveroo_sample_status.csv"
} else {
    Write-Host "[merge-w23] Nessun checkpoint trovato, si parte da zero."
}

Write-Host "[merge-w23] Dati esistenti preservati:"
foreach ($f in @("deliveroo_promo_raw.csv","deliveroo_promo_products.csv","deliveroo_promo_deduped.csv")) {
    $p = "$output\$f"
    if (Test-Path $p) {
        $size = [math]::Round((Get-Item $p).Length / 1KB, 0)
        Write-Host "  OK  $f ($size KB)"
    } else {
        Write-Host "  --  $f (non trovato)"
    }
}

Write-Host ""
Write-Host "[merge-w23] Avvio scraping completo W23 (merge mode)..."
Write-Host "[merge-w23] skip-city-after-same-results = 0 (nessuno skip)"
Write-Host ""

$maxRetries = 20
$attempt    = 0
$scrapeExit = 1

while ($scrapeExit -ne 0 -and $scrapeExit -ne 2 -and $attempt -lt $maxRetries) {
    if ($attempt -gt 0) {
        Write-Host "[merge-w23] Auto-restart #$attempt - attendo 30 secondi..."
        Start-Sleep -Seconds 30
        Get-Process chrome, chromedriver -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    $attempt++

    Push-Location $proj
    & $python $script `
        --polygons     "$proj\Polygons.csv" `
        --sample-step-km           4.5 `
        --max-points-per-city      0 `
        --skip-city-after-same-results 0 `
        --load-more-clicks         1
    $scrapeExit = $LASTEXITCODE
    Pop-Location
}

if ($scrapeExit -eq 0) {
    Write-Host ""
    Write-Host "[merge-w23] Scraping completato dopo $attempt tentativo/i."
    Write-Host "[merge-w23] Ora lancia manualmente run_friday.ps1 per la pipeline parity."
} elseif ($scrapeExit -eq 2) {
    Write-Host "[merge-w23] Interrotto manualmente."
} else {
    Write-Host "[merge-w23] ERRORE dopo $maxRetries tentativi (exit $scrapeExit)."
}
