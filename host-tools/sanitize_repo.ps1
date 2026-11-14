# Redact device identifiers and local user paths in the repo tree (ASCII-only rules)
$repo = 'D:\dsh-work\repo\ai-passport-hardware'
$enc  = New-Object System.Text.UTF8Encoding($false)
$files = Get-ChildItem $repo -Recurse -File | Where-Object { $_.Extension -in '.md', '.ps1', '.py', '.txt' }
foreach ($f in $files) {
    $orig = [IO.File]::ReadAllText($f.FullName)
    $t = $orig
    $t = $t -replace '98:C3:77:F5:30:38', '98:C3:77:XX:XX:XX'
    $t = $t -replace '98c377f53038', 'XXxxXXxxXXxx'
    $t = $t -replace 'TUy98c377f53038', 'TUy<REDACTED>'
    $t = $t -replace 'C:\\Users\\[^\\]+\\AppData\\Local\\hermes', '%LOCALAPPDATA%\hermes'
    $t = $t -replace 'C:\\Users\\[^\\]+\\AppData\\Roaming\\uv', '%APPDATA%\uv'
    $t = $t -replace 'C:/Users/[^/]+/AppData/Local/hermes', '%LOCALAPPDATA%/hermes'
    if ($t -ne $orig) {
        [IO.File]::WriteAllText($f.FullName, $t, $enc)
        'patched: ' + $f.Name
    }
}
'--- verification ---'
$bad = Select-String -Path (Join-Path $repo '*\*') -Pattern 'TUy98c|98c377f53038|98:C3:77:F5:30:38|AppData\\\\Local\\\\hermes|Users\\.*.AppData' -ErrorAction SilentlyContinue
if ($bad) { $bad | ForEach-Object { $_.Filename + ':' + $_.LineNumber } } else { 'clean' }