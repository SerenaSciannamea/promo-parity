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
# STEP 0a — Verifica che i dati Deliveroo siano aggiornati questa settimana
#            Se non lo sono, il confronto sarebbe settimane diverse -> abort
# ===========================================================================

$deliverooCsv    = "$proj\output\deliveroo_promo_deduped.csv"
$currentWeek     = "{0}-W{1:D2}" -f (Get-Date -UFormat "%G"), [int](Get-Date -UFormat "%V")

# Calcola il lunedì della settimana corrente (inizio settimana ISO)
$today           = Get-Date
$dayOfWeek       = [int]$today.DayOfWeek   # 0=Sun, 1=Mon ... 6=Sat
if ($dayOfWeek -eq 0) { $daysToMonday = 6 } else { $daysToMonday = $dayOfWeek - 1 }
$weekStart       = $today.AddDays(-$daysToMonday).Date

if (Test-Path $deliverooCsv) {
    $rooModified = (Get-Item $deliverooCsv).LastWriteTime
    if ($rooModified -lt $weekStart) {
        $rooAge = "{0:dd/MM/yyyy HH:mm}" -f $rooModified
        $errMsg = "BLOCCO: deliveroo_promo_deduped.csv non e' aggiornato questa settimana (ultima modifica: $rooAge). Aggiorna prima i dati Deliveroo."
        Write-Log "ERRORE: $errMsg"
        Send-Notify -Subject "BLOCCO pipeline $currentWeek — Deliveroo non aggiornato" -Body $errMsg -IsError
        exit 1
    }
    Write-Log "Deliveroo OK: aggiornato il $("{0:dd/MM/yyyy HH:mm}" -f $rooModified)"

    # -----------------------------------------------------------------------
    # Verifica copertura città: tutte le city code Glovo devono avere dati Deliveroo
    # -----------------------------------------------------------------------
    $expectedCities = @("BAR","BOL","CAT","FIR","MIL","NAP","PAD","PMO","QTC","ROM","TOR","VER")

    $foundCities = & $venv -c @"
import pandas as pd, sys
try:
    df = pd.read_csv(r'$deliverooCsv', dtype=str, usecols=['city_code']).fillna('')
    cities = sorted(df['city_code'].str.strip().str.upper().unique().tolist())
    print(','.join(cities))
except Exception as e:
    print('ERROR:' + str(e), file=sys.stderr)
    sys.exit(1)
"@

    $foundList   = $foundCities -split ","
    $missingList = $expectedCities | Where-Object { $_ -notin $foundList }

    if ($missingList.Count -gt 0) {
        $missingStr = $missingList -join ", "
        Write-Log "ATTENZIONE: città Deliveroo mancanti: $missingStr"

        Write-Host ""
        Write-Host "============================================================" -ForegroundColor Yellow
        Write-Host " ATTENZIONE — Città senza dati Deliveroo: $missingStr" -ForegroundColor Yellow
        Write-Host " La pipeline produrra' risultati parziali per queste citta'." -ForegroundColor Yellow
        Write-Host " Premi INVIO per continuare comunque, oppure Ctrl+C per annullare." -ForegroundColor Yellow
        Write-Host "============================================================" -ForegroundColor Yellow
        Write-Host ""

        try {
            $null = Read-Host "Premi INVIO per continuare"
        } catch {
            # In esecuzione non interattiva (es. Task Scheduler): logga e prosegui
            Write-Log "Esecuzione non interattiva: pipeline continua nonostante citta' mancanti ($missingStr)"
        }

        # Invia notifica email di avviso (non blocca)
        $warnBody = "Attenzione: le seguenti citta' non hanno dati Deliveroo per $currentWeek e saranno escluse dal confronto prodotti:`n`n$missingStr`n`nLo scraper potrebbe aver girato troppo presto. Valuta di riscrapare e ripetere la pipeline."
        Send-Notify -Subject "AVVISO pipeline $currentWeek — citta' Deliveroo mancanti" -Body $warnBody
    } else {
        Write-Log "Copertura Deliveroo completa: $($foundList -join ', ')"
    }

} else {
    $errMsg = "BLOCCO: deliveroo_promo_deduped.csv non trovato in $deliverooCsv"
    Write-Log "ERRORE: $errMsg"
    Send-Notify -Subject "BLOCCO pipeline $currentWeek — file Deliveroo mancante" -Body $errMsg -IsError
    exit 1
}

# ===========================================================================
# STEP 0b — Scarica automaticamente il CSV Glovo da Google Sheets
# ===========================================================================

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
