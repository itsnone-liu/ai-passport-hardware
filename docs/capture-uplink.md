# 采集上行：帧路径、堆预算与服务端契约

本文记录 M3 采集上行在 ESP32-C3 上的**实测**行为与由此得出的设计约束。
所有数字都来自本机实测（`get_status` / `capture.ended` 回执 / 串口日志），
不是估算；凡属推断会明确标注。

硬件前提：ESP32-C3 QFN32，400 KB SRAM，**无 PSRAM**，8 MB flash，
Wi-Fi 2.4 GHz，ES8311 全双工 codec，麦克风走 I2S。

---

## 1. 帧路径

```
I2S DMA ──(20 ms/640 B)──> 采集任务
                             │
                             ├─ accumulate()  立即计入 RMS/peak（与能否送出无关）
                             │
                             └─ pend_push()   写入 4 帧静态环（BSS，不占堆）
                                     │
                                     ├─ 环空出来 → send(MSG_DONTWAIT 语义)
                                     │               │
                                     │               ├─ 写通 → 出环
                                     │               ├─ EAGAIN → 留环，下一拍再试
                                     │               └─ 真错误 → 收轮(error)
                                     └─ 环满 → 丢帧 + dropped++
```

关键点：**采集任务永不阻塞在写上**。它只做 memcpy 和一次"尽力发送"，
所以任何对端行为都不会把采集任务、轮次状态或超时判定拖住。

### 为什么不是"同步写 + 阻塞发送"

早期版本由采集任务直接 `send()`（`SO_SNDTIMEO=250 ms`）。实测后果：

- 对端不读时，`send()` 每帧阻塞 250 ms，随后 `finish_turn()` 又在写上
  阻塞 **3.5 秒**，期间轮次状态卡在 `capturing`，新的 `capture.start` 被判 `busy`；
- 堆在 1 秒内从 28.2 KB 掉到 2.6 KB，收轮后不返还，控制通道再也发不出包。

### 三个反直觉的坑（都踩过）

| 做法 | 结果 |
|---|---|
| 只给 `send()` 打 `MSG_DONTWAIT` | **无效**：lwIP 不看这个标记，仍按 `SO_SNDTIMEO` 阻塞（实测 2 秒只送出 8 帧） |
| 把 socket 整体设 `O_NONBLOCK`，不动 `recv` 循环 | 阻塞 `recv` 变成死自旋，**IDLE 任务饿死 → `task_wdt` 报警** |
| 静态缓冲开大（8 帧环 + 4 KB 暂存） | 静态数组直接吃 RAM：空闲堆 28.3 KB → **19.1 KB** |

结论：socket 用 `O_NONBLOCK`，并且**每个 `recv` 的 EAGAIN 分支都要 `vTaskDelay(2~5 ms)` 退让**；
缓冲按需最小化（4 帧环 + 660 B 单帧暂存区）。

### 整帧原子性

`send()` 可能只写出去半帧就 `EAGAIN`。若下次重试从帧头重发，对端看到的是坏流。
因此 PCM 走一个**单帧暂存区**：半帧留在暂存区里继续写完，只有整帧写完才计一帧成功。

---

## 2. 堆预算（实测）

| 时刻 | internal free | largest block |
|---|---|---|
| 空闲（会话已建立） | 24.9 – 30.4 KB | 14,848 |
| 起轮瞬间（codec 打开后） | 27.6 – 28.5 KB | ~14,800 |
| 正常采集进行中（对端在读） | 14.5 – 21.1 KB | 7,680 |
| 收轮后 | 24.2 – 30.4 KB（全额返还） | 14,848 |
| 对端不读、旧版本 | **2.6 – 4.3 KB** | 1,280 |

两个容易被误判的点：

1. **打开 codec 不耗堆**，反而净释放（实测 `26620 → 32608`）。早期"开 codec 吃 28 KB"
   的判断是错的，指向了错误的方向。
2. **起轮的开销来自 TCP 发送路径**：每 640 B 在途数据约吃掉 1.7 KB 堆
   （发送缓冲按小块分配，本身很碎）。把 `SO_SNDBUF` 从 16 KB 压到 4 KB、
   并且**写不进去就不再灌**，才能把这笔开销封顶。

### 安全垫

`free < 4 KB` 且**连续 3 拍**（起轮后 300 ms 宽限）才收轮，`reason=heap_guard`。
判据必须用 `free` 而不是 `largest`：起轮瞬间 `largest` 会碎到 7.6 KB 而 `free`
还有 24 KB，用 `largest` 会把**所有**正常轮次误杀（实测过）。

自愈：控制通道连续 4 次重连失败且 `free < 6 KB` 时主动 `esp_restart()`。
依据是实测的"上行饿死 → 控制通道 5 s 连接超时 → 退避 4.8/9.6/19.2 s 永远连不上 →
堆停在 4.9 KB、最大块 1.5 KB"，此时除重启没有出路，主动重启比让用户拔电源快。

---

## 3. 服务端契约（联调必须遵守）

1. **音频连接是会话级的**：设备开机拨一次，之后所有轮次复用这一条连接。
   - 读取器**必须常驻**：每轮只切换"当前 `turn_id` 与落盘位置"，**永不停止 `recv`**。
   - 若确实要按轮拆，**必须 `close()` 掉 socket**，设备才会重新拨号。
     只停读不关连接，后续所有轮次的帧只能被设备丢弃（表现为"只有开机后第一轮能通"）。
2. **轮次归属以设备为准**：`turn_id` 由设备分配，在 `capture.started` / `capture.ended`
   里下发。服务端**不要**用自己的"我发起过的轮次表"决定是否读流——设备可以按键自起一轮。
3. **不要发 `capture.stop`**：松手由设备自己收轮；后来的 `capture.stop` 会得到
   `err:"turn_closed"`，当成功处理、不要重试。
4. `capture.started.pre_roll_ms`（约 60–300）是**真实的前置音频**，不要裁掉；
   每轮开头另有 120 ms 陈旧 DMA 已由设备静默丢弃。
5. **以 `capture.ended` 为结束权威**，`reason` 取值与处置：

| reason | 含义 | 服务端处置 |
|---|---|---|
| `released` | 正常松手收轮 | 跑 ASR |
| `uplink_stall` | 松手时丢帧数 > 送帧数，对端没在收 | **不要跑 ASR**，检查读取器 |
| `cap_start_timeout` | 按键后 3000 ms 没等到 `capture.start` | 忽略本轮 |
| `heap_guard` | 堆余量不足主动收轮 | 可重试 |
| `max_duration` | 超过单轮上限 | 可重试 |
| `error` | 上行错误 | 可重试，必要时重建音频通道 |

6. 可重试的瞬态错误码：`not_supported`（音频通道未就绪）、`busy`（拨号中/上轮未收尾）、
   `no_mem`（堆不足）。

---

## 4. 已验证样本

按住 OK 2 秒不说话后松手（对端在读）：

```
[0.25s] capture.start turn=1  pre-roll 80 ms  heap=27,592
[0.37s] frames=8    dropped=0
[0.88s] frames=34   dropped=0
[1.90s] frames=84   dropped=0
[2.08s] 松手 → capture.ended reason=released frames=94 dropped=0
[2.40s] heap=24,160 → [2.91s] heap=30,408  block=14,848  auth=ok  audio_ready=True
```

判据：`frames ≈ 秒数 × 50`（本轮 94/100，差额是每轮开头丢弃的 120 ms 陈旧 DMA）、
`dropped=0`、`reason=released`、堆全额返还、控制通道不掉。

对端不读时不构成"设备故障"：帧被丢、堆稳定在 27–30 KB、控制通道存活、
`capture.ended` 报 `uplink_stall`，设备不需要重启。