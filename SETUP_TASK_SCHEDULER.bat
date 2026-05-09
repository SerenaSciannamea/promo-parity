@echo off
:: ===========================================================================
:: SETUP_TASK_SCHEDULER.bat
:: Registra i due task automatici del venerdi' sera.
:: Eseguire con TASTO DESTRO -> "Esegui come amministratore"
:: ===========================================================================

set PROJ=C:\Users\SerenaSciannamea\Desktop\Promo Parity Serena
set PWSH=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe
set ARGS=-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass

echo.
echo ============================================================
echo  Setup Task Scheduler - Promo Parity Venerdi Sera
echo ============================================================
echo.

:: --------------------------------------------------------------------------
:: TASK 1 - Scraper Deliveroo (ore 15:00)
:: Gira per ore -> si avvia nel pomeriggio cosi finisce entro sera
:: --------------------------------------------------------------------------
set TASK1=PromoParity_Scraper_Deliveroo

schtasks /delete /tn "%TASK1%" /f >nul 2>&1

schtasks /create /tn "%TASK1%" ^
  /tr "%PWSH% %ARGS% -File \"%PROJ%\run_scrape.ps1\"" ^
  /sc WEEKLY /d FRI /st 15:00 ^
  /f /rl HIGHEST ^
  /sd 01/01/2026

if %ERRORLEVEL% == 0 (
    echo [OK] Task 1 registrato: Scraper Deliveroo ogni VENERDI alle 15:00
) else (
    echo [ERRORE] Task 1 fallito. Assicurati di eseguire come Amministratore.
    goto :end
)

:: --------------------------------------------------------------------------
:: TASK 2 - Pipeline Parity (ore 20:00)
:: Gira dopo che lo scraper ha finito e tu hai scaricato il CSV Glovo
:: --------------------------------------------------------------------------
set TASK2=PromoParity_Pipeline_Parity

schtasks /delete /tn "%TASK2%" /f >nul 2>&1

schtasks /create /tn "%TASK2%" ^
  /tr "%PWSH% %ARGS% -File \"%PROJ%\run_friday.ps1\"" ^
  /sc WEEKLY /d FRI /st 20:00 ^
  /f /rl HIGHEST ^
  /sd 01/01/2026

if %ERRORLEVEL% == 0 (
    echo [OK] Task 2 registrato: Pipeline Parity ogni VENERDI alle 20:00
) else (
    echo [ERRORE] Task 2 fallito.
    goto :end
)

echo.
echo ============================================================
echo  Riepilogo task registrati:
echo ============================================================
schtasks /query /tn "%TASK1%" /fo LIST 2>nul | findstr "Task Name\|Next Run\|Status"
schtasks /query /tn "%TASK2%" /fo LIST 2>nul | findstr "Task Name\|Next Run\|Status"
echo.
echo Tutto pronto! Ogni venerdi:
echo   15:00 -> Scraper Deliveroo (automatico)
echo   20:00 -> Pipeline Parity   (automatico, dopo che scarichi il CSV Glovo)
echo.

:end
pause
