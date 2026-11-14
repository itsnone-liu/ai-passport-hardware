# 采集链路第 0–3 段：验收报告

按目标逐段对账。**每行只写实测到什么，没测的就写没测。**

| 段 | 目标要求 | 实际 | 判据/证据 |
|---|---|---|---|
| 0 | 状态机修复 + 诊断指标 | 达成 | 见 §0 |
| 1 | Opus 编码器冒烟（堆≥10 KB、<5 ms/帧、200 帧无泄漏） | **未达成，实测证伪** | 见 §1 |
| 2 | Opus 接入（kind=5、audio_codec、独立编码任务、60 ms 帧） | **以 IMA ADPCM 交付** | 见 §2 |
| 3 | 音频走 UDP | 达成 | 见 §3 |

## §0 状态机修复 + 诊断指标

**修掉的 bug：每轮固定丢 6 帧。** 预录 15 帧（300 ms）、播放队列容量 10 帧
（200 ms），整段预录一次性压入再排空 → 最老的 5–6 帧必然被顶掉。

改法：`flush_preroll()` 里每压一帧就 `drain_pending()`，边走边发。

| 观测 | 修复前 | 修复后 |
|---|---|---|
| 设备 `dropped` | 6 / 轮 | **0** |
| 服务端 `lost` | 6 | **0** |
| 上行总帧 | 155 | **161** |

**诊断指标**：`get_audio` 返回 18 项 UDP 计数
（`udp_ready/sent/fail/ack/hello/down/bad/badmac/late/reorder/gap/parity/recovered/norecover/staleturn/slow/errno/ack_ms`）。
每加一项都可能把应答 JSON 撑爆——`audio` 缓冲从 448 字节提到 768，且
`terminal_udp_diag()` 改为返回长度，写不下时设备日志喊一声。这个坑踩过一次：
截断后上位机连整条应答都解析不出来，表现成"设备没应答"。

## §1 Opus 冒烟：判据不满足，方案作废

ESP32-C3 QFN32 只有 400 KB SRAM、无 PSRAM。实测：

| 项目 | 实测 | 判据 | 结论 |
|---|---|---|---|
| `opus_encoder_get_size(1)` | 24716 B | 常驻堆 ≥10 KB | 单这块就占满 |
| 最大连续空闲块 | 28672 B | — | 装不下编码器 + 工作区 |
| `opus_encoder_create()` | 返回 `err=-7` | 成功 | 失败 |
| 放进独立任务后 | `Stack protection fault` | 稳定 | 直接崩 |

**这不是"没调好"，是这块芯片的连续内存撑不住。** 于是改用 IMA ADPCM：
320 samples → 164 B/块，8.4 KB/s，块自带预测器与步长，丢一块只损失 20 ms
（不需要重传）。等价判据通过：常驻内存 328 B/块缓冲、无累积泄漏（200 帧编码后
堆无下降）。

## §2 正式接入（以 ADPCM 交付）

| 要求 | 实际 |
|---|---|
| PAV1 `kind=5` | 保留，语义 = ADPCM（原计划 Opus） |
| `audio_codec` 字段 | `"adpcm"`，上下行一致 |
| 独立编码任务 | 有；采集任务只做 I/O |
| 发送队列按 60 ms 帧重建 | **未按 60 ms**：一帧 = 20 ms、一包 = 一帧，队列按帧建立 |

偏差原因：60 ms 一帧意味着单包 3 倍载荷（492 B），丢一包就是 60 ms 空洞；
20 ms 一帧配合 §3 的 FEC，空洞可被单块恢复吃掉。这个改动有实测支撑，不是
"图省事"。

## §3 音频走 UDP

| 项目 | 实测 |
|---|---|
| 打洞（HELLO/ack，带 MAC） | 5.3 s 完成 |
| 上行 | 161 帧、`lost=0`、`blocks=161` |
| 下行 | 105–200 帧，`udp_down` 与发送一致 |
| 重排窗 | 50 对全倒序 → `udp_reorder=49`、欠载 0 |
| FEC（3+1 XOR，强制丢 15 块） | `udp_recovered=15`、`norecover=0` |
| 逐包 MAC | 4 种伪造全无 ack；真设备照常 |
| 重放旧轮次 | `udp_staleturn=5/5`、`udp_down` 不变 |
| 播放 | `underruns=0`、`overflows=0` |

带宽律决定了这一整套设计：在途字节 = 码率 × RTT，链路实测上限 18.7 KB/s。
PCM 上行 31.2 KB/s、下行 64 KB/s 都不可能；ADPCM 8.4 KB/s，加 FEC 11.2 KB/s，
留得下。

## 已知限制（不藏）

1. **真实人声端到端未验证**：所有采集都是环境声（`rms` 25–26 或 194），ASR
   返回空。这一项需要人对着设备说话，自动化不了。
2. **重放只防到轮次粒度**：旧轮次整轮重放已挡；当前轮次窗口外的包重放没有
   全局防重放计数（窗口内按"重复"丢弃）。
3. **上行无 FEC**：实测 `lost=0`，暂不实现。
4. **下行丢包率随时段波动**（0%~10%），FEC 只保证"同组丢一块能救"，
   丢两块记 `norecover` 如实上报。
5. `play_mismatch` 偶发 1–4：停止时尾部未播完的块被冲掉，属设计行为，未逐块对账。

## §4 真人语音端到端（首次通过）

2026-11-15 07:26，用户按住 OK 键说了一句"今天天气怎么样？"后松手。

| 环节 | 实测 |
|---|---|
| 按键 → 起采 | `key press (state=idle)` → `capture.started turn=1 pre_roll_ms=140` |
| 采集 | `frames=157 dropped=0 rms=1616 peak=32768`（3.14 s） |
| 上行解码 | `blocks=157 lost=0 bad=0 (3.14 s decoded, rms=1575)` |
| UDP 收齐 | `blocks=157 reordered=0 dups=0 gaps=0 pending=0 tail=0` |
| **ASR** | **`turn 1 asr done in 3218 ms text='今天天气怎么样？'`** |
| 回放 | `playback.start ok`、`played=114`、`underruns=0`、`overflows=0` |

**这是第一次真实人声走完全链路**：麦克风 → ES8311 → ADPCM → UDP → ASR →
TTS → 回放出声。此前所有采集都是环境声（`rms` 25–26），ASR 返回空字符串。

时间线（松手 07:26:37.939 起算）：

| 时刻 | 事件 | 相对松手 |
|---|---|---|
| 07:26:41.451 | ASR 完成 | +3.51 s |
| 07:26:42.632 | 第一块 TTS 音频（11744 B） | +4.69 s |
| 07:26:42.787 | `playback.start` ok | +4.85 s |

### 这次暴露的两个真问题（不藏）

1. **Brain 是 mock**：启动行写的是 `brain=mock`。ASR 和 TTS 是真的（bailian），
   中间那句"回答"还没接真 LLM。所以设备能听会说，但说的话不是 LLM 生成的。
2. **回放没走 UDP**：下行日志是 `first PCM frame turn=1 len=640` +
   `downlink done: 72960 bytes`——这是 **TCP/PCM** 路径（640 B/帧），
   不是 §3 做的 UDP/ADPCM（164 B/块）。`udp_down` 在整个 turn 里没有增长。
   也就是说：**上行已经全走 UDP，下行在真实回放路径上还在走 TCP**；
   台架 `push_udp` 走 UDP 是因为它直接调 `send_down_frame`，
   而 TTS 回放走的是 `_write_audio` 那条 PCM 通道。

   TCP 下行意味着丢包会卡住整条流（实测 8.4→64 KB/s 需求超过 18.7 KB/s
   上限，只能靠 TCP 的流控挤，这正是原本要做 UDP 的理由）。这是下一步。

### 顺带确认：按键语义

`terminal_main.c` 里是"按住说、松手停"：`OK+PRESS → capture_arm()`、
`OK+RELEASE → capture_request_end()`。旧代码只认 CLICK，人手按住必然超过
500 ms 被判成 LONG 去调亮度，采集一次都没启动过——这就是之前真人测试
一直失败的真因，已修。

## §5 接真 LLM（DeepSeek）

`brain=mock` → `brain=bailian-llm`（云百炼 token-plan 的 DeepSeek）。

选型不是拍脑袋，是量过首字延迟的：

| 模型 | 首字（含思考） | 首字（`enable_thinking:false`） |
|---|---|---|
| `qwen3.8-flash` | 4182 ms | — |
| `deepseek-v4.1-flash-1017` | — | **1207 ms** |
| `deepseek-v4-flash-0731` | — | 1481 ms |
| `deepseek-v4-pro` | 3222 ms | 1963 ms |

两个坑：

1. **思考模式默认开着**。deepseek 系列把正文放在 `content`、思考放在
   `reasoning_content`；不关思考时流里只有后者，而原来的解析只读 `content`，
   于是"成功请求、正文为空"。关掉 `enable_thinking` 后首字从 3–4 s 降到
   1.2 s。代码里如果关掉后仍只收到思考，会显式抛错，而不是把内心独白
   念给用户听。
2. **多轮记忆**：按 session 保留最近 6 轮。实测第二问"我刚才问你什么了"
   答得回去（`你刚才问的是今天天气怎么样`）。

直测（绕过设备）：

```
Q 今天天气怎么样？
  首字 1086 ms / 总 1505 ms / 13 块
Q 我刚才问你什么了？
  首字  923 ms / 总  996 ms / 3 块
```

## §6 回放下行切到 UDP/ADPCM

之前 §4 的结论是"回放还在走 TCP/PCM"。根因不是 UDP 通道没做好，而是
**真实回放从来没调用 `send_down_frame`**：

```
await self.audio.send_frame(pav1.KIND_PCM, turn_id, frame)   # 640 B/帧，TCP
```

台架 `push_udp` 直接调 `send_down_frame`（UDP 优先、失败回退 TCP），所以
"下行 UDP 通过"这个结论一直只对台架成立。改成走统一入口后：

```
first downlink frame turn=1 len=640 via udp
```

一次回放（"下行现在走 UDP 了…"）的实测：

| 指标 | 切换前（TCP） | 切换后（UDP） |
|---|---|---|
| `udp_down` | 130（不动） | **292（+162）** |
| `udp_parity` | 50 | **101（+51）** |
| `udp_recovered` | 15 | 16（FEC 救回 1 块） |
| `play_overflows` | 1 | **0** |
| `play_underruns` | 0 | **1** |
| 单帧 640 B → | 640 B 上线 | 188 B 上线（8.4 KB/s） |

另外每轮开始重置 `_down_seq`/`_down_state`/`_fec_acc`：跨轮沿用会让一个
奇偶组横跨两轮，恢复出来的块挂到错误的 turn 上。

服务端测试 85 passed, 3 skipped。

### 还没解决的

- `play_underruns=1`：TCP 时是 0，切 UDP 后出现一次。怀疑是开头
  prebuffer 差一点（`playback_lead=0.12s`），需要按发生位置确认。
- 上行仍无 FEC（实测 `lost=0`，暂未暴露）。
