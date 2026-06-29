# ===========================================================================
# watchdog_promo_http.ps1 — sorveglia il run serale dello scraper ufficiale.
# Gira ogni ~20 min nelle fasce Mar/Ven (via task pianificato). Se trova il run
# INCOMPLETO e FERMO (nessun processo scraper/wrapper attivo e marker di oggi
# assente) -> rilancia il wrapper, che riprende dal sample_status.
# Idempotente: non fa nulla se il run e' gia' in corso o gia' completato.
# ===========================================================================
$ErrorActionPreference = "Continue"
$proj    = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena"
$wrapper = "$proj\run_promo_http.ps1"
$marker  = "$proj\data\promo_http_last_success.txt"
$log     = "$proj\data\promo_http_watchdog.log"
New-Item -ItemType Directory -Force -Path "$proj\data" | Out-Null
function Log($m) { Add-Content -Path $log -Value ("[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m) -Encoding UTF8 }

$today = Get-Date -Format "yyyy-MM-dd"

# 1) run di oggi gia' completato?
if (Test-Path $marker) {
    if ((Get-Content $marker -Raw).Trim() -eq $today) { Log "Run di $today gia' completato -> nulla da fare."; return }
}

# 2) scraper o wrapper gia' in esecuzione?
$pyRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*promo_http_scraper*' }).Count
$wrapRunning = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*run_promo_http.ps1*' }).Count
if ($pyRunning -gt 0 -or $wrapRunning -gt 0) {
    Log "Run in corso (python=$pyRunning, wrapper=$wrapRunning) -> nulla da fare."
    return
}

# 3) incompleto e fermo -> rilancio il wrapper (riprende dal punto di interruzione)
Log "Run NON completato e nessun processo attivo -> RILANCIO il wrapper (resume)."
Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-ExecutionPolicy","Bypass","-File","`"$wrapper`""
Log "Wrapper rilanciato."
