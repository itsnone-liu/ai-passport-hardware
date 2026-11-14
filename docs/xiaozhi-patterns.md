# 上游小智（xiaozhi-esp32）音频架构：可借的经验与本机取舍

调研时间：2026-11-14。来源是上游 **main 分支源码原文**（链接见各条），
不是二手总结。凡属推断会写明"推断"；凡未验证会写明"未验证"。

本机前提：ESP32-C3 QFN32 / 400 KB SRAM / **无 PSRAM** / IDF 5.5.3 /
ES8311 全双工 / 暂无唤醒词。

---

## 0. 一句话

上游在**同一颗 C3** 上跑通了 Opus + WakeNet，用的是"**小帧长（60 ms）+ 低复杂度
（complexity 0）+ DTX/VBR + 分层任务 + 满了丢最旧 + 从不在采集路径上阻塞**"这套组合。
我们绝大多数坑与它的注释逐字对应，说明这不是风格差异，而是这台硬件的物理约束。

---

## 1. 三条与我们实测完全撞车的做法

### 1.1 绝不在采集路径上阻塞，队列满了丢**最旧**

> "Microphone audio is realtime, so **drop the oldest frame instead of blocking**.
> Blocking here would stall the audio engine task and **deadlock the whole input
> pipeline when the send queue stops being drained (e.g. network congestion or a
> failed UDP send)**."
>
> —— [`main/audio/audio_service.cc`](https://github.com/78/xiaozhi-esp32/blob/main/main/audio/audio_service.cc)

我们用自己的事故复现了同一结论（同步 `send()` → `finish_turn` 阻塞 3.5 秒 →
堆被未确认段吃光 → 控制通道死 → 必须重启），见 `docs/capture-uplink.md`。

**差别（要改）**：上游丢**最旧**，我们当前丢**最新**。丢最新会让环里积压旧帧、
延迟越拖越长；丢最旧能保证送出的一直是"最近的声音"。低延迟场景应改为丢最旧。

### 1.2 起录先丢 120 ms

上游 `audio_input_need_warmup_` → `vTaskDelay(120 ms)` 丢弃首批数据。
我们独立测出"每轮开头 120 ms 是陈旧 DMA 数据"并按此实现，**数值一致**。

### 1.3 采集 / 编码 / 输出必须是不同任务

上游任务与优先级（`audio_service.cc`）：`audio_input` **prio 8** → 编码队列（容量 2）
→ `opus_codec` **prio 2** → 发送队列 → 网络；另有 `audio_output` **prio 4**。

我们目前是"采集任务里采集 + 发送"一条龙。PCM 可以凑合，**一旦上 Opus 必须拆开**，
否则编码会把 20 ms 采集节拍顶掉。

---

## 2. 关键参数（上游原文，注意与我们假设的差异）

来源：[`main/audio/audio_service.h`](https://github.com/78/xiaozhi-esp32/blob/main/main/audio/audio_service.h)

| 参数 | 上游取值 | 我们的现状 | 影响 |
|---|---|---|---|
| 帧长 | `OPUS_FRAME_DURATION_MS = 60` | 20 ms（PCM DMA 节拍） | 包数 50/s → **16.7/s**，CPU/协议开销降 3 倍 |
| 编码器 | `esp_opus_enc`（Espressif esp_audio_codec） | — | 带 complexity/DTX/VBR/FEC 开关，内存比裸 libopus 可控 |
| complexity | **0** | — | 最省 CPU，C3 必需 |
| DTX | **true** | — | 静音期几乎不发包：省电、省空口 |
| VBR | true | — | 省码率 |
| FEC | false | — | 只在 UDP 场景有意义（见 §4） |
| bitrate | `ESP_OPUS_BITRATE_AUTO` | — | 由编码器按模式选 |
| 采样率 | 16 kHz 单声道 | 16 kHz 单声道 ✅ | 一致，无需重采样 |
| 编码队列 | 2 | — | 极小，配合"丢最旧" |
| 发送队列 | `2400 / 60 = 40` 包 | 4 帧（80 ms） | **缓冲时间容量差 30 倍** |
| 解码队列 | `1200 / 60 = 20` 包 | — | 下行同理 |
| codec 空闲关断 | `AUDIO_POWER_TIMEOUT_MS = 15000` | 待查 | 电池设备上很值钱 |

**最重要的一行是发送队列**：上游靠"60 ms 帧 + 40 包"把抗抖动做到 **2.4 秒**，
代价只有 40 × ≈180 B ≈ **7 KB**。同样 7 KB 用 PCM 只能存 220 ms。
**码率降下来，缓冲的"时间容量"就上去了** —— 这比"省带宽"更关键。

---

## 3. C3 上唤醒词可行（有证据，但仍要实测）

上游在非 S3/P4 平台走 `LiteAudioEngine`（无完整 AFE），其中：

- `CreateWakeWordLocked()` 使用**独立 WakeNet**（`EspWakeWord`，`esp_wn_models.h`）；
- 另有 `ReleaseWakeWordResources()` / `RestoreWakeWordResources()` 用于**动态放/取模型内存**。

来源：[`main/audio/engines/lite_audio_engine.cc`](https://github.com/78/xiaozhi-esp32/blob/main/main/audio/engines/lite_audio_engine.cc)

结论：**唤醒词在 C3 上是可行路线**（"需要靠动态释放挤内存"说明它很紧，但成立）。
这给"按键磨损"问题提供了真正的替代方案：唤醒词为主、按键兜底。

注意：LiteAudioEngine **不支持设备侧 AEC**（`EnableDeviceAec()` 直接返回警告），
所以做"实时打断/回声消除"需要 S3 级别算力，C3 上不做。

---

## 4. 传输层：上游有 UDP 音频通道

协议目录同时存在（[`main/protocols/`](https://github.com/78/xiaozhi-esp32/tree/main/main/protocols)）：

- `websocket_protocol.cc` — 音频与信令走同一条 WS/TCP 连接；
- `mqtt_protocol.cc` + **`udp_probe.h`** — 信令走 MQTT、**音频走 UDP**（含 UDP 探测）。

**为什么这条比 Opus 更根本**：TCP 有队头阻塞。900 ms 抖动下，一个卡住的段会把
整条音频流钉住；UDP 丢一包只丢一包，配 jitter buffer + PLC 即可糊过去。

我们当前 PAV1 音频复用 TCP（`docs/capture-uplink.md`），是结构性风险。
推断（**未验证**）：若抖动主要是"时延大幅波动"而非丢包，UDP 收益依然存在
（不再被重传/窗口阻塞），但需要服务端实现 jitter buffer。

---

## 5. 借 / 不借清单

### 该借（按性价比排序）

| # | 做法 | 依赖 | 风险 |
|---|---|---|---|
| 1 | 队列满**丢最旧**，队列按**时间**定深 | 无 | 极低 |
| 2 | 发送/编码队列按"秒"而不是"帧数"设计 | 无 | 低 |
| 3 | 60 ms 帧 + complexity 0 + DTX + VBR | Opus | 中 |
| 4 | 采集/编码/输出任务分层（prio 8 / 2 / 4） | Opus | 中 |
| 5 | codec 空闲 15 s 关断 | 无 | 低 |
| 6 | 音频走 UDP（控制走 TCP） | 服务端配合 | 高 |
| 7 | 独立 WakeNet + 资源动态释放 | esp-sr 模型 | 中高（内存紧） |

### 不该借

- **整套 AFE**（AEC/降噪/波束）：C3 走轻量路径本就没有；我们按键触发、不做实时打断，不需要回声消除。
- **整体移植小智固件**：薄终端路线已跑通（控制面/协议自研，见 `docs/terminal-firmware.md`）。
- **20 ms Opus 帧**：上游用 60 ms，包数与 CPU 都更省；改 Opus 时直接对齐 60 ms。
- **云端 MQTT 体系**：我们的控制通道（TCP 18080 + 认证 + PAV1）已验证可用，不为此重写。

---

## 6. 未验证清单（诚实记录）

1. `esp_opus_enc` 在 **IDF 5.5.3 + C3** 上的组件可用性与真实内存占用 —— **必须实测**，
   不能拿上游"S3 上能跑"当依据（上游 CI 覆盖多目标，但我们没跑过）。
2. `esp_opus_enc` 单帧编码耗时（C3 是 160 MHz 单核，要求 < 5 ms/帧）。
3. DTX 开启后对 ASR 的影响（静音段不发包，服务端需正确处理时间戳跳变）。
4. 唤醒词在 C3 上的实际内存峰值（上游靠 release/restore 挤内存，说明余量很小）。
5. 上游 60 ms 帧与本机 I2S DMA 配置（6×240 帧 = 6×15 ms）的契合度。