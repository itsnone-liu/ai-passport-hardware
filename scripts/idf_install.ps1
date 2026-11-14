# Install ESP-IDF v5.5.3 toolchain for esp32c3 (tools on D:, China download mirror)
$ErrorActionPreference = 'Continue'
$env:IDF_TOOLS_PATH    = 'D:\esp\espressif'
$env:IDF_GITHUB_ASSETS = 'dl.espressif.cn/github_assets'
$idf = 'D:\esp\esp-idf-v5.5.3'

"IDF_PATH          = $idf"
"IDF_TOOLS_PATH    = $env:IDF_TOOLS_PATH"
"IDF_GITHUB_ASSETS = $env:IDF_GITHUB_ASSETS"
"python            = " + (Get-Command python).Source

$sw = [Diagnostics.Stopwatch]::StartNew()
& "$idf\install.ps1" esp32c3
$rc = $LASTEXITCODE
$sw.Stop()
"INSTALL exit=$rc elapsed={0:N1} min" -f $sw.Elapsed.TotalMinutes

if (Test-Path $env:IDF_TOOLS_PATH) {
    $s = Get-ChildItem $env:IDF_TOOLS_PATH -Recurse -File -Force | Measure-Object -Property Length -Sum
    "tools dir: {0:N0} files {1:N0} MB" -f $s.Count, ($s.Sum / 1MB)
    Get-ChildItem $env:IDF_TOOLS_PATH -Directory | Select-Object -ExpandProperty Name
}