# 等待 AI passport（ESP32-C3, VID_303A）插上，然后自动全片备份 + 校验
$ErrorActionPreference = 'Continue'
$py   = '%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe'
$env:PYTHONPATH = 'D:\dsh-work\tools'
$out  = 'D:\dsh-work\flash\full-backup.bin'
$deadline = (Get-Date).AddMinutes(20)

function Get-EspPort {
    $ports = [System.IO.Ports.SerialPort]::GetPortNames()
    if (-not $ports) { return $null }
    foreach ($p in $ports) {
        $dev = Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -match [regex]::Escape("($p)") }
        if ($dev -and ($dev.InstanceId -like 'USB\VID_303A*')) { return $p }
    }
    # 绝不退回"第一个端口"。
    # 这个脚本接下来要做的是把 8 MB 镜像写进设备；如果按名字匹配不到就随手挑一个端口，
    # 插在旁边的任何一块板子、任何一个 USB 串口设备都可能被当成本机写进去。
    # 找不到就返回 $null，让调用方继续等——宁可等，不可猜。
    return $null
}

"$(Get-Date -Format 'HH:mm:ss') 开始等待设备插入（最长 20 分钟）..."
$port = $null
while ((Get-Date) -lt $deadline) {
    $port = Get-EspPort
    if ($port) {
        Start-Sleep -Seconds 2   # 给枚举/驱动一点时间
        "detected: COM port = $port at $(Get-Date -Format 'HH:mm:ss')"
        break
    }
    Start-Sleep -Milliseconds 1500
}
if (-not $port) { "TIMEOUT: 20 分钟内没有检测到设备，放弃。"; exit 1 }

"$(Get-Date -Format 'HH:mm:ss') 读取芯片信息..."
& $py -m esptool --port $port chip-id 2>&1 | Select-String -Pattern 'Chip type|Features|MAC|Detecting|error' | ForEach-Object { $_.ToString() }

"$(Get-Date -Format 'HH:mm:ss') 开始全片备份 8MB -> $out"
$sw = [Diagnostics.Stopwatch]::StartNew()
$log = & $py -m esptool --port $port --baud 460800 read-flash 0x0 0x800000 $out 2>&1
$sw.Stop()
($log | Select-Object -Last 6 | ForEach-Object { $_.ToString() }) -join "`n"
"backup elapsed: {0:N1}s" -f $sw.Elapsed.TotalSeconds

if (-not (Test-Path $out)) { "FAILED: 备份文件不存在。"; exit 2 }
$len = (Get-Item $out).Length
"backup size: $len bytes (期望 8388608)"
"MD5   : " + (Get-FileHash $out -Algorithm MD5).Hash
"SHA256: " + (Get-FileHash $out -Algorithm SHA256).Hash

"$(Get-Date -Format 'HH:mm:ss') 回读校验（逐字节比对）..."
& $py -m esptool --port $port --baud 460800 verify-flash 0x0 $out 2>&1 | Select-Object -Last 6 | ForEach-Object { $_.ToString() }
"$(Get-Date -Format 'HH:mm:ss') 备份流程结束。"