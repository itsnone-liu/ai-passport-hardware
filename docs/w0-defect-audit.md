# W0 十一条缺陷复核 + 一个更严重的实测缺陷（2026-11-14 第二轮）

方案 `ai_passport_xiaozhi_reuse_dual_agent_plan.md` v2.0 的 W0 表是对着 `main@913f3ab` 写的。
本轮逐条对着当前分支 `feat/passport-voice-windows` 复核，结论：**九条已经修过，两条是真缺陷**。
另外在验证过程中发现一个 W0 表里没有、但优先级高于全部十一条的缺陷（§3）。

## 1. 十一条复核

| 编号 | 方案描述 | 现状 | 证据 |
|---|---|---|---|
| W0-1 | `scan` 回复 1024 B 撑爆 512 B 的 `send_fmt` | **早已修** | `terminal_link.c:32` `TX_MAX 2048`，注释即写明原因 |
| W0-2 | `terminal_net_scan()` `calloc` 失败提前 return，`s_scanning` 不复位 | **早已修** | `terminal_net.c:638-642` 单一出口 `done:`，`free` + `s_scanning=false` + `esp_wifi_connect` |
| W0-3 | SSID 用 `%s` 拼 JSON、`snprintf` 超长风险 | **早已修** | `terminal_net.c:526` `json_escape()`；`:631-636` 用 `str_append` 累加并带 `truncated` |
| W0-4 | 回复广播到所有 sink，不对源定向 | **早已修** | `terminal_link.c:114` `reply` 带 `src`，只回请求来源 |
| W0-5 | `set_net` 先断链路再回执，发起方永远收不到结果 | **早已修** | `terminal_link.c:283-299` 先落参数、先回执，`terminal_net_reconnect()` 在回执之后 |
| W0-6 | `show_text` 锁失败仍回 `ok:true` | **早已修** | `terminal_link.c:266-270` 失败回 `err:"display busy"` |
| W0-7 | 超长行丢前缀、把尾巴当新命令 | **早已修** | `terminal_net.c:189-212` `s_rx_discard` 整行丢弃到换行为止，并回 `line_too_long` |
| W0-8 | `s_sock`/`s_connected` 跨任务并发无保护 | **部分** | `terminal_link.c:44` 只有 `s_tx_mu`（发送互斥）；连接状态本身仍是裸变量，未做完整审计 |
| W0-9 | `scan` 在语音进行中断开 Wi-Fi，破坏活跃轮次 | **本轮修** | 见 §2 |
| W0-10 | 控制 TCP 无认证 | **早已修** | `terminal_auth.c` + `terminal_link.c:255` 未认证业务命令回 `unauthorized`，累计 3 次关连接 |
| W0-11 | `wait_and_backup.ps1` 匹配不到就退回第一个 COM 口 | **本轮修** | `scripts/wait_and_backup.ps1:15-19` 改为返回 `$null`、继续等 |

W0-8 的判定保持"部分"：`s_tx_mu` 覆盖了发送，但 `s_connected`/`s_sock` 的读写在 link 任务、事件回调、
reconnect 路径之间仍无锁。这一条没有实测触发的证据，不做无依据的重构。

## 2. W0-9 的修法与验收

扫描必须先 `esp_wifi_disconnect()`，所以语音进行中扫描等于当场断流。旧代码没有拒绝路径。

改法：`terminal_capture_busy()`（armed 或 capturing）、`terminal_playback_busy()`（非 idle），
`terminal_net_scan()` 在断开前检查，命中返回 `ESP_ERR_INVALID_STATE`；命令层映射成明确的
`{"ok":false,"cmd":"scan","err":"busy"}`，而不是让调用方看到一个像故障的通用错误。

验收（真机）：

```
capture.start -> ok=True
scan（语音中）-> ok=False err=busy     ← 判据通过
采集期间 session 未变=True             ← 没把 Wi-Fi 断掉
capture.stop -> ok=True
```

## 3. 新发现：上行死掉时，设备会被自己的发送队列拖死

这一条 W0 表里没有，但它是"真人说话为什么没成"的直接原因，优先于其余全部条目。

### 现象（实测）

对一个**不读音频**的对端起采，`capture.start` 之后 1 秒内：

| 时刻 | heap | heap_min | block | 说明 |
|---|---|---|---|---|
| 空闲 | 28,708 | 20,748 | 18,432 | 正常 |
| 起采 +1 s | **1,864** | 1,012 | **1,152** | 57 帧（≈37 KB）进入发送路径，对端一个 ACK 都没回 |
| 收轮后 +7 s | 1,160 | 296 | 896 | **内存没有回来**，音频通道再也连不上（`audio_ready=False`） |

`rx/rms/peak` 同时给出旁证：`peak=32768`、`rms≈13000`，麦克风数据全程满量程。

### 机制

1. 上行 33 KB/s 灌进 16 KB 发送缓冲，对端不读 → send 阻塞（旧值 5 秒/帧）；
2. lwIP 为未确认数据保留 pbuf + 重传队列，堆在 1 秒内被吃光，最大连续块从 18 KB 掉到 1.1 KB；
3. 采集任务此时阻塞在 send 里，`s_req_stop`、丢帧计数、超时检查**一个都跑不到**，`capture.stop`
   表面回 `ok` 但轮次不结束；
4. 即使轮次结束，冷关闭的 socket 把 pcb 连同未确认队列挂在 FIN_WAIT，内存不返还。

### 本轮已做的三处改动

| 改动 | 位置 | 作用 |
|---|---|---|
| 音频 socket 发送超时 5 s → **250 ms** | `terminal_pav1.c` | 一帧 20 ms，250 ms 塞不进协议栈就是链路死了；短超时让采集任务能回到 `s_req_stop` 的判断 |
| 上行看门狗（2 s 墙钟无进展 → 收轮） | `terminal_pav1.c` `serve()` | 采集任务可能自己阻塞，看门狗必须放在**至少每秒醒一次**的任务里；判据用"已发帧数是否还在涨"，不用丢帧计数 |
| 上行死掉时用 **RST** 关闭音频通道 | `terminal_pav1.c` `close_sock()` | `SO_LINGER{1,0}` 让 close 直接发 RST，音频是"过期即废"的数据，不值得保序重传 |

另加 `terminal_capture_force_stop()`：允许从别的任务强制收轮——采集任务自己卡住时，只有还活着的那个人能救场。

### 还没修好的部分（不要当成已解决）

RST 之后堆**仍然停在 1.1–1.9 KB 不回升**，音频通道重连失败。上面三条把"拖死"变成了"能收轮、
能报 `uplink_stall`"，但**没有做到"收轮后恢复"**。下一步要查的是那 24 KB 到底归谁：
候选是 lwIP pcb 残留、`esp_codec_dev` 的 DMA 缓冲、或 Wi-Fi TX 缓冲；判据是真机上
`heap_caps` 分区统计 + 关闭音频通道前后对比。

在这条修好之前，**设备一旦遇到"对端不读"就必须重启**。给 处暑 的联调约定：音频通道没在消费时
不要起采；起采后 2 s 内没有消费到数据，设备会主动报 `uplink_stall` 并断音频通道。

## 4. 复现命令

```powershell
# 起采 + 期间 scan（应 busy）+ 收轮
$env:PYTHONPATH='D:\dsh-work\tools'; python D:\dsh-work\w09_scan_busy.py
# 按秒追踪堆：空闲 → 起采 → 收轮（看 §3 那张表在自己手上重现）
$env:PYTHONPATH='D:\dsh-work\tools'; python D:\dsh-work\heap_trace.py
# 设备是否还活着（连发 status/audio/stop/audio，打印原始字节）
$env:PYTHONPATH='D:\dsh-work\tools'; python D:\dsh-work\is_alive.py
```

主机脚本必须走 `sitecustomize.py`（置 `PYTHONPATH`），否则 pyserial 的 DTR 会把设备重启，
测出来的 `boot` 每次都在变，所有结论作废。