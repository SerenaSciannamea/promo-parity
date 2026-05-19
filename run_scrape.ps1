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

# App Password Gmail - letta da secrets.ps1 (mai committato su GitHub)
$emailAppPassword = ""
$secretsFile = Join-Path $PSScriptRoot "secrets.ps1"
if (Test-Path $secretsFile) { . $secretsFile }

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
# ---------------------------------------------------------------------------
$rawCsv = Join-Path $outputDir "deliveroo_promo_raw.csv"

if (Test-Path $rawCsv) {
    $now         = Get-Date
    $currentWeek = "{0}-W{1:D2}" -f (Get-Date -UFormat "%G"), [int](Get-Date -UFormat "%V")

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
$null = $paramList.Add("4.5")
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
# Avvia lo scraper con auto-restart in caso di crash
# ---------------------------------------------------------------------------
$currentWeek = "{0}-W{1:D2}" -f (Get-Date -UFormat "%G"), [int](Get-Date -UFormat "%V")
Write-Log "Avvio scraper Deliveroo per settimana $currentWeek..."

$maxRetries = 20
$attempt    = 0
$scrapeExit = 1

while ($scrapeExit -ne 0 -and $scrapeExit -ne 2 -and $attempt -lt $maxRetries) {
    if ($attempt -gt 0) {
        Write-Log "Auto-restart #$attempt - attendo 5 secondi e riprendo dal punto di interruzione..."
        Start-Sleep -Seconds 5
        Get-Process chrome, chromedriver -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    $attempt++
    Push-Location $PSScriptRoot
    & $python $paramList.ToArray()
    $scrapeExit = $LASTEXITCODE
    Pop-Location
}

if ($scrapeExit -eq 0) {
    Write-Log "Scraper completato con successo (tentativi: $attempt)."
    Send-Notify -Subject "Scraper Deliveroo completato $currentWeek" -Body "Lo scraper Deliveroo ha terminato con successo dopo $attempt tentativo/i. I dati sono pronti per la pipeline."

    # ---------------------------------------------------------------------------
    # Avvia automaticamente la pipeline parity al termine dello scraper
    # ---------------------------------------------------------------------------
    $fridayScript = Join-Path $PSScriptRoot "run_friday.ps1"
    if (Test-Path $fridayScript) {
        Write-Log "Avvio automatico pipeline parity..."
        Push-Location $PSScriptRoot
        & powershell.exe -NonInteractive -ExecutionPolicy Bypass -File $fridayScript
        $pipelineExit = $LASTEXITCODE
        Pop-Location
        if ($pipelineExit -eq 0) {
            Write-Log "Pipeline parity completata con successo."
        } else {
            Write-Log "ERRORE: Pipeline parity terminata con exit code $pipelineExit."
        }
    } else {
        Write-Log "ATTENZIONE: run_friday.ps1 non trovato, pipeline non avviata."
    }

} elseif ($scrapeExit -eq 2) {
    Write-Log "Scraper interrotto manualmente (exit 2). Pipeline non avviata."
} else {
    $errMsg = "Lo scraper Deliveroo ha terminato con errore dopo $maxRetries tentativi (exit code $scrapeExit)."
    Write-Log "ERRORE: $errMsg"
    Send-Notify -Subject "ERRORE scraper Deliveroo $currentWeek" -Body $errMsg -IsError
    Write-Log "Pipeline non avviata a causa dell'errore dello scraper."
}
