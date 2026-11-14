# 按 ESP-IDF v5.5.3 里 pin 的 commit 拉取各子模块源码（HTTP zip，非 git）
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = 'D:\esp'; $dl = "$root\dl"; $target = "$root\esp-idf-v5.5.3"; $tmp = "$root\_tmp"
New-Item -ItemType Directory -Force -Path $dl, $tmp, $target | Out-Null
function Log($m) { "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m }

$subs = @(
    @{ p = 'components/bootloader/subproject/components/micro-ecc/micro-ecc'; r = 'kmackay/micro-ecc' },
    @{ p = 'components/spiffs/spiffs';                    r = 'pellepl/spiffs' },
    @{ p = 'components/json/cJSON';                       r = 'DaveGamble/cJSON' },
    @{ p = 'components/mbedtls/mbedtls';                  r = 'espressif/mbedtls' },
    @{ p = 'components/lwip/lwip';                        r = 'espressif/esp-lwip' },
    @{ p = 'components/mqtt/esp-mqtt';                    r = 'espressif/esp-mqtt' },
    @{ p = 'components/protobuf-c/protobuf-c';            r = 'protobuf-c/protobuf-c' },
    @{ p = 'components/unity/unity';                      r = 'ThrowTheSwitch/Unity' },
    @{ p = 'components/bt/host/nimble/nimble';            r = 'espressif/esp-nimble' },
    @{ p = 'components/esp_wifi/lib';                     r = 'espressif/esp32-wifi-lib' },
    @{ p = 'components/bt/controller/lib_esp32c3_family'; r = 'espressif/esp32c3-bt-lib' },
    @{ p = 'components/bt/controller/lib_esp32';          r = 'espressif/esp32-bt-lib' },
    @{ p = 'components/esp_phy/lib';                      r = 'espressif/esp-phy-lib' },
    @{ p = 'components/esp_coex/lib';                     r = 'espressif/esp-coex-lib' },
    @{ p = 'components/heap/tlsf';                        r = 'espressif/tlsf' }
)

# ---- 1. 取 pin SHA ----
$queue = @()
foreach ($s in $subs) {
    $parent = (Split-Path $s.p -Parent) -replace '\\', '/'
    $leaf   = Split-Path $s.p -Leaf
    $sha = $null
    for ($try = 1; $try -le 3 -and -not $sha; $try++) {
        try {
            $resp = Invoke-RestMethod -Uri "https://api.github.com/repos/espressif/esp-idf/contents/$parent`?ref=v5.5.3" `
                        -Headers @{ 'User-Agent' = 'ps'; 'Accept' = 'application/vnd.github+json' } -TimeoutSec 40
            $e = $resp | Where-Object { $_.name -eq $leaf } | Select-Object -First 1
            if ($e -and $e.sha) { $sha = $e.sha }
        } catch { Start-Sleep -Seconds 3 }
    }
    if (-not $sha) { Log ("!! NO PIN: " + $s.p); continue }
    $zip = "$dl\" + ($s.r -replace '/', '_') + "_" + $sha.Substring(0, 8) + ".zip"
    $queue += @{ p = $s.p; r = $s.r; sha = $sha; zip = $zip }
    Log ("pin {0,-28} {1} -> {2}" -f $s.p, $sha.Substring(0, 8), (Split-Path $zip -Leaf))
}

# ---- 2. 并发下载 ----
$batch = 5
for ($i = 0; $i -lt $queue.Count; $i += $batch) {
    $end = [math]::Min($i + $batch - 1, $queue.Count - 1)
    $chunk = $queue[$i..$end]
    Log ("batch: " + (($chunk | ForEach-Object { $_.r }) -join ', '))
    $jobs = @()
    foreach ($q in $chunk) {
        if ((Test-Path $q.zip) -and (Get-Item $q.zip).Length -gt 1024) { continue }
        $jobs += Start-Job -ArgumentList "https://codeload.github.com/$($q.r)/zip/$($q.sha)", $q.zip -ScriptBlock {
            param($u, $o)
            & curl.exe -L --retry 10 --retry-delay 4 --retry-all-errors --connect-timeout 25 `
                       --speed-limit 1024 --speed-time 90 -o $o $u 2>&1 | Out-Null
            $len = 0; if (Test-Path $o) { $len = (Get-Item $o).Length }
            $len
        }
    }
    $jobs | Wait-Job | Out-Null
    foreach ($j in $jobs) { Log ("  got {0:N1} MB" -f ((Receive-Job $j | Select-Object -Last 1) / 1MB)); Remove-Job $j -Force }
}

# ---- 3. 校验 + 展开 ----
function Test-Zip($path) {
    try { $z = [System.IO.Compression.ZipFile]::OpenRead($path); $n = $z.Entries.Count; $z.Dispose(); $n }
    catch { -1 }
}
function Expand-To($zip, $dest) {
    $stage = Join-Path $tmp ([guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $stage -Force
    $inner = Get-ChildItem $stage -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    if ($inner) { Copy-Item (Join-Path $inner.FullName '*') $dest -Recurse -Force }
    else        { Copy-Item (Join-Path $stage '*') $dest -Recurse -Force }
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
foreach ($q in $queue) {
    $n = Test-Zip $q.zip
    if ($n -lt 0) { Log ("BAD ZIP: " + (Split-Path $q.zip -Leaf)); continue }
    $dest = Join-Path $target ($q.p -replace '/', '\')
    Expand-To $q.zip $dest
    Log ("  extracted {0}  files={1}" -f $q.p, (Get-ChildItem $dest -Recurse -File | Measure-Object).Count)
}
Log '=== SUBS DONE ==='
$st = Get-ChildItem $target -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum
'target now: {0:N0} files {1:N0} MB' -f $st.Count, ($st.Sum / 1MB)