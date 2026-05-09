param(
    [string]$Cities = "",
    [int]$MaxPointsPerCity = 0,
    [switch]$ShowBrowser,
    [string]$StoresCsv = "",
    [string]$GoogleSheet = "",
    [int]$GoogleWorksheetGid = 0,
    [string]$GoogleServiceAccountJson = "",
    [int]$SkipCityAfterSameResults = 4
)

$python    = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$script    = Join-Path $PSScriptRoot "deliveroo_promo_parity.py"
$polygons  = Join-Path $PSScriptRoot "Polygons.csv"
$outputDir = Join-Path $PSScriptRoot "output"
$scrapeLog = Join-Path $PSScriptRoot "data\scraper_log.txt"

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "data") | Out-Null

# App Password Gmail — stessa di run_friday.ps1
$emailAppPassword = ""   # <-- INCOLLA QUI LA APP PASSWORD

# ---------------------------------------------------------------------------
# Funzione di log con timestamp
# ---------------------------------------------------------------------------
function Write-Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $scrapeLog -Value $line -Encoding UTF8
}

function Send-Notify {
    param([string]$Subject, [string]$Body, [switch]$IsError)
    if (-not $emailAppPassword) { return }
    $notifyArgs = @(
        "-m", "pipeline.notifier",
        "--subject",      $Subject,
        "--body",         $Body,
        "--app-password", $emailAppPassword,
        "--log",          $scrapeLog
    )
    if ($IsError) { $notifyArgs += "--error" }
    Push-Location $PSScriptRoot
    & $python @notifyArgs 2>$null
    Pop-Location
}

# ---------------------------------------------------------------------------
# Archivia automaticamente l'output della settimana precedente
#
# Logica: se il file deliveroo_promo_raw.csv esiste ed e' stato scritto
# in una settimana ISO diversa da quella corrente, spostiamo tutti i file
# di output in output/archive/YYYY-Www/ e ripartiamo da zero.
# ---------------------------------------------------------------------------
$rawCsv = Join-Path $outputDir "deliveroo_promo_raw.csv"

if (Test-Path $rawCsv) {
    # Settimana corrente in formato ISO (es. "2026-W20")
    $now         = Get-Date
    $currentWeek = "{0}-W{1:D2}" -f (Get-Date -UFormat "%G"), [int](Get-Date -UFormat "%V")

    # Settimana in cui e' stato scritto il file
    $fileDate = (Get-Item $rawCsv).LastWriteTime
    $fileWeek = "{0}-W{1:D2}" -f ($fileDate.ToString("yyyy")), [int]($fileDate | Get-Date -UFormat "%V")

    if ($fileWeek -ne $currentWeek) {
        Write-Log "Trovati file della settimana $fileWeek (settimana corrente: $currentWeek)."
        Write-Log "Archivio i file vecchi e riparto da zero per la nuova settimana..."

        $archiveDir = Join-Path $outputDir "archive\$fileWeek"
        New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null

        $filesToArchive = @(
            "deliveroo_promo_raw.csv",
            "deliveroo_promo_deduped.csv",
            "deliveroo_sample_status.csv",
            "deliveroo_promo_products.csv",
            "stores_with_deliveroo_names.csv"
        )

        foreach ($fname in $filesToArchive) {
            $src = Join-Path $outputDir $fname
            if (Test-Path $src) {
                Move-Item -Path $src -Destination (Join-Path $archiveDir $fname) -Force
                Write-Log "  Archiviato: $fname -> archive/$fileWeek/$fname"
            }
        }

        Write-Log "Archiviazione completata. Lo scraper parte da zero per $currentWeek."
    } else {
        Write-Log "File output gia' della settimana corrente ($currentWeek): riprendo da dove mi ero fermato."
    }
} else {
    Write-Log "Nessun file output precedente trovato. Partenza da zero."
}

# ---------------------------------------------------------------------------
# Costruisci la lista argomenti per lo scraper
# ---------------------------------------------------------------------------
$paramList = New-Object System.Collections.Generic.List[string]
$null = $paramList.Add($script)
$null = $paramList.Add("--polygons")
$null = $paramList.Add($polygons)
$null = $paramList.Add("--sample-step-km")
$null = $paramList.Add("2.5")
$null = $paramList.Add("--max-points-per-city")
$null = $paramList.Add("$MaxPointsPerCity")
$null = $paramList.Add("--skip-city-after-same-results")
$null = $paramList.Add("$SkipCityAfterSameResults")

if ($Cities -ne "") {
    $null = $paramList.Add("--city-codes")
    $null = $paramList.Add($Cities)
}

if ($ShowBrowser) {
    $null = $paramList.Add("--show")
}

if ($StoresCsv -ne "") {
    $null = $paramList.Add("--stores-csv")
    $null = $paramList.Add($StoresCsv)
    $null = $paramList.Add("--stores-column-index")
    $null = $paramList.Add("1")
}

if ($GoogleSheet -ne "") {
    $null = $paramList.Add("--google-sheet")
    $null = $paramList.Add($GoogleSheet)
}

if ($GoogleWorksheetGid -ne 0) {
    $null = $paramList.Add("--google-worksheet-gid")
    $null = $paramList.Add("$GoogleWorksheetGid")
}

if ($GoogleServiceAccountJson -ne "") {
    $null = $paramList.Add("--google-service-account-json")
    $null = $paramList.Add($GoogleServiceAccountJson)
}

# ---------------------------------------------------------------------------
# Avvia lo scraper
# ---------------------------------------------------------------------------
$currentWeek = "{0}-W{1:D2}" -f (Get-Date -UFormat "%G"), [int](Get-Date -UFormat "%V")
Write-Log "Avvio scraper Deliveroo per settimana $currentWeek..."

Push-Location $PSScriptRoot
& $python $paramList.ToArray()
$scrapeExit = $LASTEXITCODE
Pop-Location

if ($scrapeExit -eq 0) {
    Write-Log "Scraper completato con successo."
    Send-Notify -Subject "Scraper Deliveroo completato $currentWeek" -Body "Lo scraper Deliveroo ha terminato con successo. I dati sono pronti per la pipeline delle 20:00."
} elseif ($scrapeExit -eq 2) {
    Write-Log "Scraper interrotto (resume attivo per la prossima esecuzione)."
    # Uscita con codice 2 = interruzione manuale o blocco, non un errore vero
} else {
    $errMsg = "Lo scraper Deliveroo ha terminato con errore (exit code $scrapeExit). Controlla il log."
    Write-Log "ERRORE: $errMsg"
    Send-Notify -Subject "ERRORE scraper Deliveroo $currentWeek" -Body $errMsg -IsError
}
