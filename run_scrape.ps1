param(
    [string]$Cities = "",
    [int]$MaxPointsPerCity = 0,
    [switch]$ShowBrowser,
    [string]$StoresCsv = "",
    [string]$GoogleSheet = "",
    [int]$GoogleWorksheetGid = 0,
    [string]$GoogleServiceAccountJson = "",
    [int]$SkipCityAfterSameResults = 4
)

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "deliveroo_promo_parity.py"
$polygons = Join-Path $PSScriptRoot "Polygons.csv"

$paramList = New-Object System.Collections.Generic.List[string]
$null = $paramList.Add($script)
$null = $paramList.Add("--polygons")
$null = $paramList.Add($polygons)
$null = $paramList.Add("--sample-step-km")
$null = $paramList.Add("2.5")
$null = $paramList.Add("--max-points-per-city")
$null = $paramList.Add("$MaxPointsPerCity")
$null = $paramList.Add("--skip-city-after-same-results")
$null = $paramList.Add("$SkipCityAfterSameResults")

if ($Cities -ne "") {
    $null = $paramList.Add("--city-codes")
    $null = $paramList.Add($Cities)
}

if ($ShowBrowser) {
    $null = $paramList.Add("--show")
}

if ($StoresCsv -ne "") {
    $null = $paramList.Add("--stores-csv")
    $null = $paramList.Add($StoresCsv)
    $null = $paramList.Add("--stores-column-index")
    $null = $paramList.Add("1")
}

if ($GoogleSheet -ne "") {
    $null = $paramList.Add("--google-sheet")
    $null = $paramList.Add($GoogleSheet)
}

if ($GoogleWorksheetGid -ne 0) {
    $null = $paramList.Add("--google-worksheet-gid")
    $null = $paramList.Add("$GoogleWorksheetGid")
}

if ($GoogleServiceAccountJson -ne "") {
    $null = $paramList.Add("--google-service-account-json")
    $null = $paramList.Add($GoogleServiceAccountJson)
}

& $python $paramList.ToArray()
