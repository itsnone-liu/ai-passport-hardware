# Build (incremental, no set-target)
$ErrorActionPreference = 'Continue'
$env:IDF_TOOLS_PATH = 'D:\esp\espressif'
$env:TEMP = 'D:\esp\tmp'
$env:TMP  = 'D:\esp\tmp'
. 'D:\esp\esp-idf-v5.5.3\export.ps1'
Set-Location 'D:\esp\ai-passport'
& idf.py build *>&1 | Tee-Object -FilePath 'D:\esp\build.log' -Append | Select-Object -Last 45
$rc = $LASTEXITCODE
"build exit=$rc"
"=== artifacts ==="
Get-ChildItem 'D:\esp\ai-passport\build' -Include '*.elf','*.bin','flasher_args.json' -File -ErrorAction SilentlyContinue | Select-Object Name, Length | Format-Table -AutoSize | Out-String -Width 60