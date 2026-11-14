# Voice protocol v1 — Windows Agent 签认

| 项 | 值 |
|---|---|
| 审查对象 | `docs/voice-protocol-v1.md` DRAFT 1 @ `feat/passport-voice-ubuntu` `c56f0bd` |
| 审查方 | Windows Agent（Passport 固件 / ESP32-C3） |
| 签认载体 | `feat/passport-voice-windows`，对应固件提交 `65994fd` + 本轮提交 |
| 结论 | **同意冻结为 v1.0，附 3 条文档修正要求（不影响 wire format）** |
| 证据基础 | 真机 ESP32-C3 Passport，8 MB XMC flash，无 PSRAM，2.4 GHz only；非模拟器 |

所有数字都是真机实测。模拟器只用于构建验证，不作为任何一格的依据。

---

## 1. D1–D8 逐条结论

| 编号 | 结论 | 依据 / 备注 |
|---|---|---|
| D1 | **同意** | 300 ms pre-roll = 9,600 B，相对最大空闲块 106,496 B 无压力。设备在 `key` 按下瞬间起采并写入 pre-roll 环，收到 `capture.start` 先补发再转实时流。不需常开麦克风（与 §1 排除项一致）。 |
| D2 | **同意** | 固件 NVS 命名空间已是 `dshtc`，Kconfig 编译期默认 `PORT=8080` → 音频缺省 8081，与"旧固件向后兼容"相符。`audio_port` 为新增 NVS 键；`set_net` 缺省取 `port+1` 的规则可原样实现。 |
| D3 | **同意** | `device_id` 由 `esp_wifi_get_mac(WIFI_IF_STA)` 后 3 字节格式化为 6 位小写 hex，MAC 为出厂烧写、跨重刷稳定。密钥落独立命名空间 `pavauth`，任何应答与日志不回显。**补充一条部署事实**：`idf.py flash` 只写 app 分区，NVS 与密钥得以保留；但整片擦除（`erase_flash` / 恢复 8 MB 原始备份）会同时抹掉密钥，之后必须重新 `provision`。 |
| D4 | **同意** | 设备按连接持有 `session_id`；重连换 session 后 `turn_id` 从 1 重开无歧义。迟到帧按 `(session_id, turn_id)` 双条件丢弃，设备侧实现不额外解释。 |
| D5 | **同意** | 8 KB ≈ 250 ms 积压。设备上行发送为非阻塞 + 丢最旧帧 + 计数，`audio_overflow` 随 `capture.ended` 上报。**明确一点**：设备绝不为"不丢帧"而扩大缓冲或阻塞采集线程。 |
| D6 | **定为 20 ms / 640 B** | 见 §2 实测。20 ms 与 40 ms 两档真机均 0 失败，所以"ES8311 DMA 只能 40 ms"的退路不需要。建议把 §4「允许帧长 40 ms」标为**仅为将来兼容保留、v1 设备不会使用**，避免两端为"要不要声明在 hello 里"产生分歧。 |
| D7 | **同意（已实现并真机验证）** | 入站 512 B / 出站 2048 B；`scan` 现在带 `truncated:true` 且 `count` = 实际发出条目数。附带把错误码逐字对齐协议：`bad_json` / `unknown_cmd` / `line_too_long`（此前是 `bad json` / `unknown cmd` / `line too long`，带空格，属于 wire 不一致，已按 §6.5 改正）。 |
| D8 | **同意** | ESP32-C3 有 mbedTLS HMAC-SHA256（flash 约 +8–10 KB，分区余量 85% 无压力）；无 RTC 所以"协议里不出现时间戳"正好，用 `esp_random()` 取 16 字节 nonce。nonce 在 Wi-Fi 就绪后取，不依赖上电早期熵。 |

---

## 2. D6 的实测数据（本轮新增）

同一次构建、同一台设备，只改帧长：

| 帧长 | 帧数 | 帧字节 | 读失败 | 写失败 | 录音耗时（名义 2000 ms） | rate_ratio |
|---|---|---|---|---|---|---|
| 20 ms | 100 | 640 | **0** | **0** | 1992 ms | 99 |
| 40 ms | 50 | 1280 | **0** | **0** | 1992 ms | 99 |

两档总字节数都是 64,000，耗时完全一致，说明节拍由 codec 时钟决定、与帧长无关。
`rate_ratio` = 实际耗时 × 100 / 名义时长，两档均为 99，即 2 秒周期内快 0.4 %，
落在 §4「等效采样数必须是 320 的整数倍」允差内——因为固件每帧从 I2S 精确读 320 个采样，
不靠定时器凑数。

非法帧长（如 25 ms）返回 `ESP_ERR_INVALID_ARG`，不会静默降级。

---

## 3. 文档修正要求（3 条，均不改 wire format）

### 3.1 §8 内存账的可用堆数字偏大

原文："启动后可用堆 ~170 KB 时余量充足"。真机实测（Wi-Fi + LVGL + UI 全部起来、运行 21 s）：

```json
"heap":118624,"heap_min":113236,"heap_block":106496
```

即**空闲 118,624 B、最大连续块 106,496 B**。§8 的 55 KB 预算仍然成立（最大单块 38,400 B ≪ 106,496 B），
但余量是 ~63 KB 而非 ~115 KB。请把该行改成"实测空闲 ≈ 116 KB / 最大连续块 ≈ 104 KB"，
并把"须实测高水位"保留为硬要求。这个数字也已经做进固件 `get_status` 应答（`heap`/`heap_min`/`heap_block`），
联调时服务端可直接读，不必靠估算。

### 3.2 §2 / §7.2 漏了 `scan` 与 `set_net` 会终止整个会话

`scan` 必须让 WiFi 驱动主动 disconnect 才能扫（否则 `esp_wifi_scan_start` 直接返回
`ESP_ERR_WIFI_STATE`），`set_net` 要重建 STA。两者的后果是**两条 TCP 通道同时断、当前
`session_id` 作废**，不只是"禁止开新语音轮"。§7.2 只写了后者，容易让服务端以为控制通道还能用。

要求补写：`scan` / `set_net` 执行即视为会话终止；设备重连后重新 `hello`/`auth` 拿新
`session_id`；服务端必须容忍"正在播放时对端消失"并按 `playback.ended(cancelled)` 收尾。

### 3.3 `ready` 事件与 `hello` 的先后未定义

§6.4 说每条载体各自发一次 `ready`，§3.2 说设备连上先 `hello`。当前固件连上后立刻发
`{"event":"ready","link":"net"}`。若服务端在 auth 前收到 `ready` 会按 §3.2 判 `unauthorized`
甚至累计 3 次关连接。要求明确其中之一：

- 建议：**`ready` 在 `auth` 成功之后才发**，且 `link` 取值只用于 UI 提示；
- 或：`ready` 是唯一允许在 auth 之前出现的事件。

我按前者实现，除非处暑反对。

---

## 4. 固件现状（必须如实说明）

**目前固件里没有任何 PAV1 帧实现。** 已具备的是：控制通道 JSONL（含 W0 修复）、
Wi-Fi STA + 主动 TCP、以及 codec 自测（`audio_selftest`：录 N ms 再回放，用来验证采集/播放/帧时序）。

因此：

- `mock_device.py` 验证的是 Ubuntu 侧状态机，**不是**我的固件；
- 真机联调现在无法开始，缺的是固件侧音频通道，不是环境。

Windows 侧后续顺序（每步真机验证后才报，不合并声明）：

| 步骤 | 内容 | 解锁什么 |
|---|---|---|
| W2-1 | 请求来源定向 + 事件广播（协议 §6.2 / W0-4） | `provision` 能判断"是否来自 USB" |
| W2-2 | 控制通道 `hello`/`auth`/`provision` + `session_id` | 服务端可与真机握手 |
| W2-3 | PAV1 framer（16 B 头，坏头即关通道，单帧 ≤ 4096） | 音频通道可建立 |
| W2-4 | 上行采集 + pre-roll 环 + `capture.start/stop` | 第一段真实语音可上传 |
| W2-5 | 下行播放环（1200 ms）+ `playback.start/stop` + 打断 | 第一次听到 TTS |
| W2-6 | C 侧读 `protocol/v1/vectors.json` 的向量测试 | 两端行为逐字节对齐 |

W2-1 必须先做：没有来源信息，`provision` 的"仅 USB"约束无法执行，`id` 撞车也无法避免。

---

## 5. 需要 Ubuntu 侧提供的一件事

设备当前指向 `192.168.2.108:18080`，那是本机占位地址（Windows 防火墙挡住，无法在非管理员下放行）。
真实 Ubuntu 服务起来后，给我 **host + control_port + audio_port**，我用 USB `set_net` 下发，
不需要重刷固件。这一条不阻塞 W2-1～W2-3（可在模拟器与自环上验证），只阻塞真机端到端。

---

## 6. 签认

D1–D8 全部同意；§3 的 3 条为文档修正要求，不改任何字节格式。
处暑确认 §3 后，本文件即视为 Windows 侧签认完成，`docs/voice-protocol-v1.md` 可改为 `FROZEN v1.0`。

Windows 侧承诺：在 v1 冻结后遵守 §13 流程——任何 wire 变更先改文档与向量、升版本、两端同步，
不出现"固件先改、文档后补"。