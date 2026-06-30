# _schedule_watchdog.ps1 — registra il task WATCHDOG (ESEGUIRE COME AMMINISTRATORE)
# Mar/Ven dalle 19:35, ripete ogni 20 min per 8h. Rilancia il wrapper se il run si ferma.
$ErrorActionPreference = "Continue"
$task    = "Promo HTTP Watchdog"
$script  = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\watchdog_promo_http.ps1"
$log     = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\data\_schedule_watchdog.log"
function Log($m){ $l="[{0:HH:mm:ss}] {1}" -f (Get-Date),$m; Write-Host $l; Add-Content -Path $log -Value $l -Encoding UTF8 }
Set-Content -Path $log -Value "=== schedule watchdog $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding UTF8

try { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction Stop; Log "Rimossa registrazione precedente." } catch {}

$at  = (Get-Date).Date.AddHours(19).AddMinutes(35)
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday, Friday -At $at
# Aggiunge la ripetizione (ogni 20 min per 8h) al trigger settimanale
$rep = New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval (New-TimeSpan -Minutes 3) -RepetitionDuration (New-TimeSpan -Hours 12)
$trg.Repetition = $rep.Repetition

$action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
try {
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trg -Principal $principal -Settings $settings `
        -Description "Watchdog scraper Promo Parity: rilancia il run se si ferma (Mar/Ven, ogni 20 min)" -Force | Out-Null
    Log "Task '$task' registrato: Mar/Ven dalle $($at.ToString('HH:mm')), ripetizione 20 min per 8h."
} catch { Log "ERRORE registrazione: $($_.Exception.Message)" }

$t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
if ($t) { Log "Stato: $($t.State) | NextRun: $((Get-ScheduledTaskInfo -TaskName $task).NextRunTime)" }
Log "FATTO."
