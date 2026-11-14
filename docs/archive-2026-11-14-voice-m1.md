# 存档点：语音终端 M1 阶段（2026-11-14 22:20）

> 这份文档是一个**可以独立读懂的存档**：把"做了什么、量到多少、为什么这么改、还剩什么"全部落成文字。
> 下次接手（人或 agent）从这一篇就能重建上下文，不需要翻对话记录。
> 需要现场数据时，每一节都写了对应的复现命令。

---

## 1. 目标（不变）

把 FoloToy AI Passport 做成 **dsh（Ubuntu 服务器）的瘦终端**：

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0.5 | 刷官方小智固件，验证硬件正常 | ✅ 已完成 |
| M1 | 自研固件 + USB 控制通道 | ✅ 已完成（真机验证） |
| M3 | 设备主动外连的网络通道 | ✅ 已完成（真机验证） |
| **语音闭环** | 采集 → ASR → dsh → TTS → 播放 | ⬜ **只剩"真人说话被 ASR 认出"这一步** |

**分工**：Windows 固件侧 = 我（代号"小白马"）；Ubuntu 语音/dsh 侧 = 处暑（另一个 dsh 实例）。用户转达消息。

**铁律**（写在最前面，因为踩过）：
- **只走 USB 刷机**；8 MB 原厂备份永不覆盖，始终可回滚。
- 设备**永远只对外拨号**，从不监听入站。
- 不把模拟器的成功当硬件成功；不在没有真人开口时声称 ASR 成功。

---

## 2. 硬件事实（实测，非手册抄录）

| 项 | 值 |
|---|---|
| SoC | ESP32-C3 QFN32 v1.1，**无 PSRAM**，仅 2.4 GHz |
| Flash | 8 MB XMC，`0x000000` 起 |
| 屏幕 | ST7789 240×320 |
| 音频 | ES8311 全双工 codec，单 I2S |
| 电池 | CW2017 电量计 |
| 按键 | 3 键共用**一个 ADC 引脚**，靠分压电阻区分；一次只能识别一个键 |
| 串口 | USB-Serial/JTAG，COM3 |

**原厂 8 MB 备份（冻结，永不覆盖）**

```
D:\dsh-work\flash\full-backup.bin
SHA256 E1993D959ACFF4FC05182B2A3C02FA93E0E51C99B050431AB9B72008D4C1AC9
```

**网络现场**

```
设备 STA   192.168.2.107   SSID 2005   gw 192.168.2.1
Windows 主机 192.168.2.108
服务器     154.64.231.1  控制 18080 / 音频 18081
           asr=bailian-realtime  tts=bailian-sse  brain=mock
```

---

## 3. 架构：为什么是"瘦终端"

设备不做 ASR、不做 LLM、不做 TTS。设备只做四件事：**采集、播放、画图、上报**。
所有智能在服务器，所以换模型、换大脑、换语言都不需要动固件。

控制通道用**行分隔 JSON**，传输无关（`terminal_link.c` 有 2 个 sink 槽：USB、TCP）。
换成 WebSocket 或 BLE 只需加一个 sink，命令层不动。

**为什么 TCP 不用 WebSocket**：这条链路上只有两个已知端点、没有浏览器、没有代理，
WebSocket 的握手、掩码、分帧全是纯开销；而 20 ms 一帧的音频在 TCP 上就是字节流。

### PAV1 协议 v1（已冻结，见 `docs/voice-protocol-v1.md`）

- **控制面** `control_port 18080`：JSONL
- **音频面** `audio_port 18081`：16 字节头 + ≤4096 字节净荷

```
magic[0..3] = "PAV1"      kind[4]  1=PCM 2=HELLO 3=END 4=AUTH
flags[5]    reserved[6..7]  turn_id[8..11] uint32 LE
payload_len[12..15] uint32 LE (≤4096)
```

音频格式 `pcm_s16le / 16000 / 单声道`，**20 ms = 640 字节**。
**设备是上行时钟源，服务器是下行时钟源**（服务器必须按 31.2 KB/s = 50 帧/秒发）。

---

## 4. 固件模块与关键常量

| 文件 | 职责 | 关键常量 |
|---|---|---|
| `terminal_main.c` | 启动、按键分发、任务编排 | 长按阈值 500 ms（BSP） |
| `terminal_link.c` | 控制通道命令解析与路由 | 命令表见 §5 |
| `terminal_pav1.c` | 音频通道、帧编解码、收发 | `RX_BUF 4096` |
| `terminal_capture.c` | 麦克风采集、pre-roll、上行 | `FRAME 640`、`PREROLL 15`(300 ms)、`MAX_TURN 30 s`、`DROP_LIMIT 25`、`ARM_TIMEOUT 1500 ms` |
| `terminal_playback.c` | 环形缓冲、下行状态机、I2S 输出 | `RING_FRAMES 60`(1.2 s，静态 38,400 B)、`PREBUF 15`、`STALL 2000 ms`、`UNDERRUN 500 ms` |
| `terminal_audio.c` | codec 初始化、音量、自检 | — |
| `terminal_net.c` | Wi-Fi、连接、RTT/吞吐/PS 测试 | 重连退避 2.4→30 s |
| `terminal_boot.c` | **复位取证**（RTC 内存） | magic `0x50415631` |
| `terminal_ui.c` | LVGL 界面 | — |

### 复位取证（这一版新增，值得单独说）

RTC_NOINIT 内存能活过软复位、WDT、panic（只有彻底断电会丢），所以把三个事实存在那儿：

```
boot=7  reset=usb(code 11)  prev_heap_min=19040  prev_heap_block=18432  prev_uptime=567s
```

- `boot`：启动计数
- `reset`：`poweron/ext_pin/software/panic/int_wdt/task_wdt/brownout/usb/...`
- `prev_heap_min/block/uptime`：**上次运行最后 2 秒**的采样

**为什么重要**：两个 agent 曾在"设备是不是自己重启"上争执，双方都没有证据。
现在这是一次字段比对，不是一场辩论。`reset_code=11` 就是"从 USB 侧复位"= 刷机。

**取证工具本身的验证**：`vendor.crash` 会故意 `abort()`，实测 `boot 3→4`、`reset=panic(code 4)`、
并保留崩溃前 `prev_heap_min=22304`。**只在正常关机上验过的取证工具不算证据。**

---

## 5. 控制通道命令表

| 命令 | 方向 | 说明 |
|---|---|---|
| `get_status` | →设备 | 堆、认证、会话、boot/reset、音频描述串 |
| `get_audio` | →设备 | 采集/播放全量计数器（口径见 §7） |
| `show_text` | →设备 | 屏幕文字 |
| `set_volume` / `set_brightness` | →设备 | |
| `set_net` / `get_net` / `scan` / `provision` | →设备 | 配网；`provision` 仅 USB |
| `capture.start` / `capture.stop` | →设备 | 起停一轮上行 |
| `playback.start` / `playback.stop` | →设备 | 起停一轮下行 |
| `state.set` | →设备 | 屏幕状态 |
| `ping` | 双向 | |
| `vendor.audio_selftest` / `vendor.play_tone` | →设备 | 1 kHz 正弦，喇叭自检 |
| `vendor.i2s_pace` / `vendor.playback_selftest` | →设备 | 播放时钟自检 |
| `vendor.uplink_test` | →设备 | 上行连通与吞吐 |
| `vendor.tcp_sink` / `vendor.tcp_accept` | →设备 | 出站洪泛 / 入站监听（避开 Windows 防火墙） |
| `vendor.rtt` | →设备 | **同步回执带统计**，`timeout_ms` 默认 3000 |
| `vendor.rx_rate` | →设备 | **同步回执带统计**，量下行帧率 |
| `vendor.wifi_ps` | →设备 | 0=关省电 1=MIN_MODEM 2=MAX_MODEM |
| `vendor.capture_arm` | →设备 | 等价于点一下 OK（无人值守验证 pre-roll） |
| `vendor.audio_reconnect` | →设备 | 立即重连音频通道 |
| `vendor.crash` | →设备 | **故意 panic**，验证复位取证 |

---

## 6. 实测数据（全部是数字，不是印象）

### 6.1 链路

| 项 | 结果 |
|---|---|
| 局域网接收 | **133–221 KB/s** @16 KB 窗口 |
| 服务器 RTT（设备侧） | min **153** / avg **330** / max **1157** ms，0/6 SYN 丢失 |
| 国内锚点 223.5.5.5 | **17 / 18 / 21 ms**（空闲与推流时一致） |
| 国际锚点 8.8.8.8 | 183 / 386 / **1356** ms |
| 1.1.1.1:443 | **SYN 被过滤**，永远连不上（因此 `vendor.rtt` 必须带超时） |

**结论（处暑与我共同确认的模型）**：服务器侧干净（到锚点 mdev ≤1 ms），接入线干净（国内 17 ms），
**抖在国际段**。这一段两边都控制不了。在这约束下，"0.12 s 领先 + 1.2 s 环形缓冲"是可播完的最小组合。

我在这条上先后错了两回，都记在 §9。

### 6.2 语音管道（处暑侧实测）

| 项 | 结果 |
|---|---|
| 下行帧率 | **49.5 fps = 99 帧 / 2 s，零丢失**（31.2 KB/s 要求命中） |
| 三轮连播（0.12 s 领先） | 114/114、118/118 **零欠载零拒绝**，`boot` 全程不变 |
| 领先 0.9 s | 卡在 `buffering`（环里 14 帧，差 1 帧到预缓冲） |
| 领先 2 s | `playback_stall` → idle（TCP 大领先反而更糟） |
| 领先 0.45 s | 能播，环峰值 32，第 66/110 帧饿死 → `audio_underrun` |
| 上行采集 | 四轮通过，含 pre-roll 0/300 ms |

### 6.3 内存（关键约束）

| 配置 | 空闲堆 | 历史最低 | 最大块 |
|---|---|---|---|
| 16 KB 窗口，无会话 | 40,892 | 33,928 | 30,720 |
| **16 KB 窗口，有会话** | **28,564** | **19,040** | 18,432 |
| 32 KB 窗口，有会话 | 32,656 | 25,896 | 22,528 |
| **32 KB 窗口 + 真人说话采集** | **5,324** | **1,000** | — **崩溃** |

最后一行是这次事故：堆耗尽 → 丢 25 帧撞 `DROP_LIMIT` → 放弃本轮 → 会话掉线（`auth=none`）。
**已回滚到 16 KB 并刷机验证**（`CONFIG_LWIP_TCP_WND_DEFAULT 16384` 已确认编译进 `build/config/sdkconfig.h`）。

> 注意：38,400 B 的播放环是**静态 `.bss`**，已经算在上面这些堆数字之外，不和 TCP 窗口抢。
> `CONFIG_LWIP_TCP_OOSEQ_MAX_PBUFS` 卡在 4 改不动（Kconfig 会重生成），不再纠缠。

---

## 7. 计数器口径（踩过一次坑才写下来）

`get_audio` 混了两种口径，曾让处暑把**正确实现判成 bug**（`played +0` 而 `rx +114` 看着像丢帧）。

| 字段 | 口径 | 归零点 |
|---|---|---|
| `audio_rx_frames` / `audio_rx_end` / `audio_tx_frames` | 自启动累计 | 重启 |
| `push_ok` / `rej_idle` / `rej_turn` / `rej_size` / `play_mismatch` | 自启动累计 | 重启 |
| `played_total` | 自启动累计 | 重启 |
| `played` / `play_overflows` / `play_underruns` | **按轮** | `playback.start` |
| `frames` / `dropped` / `rms` / `peak`（采集） | **按轮** | `capture.start` |
| `turns` / `read_fail` / `overflows`（采集） | 自启动累计 | 重启 |

**守恒式只能用同一个轴**：

```
push_ok + rej_idle + rej_turn + rej_size  ==  audio_rx_frames 的增量
played_total 的增量                        ==  真正进 I2S 的帧数
```

跨轴相减必然"失败"，而失败的样子像设备 bug，其实是校验写错了。

---

## 8. 当前未决

1. **🔴 按键交互是错的（下一步要改）**
   现在是"点一下 OK = 启动录音预处理"，而人自然会"**按住**说话"。而按住超过 500 ms 被判为长按 →
   **调亮度**。所以"按住说完整一句话"这一整套操作，设备一次都不会录音。
   改法：`BSP_BTN_PRESS` 起采 → `BSP_BTN_RELEASE` 结束；长按不再调亮度（移到双击）。

2. **真人口述 ASR 尚未成功过一次**
   不是管道没搭好，是**人开口的那一刻设备没在录音**。见第 1 条。

3. **C 侧 `vectors.json` 帧解析器测试**（W2-6）未做。
   Python 侧向量已覆盖，C 侧还没有对照测试。

4. **`provision` 走已认证 TCP 会话时应返回什么错误码**——目前 `not_supported`，未与处暑定论。

5. **安全**：仓库按用户决定保持**公开**；但测试用 shared_secret 曾在对话里明文出现过，
   事后应轮换；GitHub PAT 也应吊销。

---

## 9. 踩过的坑（带因果，不是清单）

### 9.1 我自己的测试脚本在复位设备（最贵的一个）

`serial.Serial(PORT, BAUD)` 这种最自然的写法会拉 DTR，而 DTR 对 ESP32-C3 就是复位握手。
我有 **13 个脚本**这么写。于是：每跑一次测试、每抓一次日志，设备就重启一次。
后果是双向的——我这边读到"刚重启、Wi-Fi 还没连上"，误判成"服务器连不上"；
处暑那边看到"连接断开 + 新 hello"，累计了 **115 次 hello**，以为固件在崩溃。

**根因不是某个脚本写错，是"逐个改"这个思路本身**：漏掉那一个就会继续制造假故障。
所以改成系统级修复——`tools/sitecustomize.py` 在 Python 启动时包装 `serial.Serial.open()`，
统一在 open 前设 `dtr=False; rts=False`。验证：连开 4 次串口，`boot` 不变。

**教训**：当"同一类错误反复出现"时，不要去修第 N 个实例，去改产生实例的那一层。

### 9.2 我的两次错误归因（同一类错法）

- **BBR**：我把"丢包导致 cwnd 塌陷"当成默认解释，建议处暑开 BBR —— 人家早就开着。
- **bufferbloat**：我把"持续推流"当成"上行饱和"，猜 917 ms 是家里路由器队列填满。
  处暑实测推流时 RTT 18→17 ms，**一动不动**，假设当场被否掉。

两次错法一样：**先形成一个合理解释，再去找支持它的数据**。正确顺序是反过来：
先让数据决定解释。最终正确的模型（国际段抖动）完全是处暑的数据推出来的，不是我猜出来的。

### 9.3 PowerShell 5.1 会把 UTF-8 源码写成乱码

`Get-Content -Raw` 按 GBK 读 UTF-8，再写回，整份中文注释变乱码——肉眼看不出来。
在 `terminal_link.c`（95 处）和 `terminal_pav1.h`（47 处）各中过一次。

**规矩**：能用 `edit` 工具就别用 PowerShell 做字符串替换；必须用时只用
`[IO.File]::ReadAllText/WriteAllText` + `New-Object Text.UTF8Encoding($false)`。
`Add-Content` 追加代码行会丢分号（`terminal_boot.h` 因此编译失败过一次）。

### 9.4 差分法本身有缺陷

跨时间窗口差分计数器，只要窗口内发生过一次重启，整段算术就失真。
`rx_rate` 因此报出 520 fps（真实 49.5）。
**改法**：测量命令改成**同步回执自带统计**，并附 `boot` 字段。这样"读到数"和"数是哪次启动期间的"绑在一起。

### 9.5 其它

- Windows 防火墙拦入站到 `python.exe`（加规则需管理员）→ 反向测：让设备监听（`vendor.tcp_accept`）。
- `Start-Job` 随 pwsh 进程结束而死 → 用工具自带的后台任务。
- COM3 日志是 GBK 乱码 → 只 grep ASCII 安全词（`auth:` `pav1:` `cap:` `boot:`）。
- 刷新后尾随 `[exit code: 1]` 是噪声，看 `build exit=0` / `flash exit=0`。

---

## 10. 怎么复现

```powershell
# 构建（ESP-IDF 5.5.3，装在 D:）
powershell -File D:\dsh-work\idf_build3.ps1      # 看 "build exit=0"

# 刷机（只走 USB）
powershell -File D:\dsh-work\idf_flash.ps1       # 看 "flash exit=0"

# 关键：所有 Python 脚本必须带这个 PYTHONPATH，否则会复位设备
$env:PYTHONPATH='D:\dsh-work\tools'
$py='C:\Users\王艳\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe'

& $py D:\dsh-work\wait_ready.py 25    # 等认证与音频通道就绪
& $py D:\dsh-work\snap.py 4           # 一次取全 status + get_audio + 相关日志
& $py D:\dsh-work\path_check.py       # 三锚点 RTT
& $py D:\dsh-work\crash_selftest.py   # 验证 panic 能被正确记录
& $py D:\dsh-work\verify_noreset.py   # 验证连开串口不会复位
& $py D:\dsh-work\watch_key.py 120    # 监听按键事件与状态变化
```

**回滚**：`esptool write_flash 0x0 D:\dsh-work\flash\full-backup.bin`（8 MB，SHA256 见 §2）。

---

## 11. 设备当前状态（存档时刻）

```
boot=7  reset=...  auth=ok  audio_ready=True
heap=28564  heap_min=19040  block=18432  uptime=567s
采集/播放计数器全 0（空闲）
```

固件配置：**16 KB TCP 窗口**（已回滚）+ SACK + Wi-Fi 省电关闭。
仓库分支 `feat/passport-voice-windows`，存档标签见 `git tag`。