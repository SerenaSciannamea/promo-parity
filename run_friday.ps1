# ===========================================================================
# run_friday.ps1
# Pipeline settimanale Promo Parity  —  eseguito ogni venerdi' sera
#
# Parametri opzionali:
#   -GlovoCsv   <path>   CSV Glovo (se vuoto: scarica automaticamente da Sheets)
#   -Week       <str>    Settimana (es. 2026-W20). Default: settimana corrente
# ===========================================================================
param(
    [string]$GlovoCsv = "",
    [string]$Week     = ""
)

$ErrorActionPreference = "Stop"
$proj   = $PSScriptRoot
$venv   = "$proj\.venv\Scripts\python.exe"
$log    = "$proj\data\pipeline_log.txt"

# Crea cartella data se mancante
New-Item -ItemType Directory -Force -Path "$proj\data" | Out-Null

function Write-Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Send-Notify {
    param([string]$Subject, [string]$Body, [switch]$IsError)
    if (-not $emailAppPassword) { return }
    $errorFlag = if ($IsError) { "--error" } else { "" }
    $notifyArgs = @(
        "-m", "pipeline.notifier",
        "--subject",      $Subject,
        "--body",         $Body,
        "--app-password", $emailAppPassword,
        "--log",          $log
    )
    if ($IsError) { $notifyArgs += "--error" }
    Push-Location $proj
    & $venv @notifyArgs 2>$null
    Pop-Location
}

Write-Log "===== Avvio pipeline Promo Parity ====="

# ===========================================================================
# CONFIGURAZIONE
# ===========================================================================

$outputSheetsId  = "1lAsH0CaoJ3Lfp8uNaJ0-Bu3wTxlO-pn186z_coInnVs"
$sheetsSa        = "$env:USERPROFILE\Downloads\dogwood-sprite-400413-528afc69c595.json"
$glovoSheetId    = "1ah5GsEJaSnv-S8jYytar3Vn9tU8MD8IITfNAWtmtveE"
$glovoWorksheet  = "Products"

# App Password Gmail — letta da secrets.ps1 (mai committato su GitHub)
$emailAppPassword = ""
$secretsFile = "$proj\secrets.ps1"
if (Test-Path $secretsFile) { . $secretsFile }

# ===========================================================================
# STEP 0 — Scarica automaticamente il CSV Glovo da Google Sheets
# ===========================================================================

$currentWeek  = "{0}-W{1:D2}" -f (Get-Date -UFormat "%G"), [int](Get-Date -UFormat "%V")
$autoGlovoCsv = "$proj\data\glovo_auto_$currentWeek.csv"

if (-not $GlovoCsv) {

    if ($glovoSheetId -and (Test-Path $sheetsSa)) {
        Write-Log "Step 0: Download automatico CSV Glovo da Google Sheets..."

        $dlArgs = @(
            "-m", "pipeline.glovo_downloader",
            "--sheet-id",  $glovoSheetId,
            "--sa-json",   $sheetsSa,
            "--output",    $autoGlovoCsv
        )
        if ($glovoWorksheet) { $dlArgs += @("--worksheet", $glovoWorksheet) }

        Push-Location $proj
        & $venv @dlArgs
        $dlExit = $LASTEXITCODE
        Pop-Location

        if ($dlExit -eq 0 -and (Test-Path $autoGlovoCsv)) {
            $GlovoCsv = $autoGlovoCsv
            Write-Log "CSV Glovo scaricato automaticamente: $GlovoCsv"
        } else {
            Write-Log "ATTENZIONE: Download automatico fallito (exit $dlExit). Cerco in Downloads..."
        }
    }

    # Fallback: cerca in Downloads
    if (-not $GlovoCsv) {
        $downloads = "$env:USERPROFILE\Downloads"
        $candidate = Get-ChildItem $downloads -Filter "*.csv" |
                     Where-Object { $_.Name -like "*Prio*" -or $_.Name -like "*glovo*" -or $_.Name -like "*Products*" } |
                     Sort-Object LastWriteTime -Descending |
                     Select-Object -First 1

        if ($candidate) {
            $GlovoCsv = $candidate.FullName
            Write-Log "CSV Glovo trovato in Downloads (fallback): $GlovoCsv"
        } else {
            $errMsg = "Impossibile trovare il CSV Glovo. Download automatico fallito e nessun file in Downloads."
            Write-Log "ERRORE: $errMsg"
            Send-Notify -Subject "ERRORE pipeline $currentWeek" -Body $errMsg -IsError
            exit 1
        }
    }
}

# ===========================================================================
# STEP 1 — Pipeline parity
# ===========================================================================
Write-Log "Step 1: Pipeline parity Glovo vs Deliveroo (CSV: $GlovoCsv)"
$args_list = @("-m", "pipeline.run_weekly", "--glovo-csv", $GlovoCsv)
if ($Week) { $args_list += @("--week", $Week) }

if (Test-Path $sheetsSa) {
    $args_list += @("--sheets-id", $outputSheetsId, "--sheets-sa", $sheetsSa)
    Write-Log "Export su Google Sheets attivo"
} else {
    Write-Log "ATTENZIONE: credenziali non trovate, export Sheets saltato."
}

Push-Location $proj
& $venv @args_list
$exit1 = $LASTEXITCODE
Pop-Location

if ($exit1 -ne 0) {
    $errMsg = "Pipeline parity terminata con errore (exit code $exit1). Controlla il log."
    Write-Log "ERRORE: $errMsg"
    Send-Notify -Subject "ERRORE pipeline $currentWeek" -Body $errMsg -IsError
    exit $exit1
}

Write-Log "Pipeline parity completata"

# ===========================================================================
# Notifica di successo con riepilogo
# ===========================================================================
$successBody = @"
La pipeline settimanale e' terminata con successo.

Settimana:  $currentWeek
CSV Glovo:  $GlovoCsv
Dashboard:  https://promo-parity.streamlit.app

Controlla la dashboard per vedere i risultati aggiornati.
"@

Write-Log "===== Pipeline completata con successo ====="
Send-Notify -Subject "Pipeline completata $currentWeek" -Body $successBody
