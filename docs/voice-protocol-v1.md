# Voice protocol v1 (`PAV1`) — Passport ⇄ Ubuntu 语音与控制合同

| 项 | 值 |
|---|---|
| 状态 | **FROZEN v1.0（2026-11-14）** — 双方签认，wire format 冻结 |
| 作者 | Ubuntu Agent（语音服务 / dsh 集成） |
| 签认 | Windows Agent：[`docs/voice-protocol-v1-windows-signoff.md`](voice-protocol-v1-windows-signoff.md) @ `27c133f`（D1–D8 全部同意，D6 由实测结案） |
| 基线 | `ai-passport-hardware` `main @ 913f3ab`；已吸收 `feat/passport-voice-windows @ 27c133f` 的 W0 修复与真机实测 |
| 测试向量 | [`protocol/v1/vectors.json`](../protocol/v1/vectors.json) + 生成器 [`make_vectors.py`](../protocol/v1/make_vectors.py) |
| 依据 | 双 Agent 任务书 §1（P0）、§1.6（测试向量）、§4（版本纪律） |

> **冻结规则**：本文件是唯一维护点。任何 wire format 变更必须先改本文件 + 向量 + 版本号，
> 两端同步后再生效；禁止一端先改代码。签认时请在 §14 的表格里逐条写「同意 / 改为 X」。

---

## 1. 范围

**做**：设备音频上行（麦克风）、下行（TTS 播放）、语音轮次的控制与状态、认证、打断、
断线重连后的清理；控制通道沿用现有 JSONL 命令并做兼容扩展。

**不做**（v1 明确排除）：常开麦克风 / 离线唤醒词 / 全双工 / AEC / 设备端 ASR·TTS / OTA /
多设备并发 / 加密传输（TLS·DTLS）/ UDP·RTP / 设备直连百炼。

---

## 2. 通道

两条 TCP 连接，**都由 Passport 主动拨出**（设备永远是 client，永不监听端口）。

| 通道 | 端口（默认） | 载体 | 内容 |
|---|---|---|---|
| 控制 | `control_port`，服务端 `18080` | UTF-8 JSONL，`\n` 结尾，一行一个对象 | 请求/应答 + 事件 |
| 音频 | `control_port + 1`，服务端 `18081` | **只放 PAV1 二进制帧** | PCM / 握手 / END |

- 音频口地址由控制通道 `hello` 应答下发（`audio_port`），也可在 `set_net` 里显式指定；
  设备编译期默认是 `SERVER_PORT + 1`，与旧固件（默认 8080）向后兼容。
- 两条通道共享 `session_id`（服务端在 `hello` 应答里分配）。**turn 的身份是
  `(session_id, turn_id)`**，不是裸 `turn_id`——重连后 `turn_id` 可以从 1 重新开始，
  旧 session 的残余音频必须按 session 丢弃。
- 音频通道故障不得影响控制通道：`get_status` / `show_text` / 音量亮度恢复必须始终可用。
- USB 仍是开发/救援/诊断通道，v1 不承载语音。

### 2.1 端口配置流程

```
设备（首次上电，编译期默认）  control=8080  audio=8081  host=<Kconfig>
        │
        │  USB: {"cmd":"set_net","ssid":"..","password":"..","host":"1.2.3.4","port":18080,
        │        "audio_port":18081}          ← audio_port 为 v1 新增可选字段
        ▼
设备持久化到 NVS（namespace dshtc：ssid/pass/host/port/audio_port）并重连
```

缺省规则（必须实现，避免两端各写一版）：`set_net` 未给 `audio_port` 时取 `port + 1`；
`port` 未给时沿用 NVS 现值。`get_net` 应答增加 `audio_port` 字段。

---

## 3. 认证

设备与本机都处在**无加密裸 TCP**上。v1 的认证目标是「不让误连/扫描到的第三方下发命令」，
**不是**防窃听；不得对外宣称等价于 TLS。部署纪律：只允许受控局域网，禁止公网端口转发，
服务端绑定固定网卡并在防火墙上只放行设备所在网段。

### 3.1 密钥

- 每设备一把 `shared_secret`（≥32 字节随机），**独立于** `cardid` / DeviceKey / DeviceSecret。
- 服务端放在 `server/config/devices/<device_id>.secret`（权限 0600，git 忽略）。
- 设备侧首次写入走 USB `provision` 命令（见 §7.3），落 NVS 独立命名空间 `pavauth`，
  **任何日志、`get_net`、串口输出都不得回显**。
- `device_id` = `psp-` + WiFi STA MAC 后 6 位十六进制（例：`psp-00a1b2`）。它只是标识，
  不是身份凭据——单靠它可以被任何人伪造。

### 3.2 控制通道握手

```
设备 → {"id":1,"cmd":"hello","proto":"v1","device_id":"psp-00a1b2","nonce":"<32 hex>"}
服务 → {"id":1,"ok":true,"cmd":"hello","proto":"v1","server_nonce":"<32 hex>",
        "session_id":"<16 hex>","audio_port":18081}
设备 → {"id":2,"cmd":"auth","mac":"<HMAC hex>"}
服务 → {"id":2,"ok":true,"cmd":"auth","server_mac":"<HMAC hex>"}
```

```
control_mac = HMAC-SHA256(secret, "PAV1AUTH|control|<device_id>|<device_nonce>|<server_nonce>")
server_mac  = HMAC-SHA256(secret, "PAV1AUTH|server|<device_id>|<device_nonce>|<server_nonce>")
```

- `nonce` 为 16 字节随机数的 32 位小写 hex。设备无 RTC，**协议里不出现时间戳**，防重放靠
  nonce：服务端缓存最近 5 分钟内见过的 `device_nonce`（≥64 条），重复即拒绝。
- AUTH 之前的任何其他 `cmd` → `{"id":N,"ok":false,"err":"unauthorized"}`；累计 3 次关闭连接。
- AUTH 失败 → `{"ok":false,"cmd":"auth","err":"auth_failed"}` 后立即 `close()`，不做重试引导。
- 握手总时限 5 s，超时关闭。

### 3.3 音频通道握手

首帧必须是 `HELLO`，载荷 UTF-8 JSON：

```json
{"device_id":"psp-00a1b2","proto":"v1","session_id":"<控制通道分配值>","nonce":"<32 hex>"}
```

服务端必须校验 `session_id` 与 `device_id` 同属一条已认证的控制连接，否则回
`HELLO {"ok":false,"err":"session_mismatch"}` 并关闭。通过后服务端回
`HELLO {"ok":true,"server_nonce":"<32 hex>"}`，设备随后发 `AUTH` 帧：

```
audio_mac = HMAC-SHA256(secret, "PAV1AUTH|audio|<device_id>|<device_nonce>|<server_nonce>")
```

载荷 `{"mac":"<hex>"}`；服务端回 `HELLO {"ok":true}`（同帧类型，`type` 字段区分）。
认证通过前到达的 `PCM` 帧必须丢弃并报 `NOT_AUTHED`。

**音频通道的打开时机（签认时补定）**：`auth` 成功后**立即**拨号并保持长连接，不在按键时
再连——省掉一次 TCP+TLS 都不需要但确实存在的三向握手，也让 `capture.start` 一到就能立刻推流。
掉线后按需重开（重连即重走 §3.3 握手），不做"常开且无限重试"的守护循环：连续失败就退回
「无音频」状态并在 `get_status` 里如实反映（`audio_ready:false`），控制通道继续可用。

---

## 4. 音频格式

| 项 | 值 | 说明 |
|---|---|---|
| 编码 | `pcm_s16le` | 有符号 16 bit 小端，无压缩 |
| 采样率 | **16000 Hz** | 单一值；百炼 realtime ASR 与 TTS 都直接输出这个格式 |
| 声道 | **1（mono）** | |
| 首选帧长 | 20 ms = **640 B** | 与 I2S DMA 块和 20 ms 网络节拍对齐 |
| 允许帧长 | 40 ms = 1280 B | **仅为将来兼容保留，v1 设备不使用**；两端一律按 640 B 实现，`hello` 里不协商帧长 |
| 码率 | 32 KB/s（256 kbit/s）单向 | 双向同时最多 64 KB/s，4 Mbps 链路毫无压力 |

- **设备是上行时钟源**：采集按 I2S 真实时钟分帧，帧间隔的等效采样数必须是 320 的整数倍；
  不允许「按 20 ms 定时器凑数」造成 ±5% 的采样漂移（ASR 会感知为语速异常）。
- **服务端是下行时钟源**：下行帧按 20 ms 节拍发送即可，设备侧靠环形缓冲吸收抖动；
  服务端不得突发灌入整段（会打爆设备内存）。
- 两方向不做重采样协商：**服务端负责把任意 TTS 输出转成 16 kHz mono s16**，设备不接触
  任何第三方采样率。

> **D6 结案（真机实测，2026-11-14）**：同一构建、同一设备，只改帧长——
> 20 ms/640 B（100 帧）与 40 ms/1280 B（50 帧）**读失败 0、写失败 0、2000 ms 名义耗时同为
> 1992 ms、rate_ratio 同为 99**。总字节相同、耗时相同 ⇒ 节拍由 codec 时钟决定，与帧长无关。
> 所以"ES8311 DMA 只能 40 ms"的退路不存在，v1 定为 640 B，且**不在 `hello` 里协商帧长**——
> 少一个可协商项就少一个分叉点。非法帧长（如 25 ms）设备侧返回 `ESP_ERR_INVALID_ARG`，
> 不静默降级。

---

## 5. PAV1 帧

固定 **16 字节头**（小端）+ `payload_len` 字节载荷。

| 偏移 | 长度 | 字段 | 定义 |
|---|---|---|---|
| 0 | 4 | `magic` | ASCII `PAV1` |
| 4 | 1 | `kind` | `1=PCM` `2=HELLO` `3=END` `4=AUTH` |
| 5 | 1 | `flags` | v1 全 0（保留；收到非 0 应忽略而不是报错） |
| 6 | 2 | `reserved` | v1 固定 0 |
| 8 | 4 | `turn_id` | uint32 LE；与同轮控制消息一致；握手帧用 0 |
| 12 | 4 | `payload_len` | uint32 LE，**≤ 4096** |

`HELLO` / `AUTH` 载荷是 UTF-8 JSON；`PCM` 载荷是裸 PCM；`END` 载荷长度必须为 0。

### 5.1 解析规则（必须逐条对应向量）

```c
// 参考实现：只认「精确字节数」，绝不扫描重同步
while (len >= 16) {
    if (memcmp(buf, "PAV1", 4)) return FATAL(BAD_MAGIC);        // 关闭通道
    uint32_t n; memcpy(&n, buf + 12, 4);                        // 小端
    if (n > 4096) return FATAL(OVERSIZE);                       // 关闭通道
    if (kind 不在 1..4) return FATAL(UNKNOWN_KIND);             // 关闭通道
    if (kind == PCM && n == 0) return FATAL(BAD_FRAME);
    if (kind == END && n != 0) return FATAL(BAD_FRAME);
    if (len < 16 + n) break;                                    // 半截帧：等更多字节
    deliver(kind, turn_id, buf + 16, n);
    buf += 16 + n; len -= 16 + n;
}
```

| 情况 | 处理 |
|---|---|
| magic 错 / 超长 / 未知 kind | **致命**：报 `error` 事件 + 关闭音频通道（不做重同步，避免半帧被当成帧头） |
| 半截帧 | 等待；不得超时前判失败 |
| 连接关闭时仍有残帧 | 丢弃，报 `INCOMPLETE_FRAME` |
| `END` 重复 | 会话层幂等：第二次不再触发 `playback.ended` |
| `END` 后同轮 `PCM` | 报 `TURN_CLOSED`，丢弃（不播放） |
| 非当前轮 `PCM` | 报 `TURN_MISMATCH`，丢弃；**绝不「下一轮说不定用得上」** |

内存上限：解析器只允许持有一个未完成帧，且 `payload_len ≤ 4096`；不接受任何「先按
`payload_len` 分配再说」的建议（4096×N 会直接打爆无 PSRAM 的堆）。

---

## 6. 控制通道

### 6.1 现状（保留，语义不变）

`get_status` `show_text` `set_volume` `set_brightness` `ping` `get_net` `set_net` `scan`；
事件 `ready` `key`。

两条硬约束（已由 W0 修复确定）：

- **入站单行 ≤ 512 字节**；超长行丢弃到下一个 `\n` 并报一次 `line_too_long`，绝不半行执行。
- **出站单条 ≤ 2048 字节**（固件 `TX_MAX`）；`scan` 若装不下必须带 `"truncated":true`
  且 `count` = 实际发出的条目数，禁止截断成非法 JSON。

### 6.2 请求来源定向（W0-4，双方都要遵守）

- 请求的应答**只回原连接**；USB 与 TCP 同时在线时不得互相收到对方应答（`id` 会撞）。
- 只有 `ready` / `key` / `error` 等**事件**允许广播。
- 服务端实现要求：`id` 由客户端单调分配、服务端在 5 s 内不回则视为超时（`ERR_TIMEOUT`）。

### 6.3 新增命令

| 命令 | 方向 | 语义 |
|---|---|---|
| `capture.start` `{turn_id}` | 服务→设备 | 允许上传该轮音频；设备先补发 pre-roll |
| `capture.stop` `{turn_id}` | 服务→设备 | 结束上传：立即发 `END` 帧 |
| `playback.start` `{turn_id}` | 服务→设备 | 预缓冲 300 ms 后开始 I2S 播放 |
| `playback.stop` `{turn_id}` | 服务→设备 | 打断：清空环形缓冲、静音 I2S |
| `state.set` `{state,turn_id}` | 服务→设备 | 屏幕状态（`idle/listening/processing/speaking/error`） |
| `provision` `{secret,`<可选>`device_id}` | USB | 写入/轮换 `shared_secret`（仅 USB，TCP 一律拒绝） |
| `get_audio` `{}` | 服务→设备 | 返回音频通道状态（`state`/`ring_ms`/`underruns`/`overflows`），排障用 |

应答统一 `{"id":N,"ok":true,"cmd":"<命令名>"...}` 或 `{"id":N,"ok":false,"cmd":"...","err":"<code>"}`。

### 6.4 事件

| 事件 | 说明 |
|---|---|
| `ready` `{link:"usb"\|"net"}` | 每条载体各自一次，**必须在 `auth` 成功之后才发** |
| `key` `{btn,ev}` | 原样保留；`ev` ∈ press/click/double/long/release |
| `capture.started` `{turn_id}` | pre-roll 已补发，开始送实时帧 |
| `capture.ended` `{turn_id,reason}` | `released`/`cancelled`/`max_duration`/`error` |
| `playback.started` `{turn_id}` | I2S 已出第一块声音 |
| `playback.ended` `{turn_id,reason}` | `completed`/`cancelled`/`underrun`/`error` |
| `error` `{turn_id?,code,detail?}` | 结构化，见 §6.5 |

`ready` 的时机是签认时补定的：早期固件连上就发 `ready`，而 §3.2 规定 auth 之前除 `hello`/`auth`
外一律 `unauthorized`——两者直接冲突，且三次就会关连接。**定案：`ready` 在 `auth` 成功后发**，
其 `link` 字段只用于 UI 提示，不承载会话语义；服务端在认证前收到任何事件都只记录并忽略，
不计数、不触发轮次。

### 6.5 错误码（稳定，不得改写语义）

| code | 触发 | 处置方 |
|---|---|---|
| `unauthorized` | 认证前收到业务命令 | 设备生 / 服务生 |
| `auth_failed` | HMAC 不符 | 关闭连接 |
| `bad_json` | 以 `{` 开头但解析失败 | 回错误，不断开 |
| `unknown_cmd` | 未知命令名 | 回错误 |
| `line_too_long` | 入站行 > 512 B | 丢弃该行 |
| `busy` | 已有活动 turn 时又开新轮 / 扫描期间来语音命令 | 拒绝 |
| `not_supported` | 固件未实现（如中文字形） | 拒绝，不假成功 |
| `cap_start_timeout` | 按键后 1500 ms 未收到 `capture.start` | 设备放弃本轮 |
| `playback_stall` | `playback.start` 后 2000 ms 无任何 PCM 帧 | 设备报错回 idle |
| `audio_underrun` | 播放中环形缓冲见底且 500 ms 内无新数据 | 停播，`playback.ended(underrun)` |
| `audio_overflow` | 上行发送积压超上限（丢帧） | 计数 + 结束时随 `capture.ended` 上报 |
| `turn_closed` `turn_mismatch` | 见 §5.1 | 丢弃帧 |
| `bad_magic` `oversize` `unknown_kind` `bad_frame` `incomplete_frame` | 见 §5.1 | 关音频通道 |
| `session_mismatch` | 音频通道 `session_id` 与控制通道不一致 | 关音频通道 |

---

## 7. 轮次生命周期

### 7.1 正常一轮（按键说话 → 播放）

```
设备(OK 按下)   控制: {"event":"key","btn":"ok","ev":"click"}
                本地: 立即进 listening 态 + 开始写 300 ms pre-roll 环（9,600 B）
服务            分配 turn_id=12 → 控制: {"id":101,"cmd":"capture.start","turn_id":12}
设备            音频: PCM(12) …（先补发 pre-roll，再送实时帧）; 事件: capture.started
设备(OK 再按)   事件: capture.ended 前先发 END 帧(kind=3,turn=12)
服务            ASR → dsh → TTS
服务            控制: {"id":103,"cmd":"playback.start","turn_id":12} + 音频: PCM(12) …
设备            预缓冲 300 ms → I2S 播放 → 事件: playback.started
服务            TTS 结束 → 音频: END(kind=3,turn=12)
设备            缓冲播完 → 事件: playback.ended(turn_id=12,reason=completed)
服务            控制: {"id":106,"cmd":"state.set","state":"idle","turn_id":12}
```

**为什么需要 pre-roll**：`turn_id` 归服务端分配，但用户按下按键的第一个字必须录到。
所以设备按键后立刻按 300 ms 环形缓冲录制，收到 `capture.start` 时先把这段补发出去。
若 1500 ms 内没等到 `capture.start`：放弃本轮，报 `cap_start_timeout`，**不静默继续录**。

### 7.2 状态机

```
        ┌──────────────────────────── error ◄──────────┐
        │                                              │
     idle ──key──► listening ──END/stop──► processing ──playback.start──► speaking ──END+drain──► idle
        ▲              │                       │                            │
        │              └──cancel/超时───────────┘                            └──playback.stop──► idle
        └──────────────────────────────────────────────────────────────────────────────────────────┘
```

- 同一设备同一时刻**只有一个活动 turn**；再来一轮 → `busy`。
- 状态只由服务端 `state.set` + 设备本地事件共同驱动；设备不得自行发明状态（避免 UI 与服务端认知分叉）。
- `scan` / `set_net` 执行期间禁止开新语音轮（`busy`），避免断 Wi-Fi 打断会话。

### 7.3 `scan` / `set_net` 会终止整个会话（不是「暂停」）

扫描必须让 WiFi 驱动主动 disconnect（否则 `esp_wifi_scan_start` 直接返回
`ESP_ERR_WIFI_STATE`），`set_net` 要重建 STA。后果是**两条 TCP 通道同时断、
当前 `session_id` 作废**：

- 设备侧：正在播放的音频立即停止，上报（若还能上报）`playback.ended{reason:"cancelled"}`；
  若来不及，对端消失本身即为终止信号。
- 服务端侧：必须把「对端消失」当作正常收尾路径——结束当前轮、丢弃该 session 的发送队列、
  不重放旧音频；收到设备重连后的新 `hello` 视为**新会话**。
- 上层调用：`scan`/`set_net` 只允许在 `idle` 发起；服务端在发起前必须确认无活动轮次，
  否则先 `playback.stop` 再下发。

### 7.3 打断

- 播放中按键 → 设备立即静音并清空环形缓冲（不再播任何排队帧），上报 `key` 事件。
- 服务端收到后下发 `playback.stop{turn_id}`；设备回 `playback.ended(reason=cancelled)`。
- **打断不得取消 dsh 的后台任务**：TTS 生成侧停止、音频侧丢弃；dsh 的 tool 调用继续跑完，
  结果改为文本通知（`show_text`）或下一轮语音播报。
- 被打断的 turn 标记为 `cancelled`，其后续迟到的 PCM 帧按 `turn_closed` 丢弃。

### 7.4 断线重连

| 场景 | 要求 |
|---|---|
| 控制通道断 | 设备 2.4 s 退避重连；重连后重新 `hello`/`auth`，拿新 `session_id`；旧 turn 全部作废 |
| 音频通道断 | 当前 turn 终止（`playback.ended(error)` / `capture.ended(error)`），控制通道保持可用 |
| 断线重连后 | **不重放**旧音频、不重发旧命令；服务端清空该 session 的发送队列与 ASR 缓冲 |
| 服务端重启 | 设备视为断线；重连后 `hello` 重新分配 `session_id`；设备 `turn_id` 可由服务端从 1 重开 |

`turn_id` 单调递增只在**同一 session 内**有约束；跨 session 仅 `(session_id,turn_id)` 有意义。

---

## 8. 定时器与容量

| 项 | 值 | 依据 |
|---|---|---|
| 控制通道 `hello`+`auth` 总时限 | 5 s | 避免半开连接堆积 |
| 请求应答超时（服务端侧） | 5 s | `id` 关联；超时记 `ERR_TIMEOUT` |
| pre-roll 环 | 300 ms（9,600 B） | 覆盖按键到 `capture.start` 的往返 |
| `capture.start` 等待上限 | 1500 ms | > 往返 ×3（实测往返 ~200 ms） |
| 单轮采集上限 | 30 s（960 KB 流过，不落盘） | 防止忘按结束 |
| 播放环形缓冲 | **1200 ms（38,400 B）** | 无 PSRAM 下可接受；漂移/抖动余量 |
| 播放预缓冲 | 300 ms | 首音延迟与欠载的折中 |
| 播放 stall 判定 | 2000 ms 无新帧 | 慢 TTS 首包实测 1.1 s，留 2 倍余量 |
| 欠载判定 | 见底后 500 ms 无数据 | 停播而不是播空气 |
| 上行积压上限 | 8 KB（≈250 ms） | 超限丢最旧帧 + 计数 |
| 帧上限 | 4096 B | 单帧最多 128 ms 音频 |
| 控制行上限 | 入 512 B / 出 2048 B | 固件实际值（`LINE_MAX`/`TX_MAX`） |

设备侧内存账（供 Windows 侧核对，非约束）。**注**：早期稿写的"可用堆 ~170 KB"是估算，真机
（Wi-Fi + LVGL + UI 全起、运行 21 s）实测为 `heap=118624 / heap_min=113236 / heap_block=106496`。
55 KB 预算仍成立（最大单块 38,400 B ≪ 106,496 B），但余量是 ~63 KB 而不是 ~115 KB。
这三个数已做进固件 `get_status`，联调时服务端**直接读**，不要估算：

```
pre-roll 环   9,600 B
播放环       38,400 B
解析/发送缓冲 ~4 KB
I2S DMA      4 × 640 B ≈ 2.6 KB
合计         ≈ 55 KB   （实测空闲 118,624 B / 最大连续块 106,496 B 时余量 ~63 KB；须实测高水位）
```

---

## 9. 时延预算与实测基线

Ubuntu 侧在本机（洛杉矶）实测，作为 v1 的**参考基线**，不是承诺值：

| 环节 | 实测 | 备注 |
|---|---|---|
| 百炼 realtime ASR 建会话 | 0.80 s | 长连接可摊到 0 |
| 首个 partial（相对发声） | **0.50 s** | `conversation.item.input_audio_transcription.delta` |
| 终稿（相对末帧） | **0.50 s** | `.completed` |
| TTS `sentence-begin` | 0.94 s | 27 字句 |
| TTS 首个音频块 | **1.10 s** | 6.8 s 音频 2.28 s 出完 |
| 本机 → 百炼 RTT | 166 ms | 洛杉矶 → cn-beijing |

预算式（端到端首音）：`设备上行 RTT ＋ ASR 终稿 ＋ dsh 首字 ＋ TTS 首包 ＋ 下行 RTT`。
服务端选址若迁到国内，上表后三项约各降 150–200 ms；**该迁移不改协议**，只改配置。

---

## 10. 安全边界（必须如实描述）

- 明文 TCP：可被同网段窃听。v1 的 HMAC 只防「未授权下发命令」，不防窃听。
- 允许的部署：受控局域网；服务端绑定指定网卡；防火墙只放行设备网段；不开公网端口转发。
- 密钥只落在 NVS（设备）与 `server/config/devices/*.secret`（0600，git 忽略）。
- 日志纪律：不打印 `secret`、`mac`、`password`；不落原始音频到磁盘（除非显式 `--save-audio`
  调试开关，且写本地临时目录并自动清理）；转写文本按最小化原则记录。
- 拒绝：用 `cardid`/DeviceKey/DeviceSecret 做认证材料；把设备身份写入代码或仓库。

---

## 11. 兼容性

| 项 | 规则 |
|---|---|
| 旧固件（8080 默认、无音频） | 服务端必须能只跑控制通道、语命令回 `not_supported`，不 crash |
| 新固件连旧服务端 | 收到未知命令回 `unknown_cmd`；`hello` 不被识别时退化为「无认证旧模式」**仅限开发环境**，并由服务端配置项显式开启 |
| 版本号 | `hello.proto = "v1"`；服务端遇到未知版本回 `{"ok":false,"err":"unsupported_proto"}` 后关闭 |
| 帧格式变更 | 只能靠 `kind` 新增或 `flags` 位扩展；不得改头长、改字段序 |

---

## 12. 测试向量

`protocol/v1/vectors.json`（由 `make_vectors.py` 生成，可 `--check` 校验）：

- **5 个单帧**：`hello` / `pcm_20ms` / `pcm_40ms` / `end` / `auth`（含 HMAC 期望值）。
- **15 个流**，覆盖任务书 §1.6 要求：正常帧、拆包（头内 7 字节、载荷内三段）、粘包、
  超长帧、错 magic、错 `turn_id`、重复 END、END 后带载荷、未知 kind、认证前 PCM、
  错 HMAC、半截流、打断。
- **11 条控制层用例**：握手、auth、`capture.*`、`playback.*`、`state.set`、越界音量、
  未认证命令、超长行。

**两端都必须把这些向量跑成自动化测试**（C 侧建议 `test/pav1_vectors.c` 读同一份 JSON，
或把生成器移植成固定数组），通过标准：帧序列与 `error` 完全一致，一字节都不能差。
`make_vectors.py --check` 应在 CI/提交前跑，漂移即失败。

---

## 13. 未决与冻结后变更流程

1. 任何一方发现合同与实机冲突 → 在 `docs/voice-protocol-v1.md` 开「变更提案」小节，
   附最小可复现证据（十六进制帧 + 预期/实际 + 固件 SHA + 服务 commit）。
2. 双方同意后：先升版本（v1.1 或 v2）、再更新向量、最后改代码。
3. 禁止「一端先改 wire format，事后补文档」。

---

## 14. 签认记录（FROZEN v1.0，2026-11-14）

签认方：Windows Agent，载体 `docs/voice-protocol-v1-windows-signoff.md` @ `27c133f`；
Ubuntu 侧本文件 @ `feat/passport-voice-ubuntu`。**D1–D8 全部同意，无 wire format 变更。**

| 编号 | 结论 | 备注 |
|---|---|---|
| D1 | ✅ 同意 | 300 ms pre-roll = 9,600 B，相对最大连续块 106,496 B 无压力 |
| D2 | ✅ 同意 | NVS 命名空间 `dshtc`，Kconfig 默认 8080 → 音频 8081；`audio_port` 为新增 NVS 键 |
| D3 | ✅ 同意（附部署事实） | `idf.py flash` 只写 app 分区，NVS 与密钥保留；**整片擦除 / 恢复 8 MB 备份会抹掉密钥，之后必须重新 `provision`** |
| D4 | ✅ 同意 | 迟到帧按 `(session_id, turn_id)` 双条件丢弃 |
| D5 | ✅ 同意（附边界） | 设备上行非阻塞 + 丢最旧 + 计数；**绝不为"不丢帧"扩大缓冲或阻塞采集线程** |
| D6 | ✅ 定为 640 B | 真机实测见 §4 结案；40 ms 仅保留兼容，`hello` 不协商帧长 |
| D7 | ✅ 同意（已实现并真机验证） | 错误码已逐字对齐 `bad_json`/`unknown_cmd`/`line_too_long`（原带空格）；`scan` 带 `truncated:true` |
| D8 | ✅ 同意 | mbedTLS HMAC-SHA256，flash +8–10 KB；`esp_random()` 取 nonce，不依赖上电早期熵 |

**签认时补充的两条协议口径**（已并入正文，不改字节）：

1. `ready` 只在 `auth` 成功后发送（§6.4）——此前它与"认证前一律 `unauthorized`"直接冲突。
2. `scan` / `set_net` 视为会话终止：两条通道同断、`session_id` 作废（§7.3）。

**仍未实现、由 Windows 侧推进的部分**（不阻塞合同）：固件当前**没有任何 PAV1 帧**，
已具备 JSONL 控制通道、Wi-Fi STA + 主动 TCP、codec 自测。真机端到端要等 W2-3 framer
与 W2-4 上行 pre-roll 落地；在此之前 `mock_device.py` 只证明 Ubuntu 侧状态机，不证明固件。