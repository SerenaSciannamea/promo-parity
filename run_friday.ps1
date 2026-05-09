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

Write-Log "===== Avvio pipeline Promo Parity ====="

# ===========================================================================
# CONFIGURAZIONE — compila questi due campi una volta sola
# ===========================================================================

# ID del Google Sheet di OUTPUT (gia' configurato)
$outputSheetsId = "1lAsH0CaoJ3Lfp8uNaJ0-Bu3wTxlO-pn186z_coInnVs"

# Path al JSON del service account (gia' configurato)
$sheetsSa = "$env:USERPROFILE\Downloads\dogwood-sprite-400413-528afc69c595.json"

# ID del Google Sheet con i dati Glovo (BigQuery connector)
# → copia l'ID dalla URL del tuo foglio Glovo:
#   https://docs.google.com/spreadsheets/d/  <-- ID QUI -->  /edit
$glovoSheetId = "1ah5GsEJaSnv-S8jYytar3Vn9tU8MD8IITfNAWtmtveE"

# Nome del tab che contiene i dati Glovo
# Il connettore BigQuery crea un tab con prefisso [RAW] — lo gestiamo automaticamente
$glovoWorksheet = "[RAW]Products"

# ===========================================================================
# STEP 0 — Scarica automaticamente il CSV Glovo da Google Sheets
# ===========================================================================

# Settimana corrente (es. 2026-W20)
$currentWeek = "{0}-W{1:D2}" -f (Get-Date -UFormat "%G"), [int](Get-Date -UFormat "%V")
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
        if ($glovoWorksheet) {
            $dlArgs += @("--worksheet", $glovoWorksheet)
        }

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
    } else {
        if (-not $glovoSheetId) {
            Write-Log "glovoSheetId non configurato in run_friday.ps1. Cerco in Downloads..."
        }
    }

    # Fallback: cerca in Downloads (come prima)
    if (-not $GlovoCsv) {
        $downloads = "$env:USERPROFILE\Downloads"
        $candidate = Get-ChildItem $downloads -Filter "*.csv" |
                     Where-Object { $_.Name -like "*Prio*" -or $_.Name -like "*glovo*" -or $_.Name -like "*Products*" } |
                     Sort-Object LastWriteTime -Descending |
                     Select-Object -First 1

        if ($candidate) {
            $GlovoCsv = $candidate.FullName
            Write-Log "CSV Glovo trovato in Downloads: $GlovoCsv"
        } else {
            Write-Log "ERRORE: Impossibile trovare il CSV Glovo. Configura glovoSheetId oppure scaricalo manualmente in Downloads."
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
    Write-Log "Export su Google Sheets attivo (sheet: $outputSheetsId)"
} else {
    Write-Log "ATTENZIONE: File credenziali non trovato ($sheetsSa). Export Sheets saltato."
}

Push-Location $proj
& $venv @args_list
$exit1 = $LASTEXITCODE
Pop-Location

if ($exit1 -ne 0) {
    Write-Log "ERRORE nella pipeline parity (exit code $exit1)"
    exit $exit1
}
Write-Log "Pipeline parity completata"

Write-Log "===== Pipeline completata con successo ====="
Write-Log "Apri il dashboard: cd '$proj' && .venv\Scripts\streamlit run app.py"
