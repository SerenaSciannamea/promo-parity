# _restart_scraper.ps1 — recupero pulito del run bloccato (ESEGUIRE COME AMMINISTRATORE)
# Ferma il task + uccide wrapper/python zombie, poi ri-triggera il task.
# Il wrapper riparte e RIPRENDE dai geohash gia' coperti (sample_status di oggi).
$task = "Promo HTTP Scraper"
$log  = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\data\_restart_scraper.log"
function Log($m){ $l="[{0:HH:mm:ss}] {1}" -f (Get-Date),$m; Add-Content -Path $log -Value $l -Encoding UTF8 }
Set-Content -Path $log -Value "=== restart $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding UTF8

try { Stop-ScheduledTask -TaskName $task -ErrorAction Stop; Log "Stop-ScheduledTask OK" } catch { Log "Stop task: $($_.Exception.Message)" }
Start-Sleep -Seconds 2

# uccidi wrapper zombie (powershell che gira run_promo_http.ps1) e python residui
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*run_promo_http.ps1*' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; Log "kill wrapper PID $($_.ProcessId)" } catch {} }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; Log "kill python PID $($_.ProcessId)" } catch {} }
Start-Sleep -Seconds 3

# ri-triggera il task -> wrapper fresco -> resume dai 263 geohash
try { Start-ScheduledTask -TaskName $task -ErrorAction Stop; Log "Start-ScheduledTask OK (resume)" } catch { Log "Start task: $($_.Exception.Message)" }
Start-Sleep -Seconds 20
$py = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'").Count
Log "Dopo 20s: python attivi = $py | task stato = $((Get-ScheduledTask -TaskName $task).State)"
Log "FATTO."
