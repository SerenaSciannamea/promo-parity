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

$now   = Get-Date
$today = $now.ToString("yyyy-MM-dd")

# 0) FINESTRA OPERATIVA: il watchdog agisce SOLO durante (e subito dopo) un run
# schedulato. Lo scraper parte Mar(2)/Ven(5) alle 19:30 e puo' proseguire oltre
# mezzanotte, quindi la finestra e' [sera del run 19:00] .. [mattina dopo 06:00].
# Fuori da questa finestra NON si tocca nulla: cosi' un run gia' finito la sera non
# viene erroneamente "rianimato" dopo mezzanotte quando la data cambia.
$dow = [int]$now.DayOfWeek   # Dom=0, Lun=1, Mar=2, Mer=3, Gio=4, Ven=5, Sab=6
if (($dow -eq 2 -or $dow -eq 5) -and $now.Hour -ge 19) {
    $runDay = $today                              # sera del run (Mar/Ven >= 19:00)
} elseif (($dow -eq 3 -or $dow -eq 6) -and $now.Hour -lt 6) {
    $runDay = $now.AddDays(-1).ToString("yyyy-MM-dd")  # notte dopo (Mer/Sab < 06:00)
} else {
    Log "Fuori finestra run schedulato (dow=$dow h=$($now.Hour)) -> non intervengo."; return
}

# 1) run gia' completato? Confronta il marker sia con OGGI sia col giorno-del-run:
# un run finito a tarda sera scrive il marker con la data di quel giorno, che dopo
# mezzanotte diventa "ieri" ma coincide comunque con $runDay -> resta uno stand-down.
if (Test-Path $marker) {
    $mk = (Get-Content $marker -Raw).Trim()
    if ($mk -eq $today -or $mk -eq $runDay) { Log "Run di $runDay gia' completato (marker=$mk) -> nulla da fare."; return }
}

# 1b) pipeline in corso? Durante la pipeline i file dello scraper NON si aggiornano:
# non va scambiato per uno stallo (era la causa del loop di restart). Guardia
# anti-flag-orfano: se il flag e' piu' vecchio di 40 min lo ignoro.
$pflag = "$proj\data\pipeline_running.flag"
if (Test-Path $pflag) {
    $ageMin = [int]((Get-Date) - (Get-Item $pflag).LastWriteTime).TotalMinutes
    if ($ageMin -lt 40) { Log "Pipeline in corso (flag di $ageMin min fa) -> non intervengo."; return }
    Log "Flag pipeline STALE ($ageMin min) -> lo ignoro e continuo i controlli."
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
