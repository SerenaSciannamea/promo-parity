# ===========================================================================
# run_friday.ps1
# Pipeline settimanale Promo Parity  —  eseguito ogni venerdi' sera
#
# Parametri opzionali:
#   -GlovoCsv   <path>   CSV Glovo (default: ultima versione in Downloads)
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

# ---------- Trova il CSV Glovo piu' recente in Downloads ----------
if (-not $GlovoCsv) {
    $downloads = "$env:USERPROFILE\Downloads"
    $candidate = Get-ChildItem $downloads -Filter "*.csv" |
                 Where-Object { $_.Name -like "*Prio*" -or $_.Name -like "*glovo*" -or $_.Name -like "*Products*" } |
                 Sort-Object LastWriteTime -Descending |
                 Select-Object -First 1

    if ($candidate) {
        $GlovoCsv = $candidate.FullName
        Write-Log "CSV Glovo trovato automaticamente: $GlovoCsv"
    } else {
        Write-Log "ERRORE: Nessun CSV Glovo trovato in Downloads. Specificare -GlovoCsv <path>"
        exit 1
    }
}

# ---------- Step 1: Pipeline parity ----------
Write-Log "Step 1: Pipeline parity Glovo vs Deliveroo"
$args_list = @("-m", "pipeline.run_weekly", "--glovo-csv", $GlovoCsv)
if ($Week) { $args_list += @("--week", $Week) }

Push-Location $proj
& $venv @args_list
$exit1 = $LASTEXITCODE
Pop-Location

if ($exit1 -ne 0) {
    Write-Log "ERRORE nella pipeline parity (exit code $exit1)"
    exit $exit1
}
Write-Log "Pipeline parity completata"

# ---------- Step 2: Scraper Deliveroo (opzionale — decommentare se vuoi eseguirlo) ----------
# Write-Log "Step 2: Scraper Deliveroo"
# & "$proj\run_scrape.ps1"
# Write-Log "Scraper Deliveroo completato"

Write-Log "===== Pipeline completata con successo ====="
Write-Log "Apri il dashboard: cd '$proj' && .venv\Scripts\streamlit run app.py"
