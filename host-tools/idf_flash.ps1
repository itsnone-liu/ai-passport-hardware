# Flash the self-built dsh terminal firmware to the attached Passport
$ErrorActionPreference = 'Continue'
$env:IDF_TOOLS_PATH = 'D:\esp\espressif'
$env:TEMP = 'D:\esp\tmp'
$env:TMP  = 'D:\esp\tmp'
. 'D:\esp\esp-idf-v5.5.3\export.ps1' | Out-Null
Set-Location 'D:\esp\ai-passport'
& idf.py -p COM3 flash *>&1 | Select-Object -Last 25
"flash exit=$LASTEXITCODE"