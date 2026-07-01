# _schedule_promo_http.ps1 — rende ufficiale lo scraper HTTP (ESEGUIRE COME AMMINISTRATORE)
#  - DISABILITA (non cancella) il vecchio task Selenium
#  - REGISTRA il nuovo task per OGGI alle 19:30, su tutti i poligoni, griglia 2.5km
#  - finestra visibile (-NoExit) per il log live
$ErrorActionPreference = "Continue"
$newTask = "Promo HTTP Scraper"
$oldTask = "Deliveroo Scraper Promo Parity"
$wrapper = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\run_promo_http.ps1"
$log     = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\data\_schedule_promo_http.log"
function Log($m){ $l="[{0:HH:mm:ss}] {1}" -f (Get-Date),$m; Write-Host $l; Add-Content -Path $log -Value $l -Encoding UTF8 }
Set-Content -Path $log -Value "=== schedule promo http $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding UTF8

# 1) disabilita (mantiene) il vecchio
try { Disable-ScheduledTask -TaskName $oldTask -ErrorAction Stop | Out-Null; Log "Vecchio task '$oldTask' DISABILITATO (non cancellato)." }
catch { Log "Vecchio task: $($_.Exception.Message)" }

# 2) rimuovi eventuale registrazione precedente del nuovo
try { Unregister-ScheduledTask -TaskName $newTask -Confirm:$false -ErrorAction Stop; Log "Rimossa registrazione precedente di '$newTask'." } catch {}

# 3) registra il nuovo: RICORRENTE Martedi + Venerdi 19:30 (peak)
$when = (Get-Date).Date.AddHours(19).AddMinutes(30)
$action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$wrapper`""
$trigger   = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday, Friday -At $when
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 8)
try {
    Register-ScheduledTask -TaskName $newTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description "Scraper ufficiale Promo Parity (HTTP) - Mar/Ven 19:30, tutti i poligoni griglia 3km, poi pipeline" -Force | Out-Null
    Log "Nuovo task '$newTask' registrato: Martedi e Venerdi alle $($when.ToString('HH:mm'))."
} catch { Log "ERRORE registrazione: $($_.Exception.Message)" }

# verifica
$o = Get-ScheduledTask -TaskName $oldTask -ErrorAction SilentlyContinue
$n = Get-ScheduledTask -TaskName $newTask -ErrorAction SilentlyContinue
Log "Stato vecchio: $($o.State) | Stato nuovo: $($n.State) | NextRun nuovo: $((Get-ScheduledTaskInfo -TaskName $newTask).NextRunTime)"
Log "FATTO."
