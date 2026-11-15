# 语音链路已知问题清单（2026-11-15 夜）

> 状态基准：链路已通过可用验收（`docs/status-2026-11-15.md` §6），
> 这里记录**明知存在但尚未修**的问题，以及"看起来像 bug 其实是设计"的坑。
> 每一条都带：现象、怎么判定、归属哪一侧、临时应对。

## 一、会造成"没声音 / 没反应"的

| # | 现象 | 判定方法 | 归属 | 应对 |
|---|---|---|---|---|
| 1 | 按键后毫无反应，日志里连 `key press` 都没有 | 服务端日志无按键事件；设备自报 `cap_state=idle` | **已修**（固件，根因见 §七.1） | 新固件已消除；旧固件只能断电重启 |
| 2 | 音频通道 `audio:false` 但控制通道正常 | `server.admin list` 看 `audio` 字段；日志出现 `audio hello timeout after 16 bytes` | **已修**（固件，根因见 §七.2） | 新固件已消除（含重连）；`audio_revive_after` 仍默认关闭 |
| 3 | 屏幕有字、喇叭无声 | 日志有 `speak: first audio chunk SENTINEL` 或完全没有 `first audio chunk` | 服务端/桥 | 已修多轮；未再复现 |
| 4 | 声音播到一半断掉 | 日志 `audio_underrun` / `no playback.ended within 10s` | 设备侧缓冲见底 | 已用静音保持 + 交接缓解 |
| 5 | 屏幕停在 `speaking` 不回 idle | 日志 `-> idle (state.set FAILED ...)` 或设备忽略命令 | 若 `ok` 仍卡住 = **固件** | 已加：超时强制转 idle + 日志 |
| 6 | 热点切换后整段失联 | 日志 `Connection timed out`（Errno 110） | 网络（移动热点 NAT） | keepalive 会判死并重连；音频通道需重拨 |

## 二、会让回复"内容不对"的

| # | 现象 | 原因 | 归属 |
|---|---|---|---|
| 7 | 念出来的是过程话而不是结论 | 桥此前把每步文本都当答案；现按 `step` 边界分类（≤15 字算进度话） | 已修，规则见 status §6.2 |
| 8 | 结论被截断 | 45 秒朗读预算 | 设计如此；要"详细讲"需加详述档（未做） |
| 9 | 数学公式/英文缩写被念歪 | `speech.py` 只做规则清洗，不做数字/符号朗读转换 | 未做 |
| 10 | 表格被念成一句话 | 表格行被压平（`|` → 空格） | 未做（优先上屏） |

## 三、架构性限制（不是 bug）

| # | 限制 | 说明 |
|---|---|---|
| 11 | 语音与打字共用同一会话与队列 | 长任务会把后续轮次顶住；审批需要点飞书卡片 |
| 12 | 桥重启 = 会话运行时中断 | 改桥代码必须用户 `/restart`；桥内 agent 不能自杀重启 |
| 13 | 一次只能有一个语音 ask | 桥回 `busyWith`，不排队（排队会掩盖调用方丢轮次） |
| 14 | 这个 dsh 不发 token 级 chunk | 只有 `assistant/message`（每步一条），所以"边想边说"退化为"步进式" |
| 15 | 屏幕镜像上限 400 字符 | 更长的溢出被切；需要翻页得改协议 |

## 四、运维坑（踩过，写下来）

1. **隧道在哪台机器上**：iPad 的 `19922` 绑在**汤圆**，不在米线。查不到不代表断了。
2. **iPad 的 sshd 只支持老算法**：必须 `HostKeyAlgorithms=+ssh-rsa PubkeyAcceptedAlgorithms=+ssh-rsa`。
3. **不要用 `pkill -f "python3 -m server"`**：模式会匹配到自己所在的 shell，把自己杀掉（今天两次）。
4. **服务重启会打断正在播的一轮**：重启前先确认设备 idle，否则屏幕会停在说话态（现在会强制归位）。
5. **看门狗比健康轮次还短**：45 秒的"无文本"看门狗曾把 58 秒的正常轮判死，改成 150 秒。任何超时都要大于实测最坏值。

## 五、还没验证过的

| # | 项 | 风险 |
|---|---|---|
| 16 | 打断（barge-in）：播放中按键 → `driver.stop` | 只在单测里验过，真机未测 |
| 17 | 连续 20 轮真实对话 | 单测有 20 轮 mock，真机没有 |
| 18 | 弱网（丢包 5–10%）下的 UDP 下行 | 只有 `drop_every` 台架 |
| 19 | 语音触发的写操作审批 | 会弹飞书卡片，开车场景无法点 |
| 20 | 长时间挂机（>1 天）后设备重连 | 今天多次断连都靠手动/重启恢复 |

## 六、测试与工具现状

- `ai-passport-hardware`：**106 个 Python 测试**全绿（约 18 s），`feat/passport-voice-ubuntu`；
- `dsh-feishu`：**11 个 Node 测试**全绿 + 沙箱 E2E 8/8，`main`；
- 排障工具：`python3 -m server.tools.voice_probe ping|ask "..."`、`python3 -m server.admin list|send|speak|show`；
- 日志关键字：`first text chunk` / `first audio chunk` / `handed off` / `late answer` / `budget exhausted` / `-> idle (state.set …)` / `audio_underrun`。

## 七、固件侧修复记录（2026-11-15 夜，小白马）

原 #1、#2 已修并烧录；附带修掉一个只有诊断命令会踩的栈溢出。三项都给了"能指出哪个
计数器在动"的判据（§七.4）。

### 七.1 按键卡死（原 #1）——根因不在按键，在发送锁

`terminal_net.c` 的 socket 只设了 `SO_RCVTIMEO`，**没设 `SO_SNDTIMEO`**。服务端一旦
读得慢（正在补下行音频、写日志），`send()` 就会在 `s_tx_mu` 锁内永久阻塞；而
`terminal_link_emit_key()` 用 `portMAX_DELAY` 等同一把锁，于是 `key_task` 永久挂住，
服务端连 `key press` 都收不到。

这也解释了"为什么总在 `audio_underrun` 之后"——那正是服务端最忙、读得最慢的时刻。

**改法**：`SO_SNDTIMEO = 500 ms`（超时后 `tcp_sink` 标记链路已死，`net_task` 弃链重拨）；
`terminal_link_emit_key()` 的锁等待从 `portMAX_DELAY` 降到 120 ms，宁可在极端拥塞时丢
一条按键事件，也不让"按了没反应"变成常态。

### 七.2 半帧 HELLO（原 #2）——头与载荷分两次写

`terminal_pav1.c` 的 `send_frame_budget()` 先 `send` 16 B 帧头，再单独 `send` 载荷。
载荷那次在 50 ms 预算内写不出去时（刚建连要等 ARP、对端窗口未张开），**头已经上线、
载荷永远没发**。服务端收到一个孤立的 `payload_len=121` 的头，然后一直等 payload 到超时
——原话就是 `audio hello timeout after 16 bytes`（137 B 只到 16 B）。

**改法**：拼成一帧写入（静态 `s_tx_stage`，头+载荷同一缓冲，且拼帧动作在锁内），
要么整帧进 socket，要么一个字节都不进；握手帧的发送预算从 50 ms 提到 800 ms
（`HELLO_SEND_BUDGET_MS`），因为握手正发生在"这个连接还没发过任何字节"的时刻。

### 七.3 `get_net` 打爆 `dsh_usb` 栈

`proto_task` 栈 4096 → 8192。`get_net` 在栈上开 `body[768]`，`terminal_net_describe()`
里又有 `ssid[198] + host[390]`，加 cJSON 与 printf 的帧直接打爆，表现是
`***ERROR*** A stack overflow in task dsh_usb` 且 `reset=panic`。日常按键对话不走这条
路径，只有诊断命令会踩。

### 七.4 烧录后验收（判据 → 实测）

| 项 | 判据 | 实测 |
|---|---|---|
| 半帧 HELLO | `audio hello timeout` 计数 | **0**；同期 UDP 重连 3 次（`:18926`→`:30268`→`:14271`），每次 `audio_ready` 都保持 `true` |
| 按键卡死 | `key press` / `capture.started` 是否到达服务端 | 4 轮全部到达，turn 1→2→3→4 递增 |
| `get_net` 爆栈 | `boot` 是否变化、`reset` 取值 | `boot=2` 不变，`reset=usb`（烧录复位，非 panic） |

设备体征：`uptime=1063 s` 连续运行，`heap` 30724 → 30668（无泄漏迹象），
单轮下行 `worst send block 8 ms`。

一轮完整链路（烧录后首轮）：`key press` → 86 帧 / 1.72 s `rms=6596` → ASR 2817 ms
「测试。」→ 垫话 0.2 s → 首段文本 +14551 ms → 下行 929280 B → `-> idle (state.set ok)`。

### 七.5 仍留在固件侧的

- 原 #5（屏幕停在 `speaking`）：本轮 4 轮全部 `-> idle (state.set ok)`，未复现。按
  "`ok` 仍卡住 = 固件"的判据，等真出现时单独查 UI 状态机，不先动。
- 音频通道连续失败 3 次后 `s_should_run=false`（彻底关闭，直到控制通道重新触发
  `terminal_pav1_start`）。半帧修好后触发概率大降，但这个"3 次就永久放弃"的策略本身
  仍偏激进，下次可以改成"退避重试但永不放弃"。