# ===========================================================================
# watchdog_promo_http.ps1 — sorveglia il run serale dello scraper ufficiale.
# Gira ogni ~20 min (Mar/Ven). Recupera DUE tipi di guasto:
#   A) run morto   (nessun processo)         -> (ri)avvia il task
#   B) run APPESO  (processo vivo ma output fermo da >STALL_MIN) -> stop+kill+restart
# Idempotente: non fa nulla se il run e' sano o gia' completato.
# ===========================================================================
$ErrorActionPreference = "Continue"
$proj    = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena"
$task    = "Promo HTTP Scraper"
$outDir  = "$proj\output_http_promo"
$marker  = "$proj\data\promo_http_last_success.txt"
$log     = "$proj\data\promo_http_watchdog.log"
$STALL_MIN = 5    # output fermo da piu' di questi minuti = stallo/hang (watchdog gira ogni 3 min)
New-Item -ItemType Directory -Force -Path "$proj\data" | Out-Null
function Log($m) { Add-Content -Path $log -Value ("[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $m) -Encoding UTF8 }

$today = Get-Date -Format "yyyy-MM-dd"

# 1) run di oggi gia' completato?
if (Test-Path $marker) {
    if ((Get-Content $marker -Raw).Trim() -eq $today) { Log "Run di $today gia' completato -> nulla da fare."; return }
}

# 2) processi attivi + freschezza output (newest mtime tra i file che lo scraper scrive)
$pyRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue).Count
$files = @("deliveroo_promo_raw.csv","deliveroo_sample_status.csv","deliveroo_store_index.csv") |
         ForEach-Object { Join-Path $outDir $_ } | Where-Object { Test-Path $_ }
$newest = if ($files) { ($files | ForEach-Object { (Get-Item $_).LastWriteTime } | Measure-Object -Maximum).Maximum } else { $null }
$staleMin = if ($newest) { [int]((Get-Date) - $newest).TotalMinutes } else { 999 }

# 3) SANO? processo vivo E output avanzato di recente
if ($pyRunning -gt 0 -and $newest -and $staleMin -lt $STALL_MIN) {
    Log "Run sano (python=$pyRunning, output aggiornato $staleMin min fa) -> ok."
    return
}

# 4) guasto: morto (nessun processo) oppure APPESO (output fermo da >$STALL_MIN). Restart pulito.
Log "GUASTO rilevato (python=$pyRunning, output fermo da $staleMin min) -> stop+kill+restart del task."
try { Stop-ScheduledTask -TaskName $task -ErrorAction Stop } catch { Log "  stop: $($_.Exception.Message)" }
Start-Sleep -Seconds 2
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*run_promo_http.ps1*' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
Start-Sleep -Seconds 3
try { Start-ScheduledTask -TaskName $task -ErrorAction Stop; Log "  task ri-avviato (resume dai geohash gia' coperti)." }
catch { Log "  start: $($_.Exception.Message)" }
