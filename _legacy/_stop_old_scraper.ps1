# _stop_old_scraper.ps1 — ferma lo scraper Selenium di produzione (ESEGUIRE COME AMMINISTRATORE)
$ErrorActionPreference = "Continue"
$task = "Deliveroo Scraper Promo Parity"
$log  = "C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena\data\_stop_old.log"
function Log($m){ $l="[{0:HH:mm:ss}] {1}" -f (Get-Date),$m; Write-Host $l; Add-Content -Path $log -Value $l -Encoding UTF8 }
Set-Content -Path $log -Value "=== stop old scraper $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding UTF8

try { Stop-ScheduledTask -TaskName $task; Log "Stop-ScheduledTask OK" } catch { Log "Stop-ScheduledTask: $($_.Exception.Message)" }
Start-Sleep -Seconds 2

# kill chromedriver (sicuro) e i python che girano lo scraper Selenium (NON il Chrome personale)
Get-Process chromedriver -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Log "chromedriver killati"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*deliveroo_promo_parity*' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; Log "kill python pid $($_.ProcessId)" } catch {} }
# kill eventuale wrapper run_scrape.ps1 ancora vivo
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*run_scrape.ps1*' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; Log "kill wrapper pid $($_.ProcessId)" } catch {} }

$st = (Get-ScheduledTask -TaskName $task).State
Log "Stato task ora: $st"
Log "FATTO."
