# ===========================================================================
# start_dashboard.ps1
# Avvia il dashboard Streamlit nel browser
# Doppio clic per aprire il dashboard
# ===========================================================================
$proj      = $PSScriptRoot
$streamlit = "$proj\.venv\Scripts\streamlit.exe"

Write-Host "Avvio Promo Parity Dashboard..." -ForegroundColor Cyan
Write-Host "Apri http://localhost:8501 nel browser" -ForegroundColor Yellow
Write-Host "Premi CTRL+C per fermare" -ForegroundColor Gray

Set-Location $proj
& $streamlit run app.py --server.headless false --browser.gatherUsageStats false
