# 下一步方案：Opus + 队列语义 + UDP 音频

制定时间：2026-11-14 夜。计划执行起点：2026-11-15。
前置阅读：`docs/capture-uplink.md`（已跑通的 PCM 上行）、`docs/xiaozhi-patterns.md`（上游经验）。

---

## 0. 现状（已验证的事实，不是计划）

| 项 | 状态 | 依据 |
|---|---|---|
| 按键 → 采集 → pre-roll → 收轮 | ✅ 跑通 | `capture.started` / `capture.ended` 全链路日志 |
| PCM 上行（RTT 平稳时） | ✅ 跑通 | 按住 2 s：`frames=94 dropped=0 reason=released`，堆 14.4→30.4 KB 全返还 |
| 对端不读时的健壮性 | ✅ 已修 | 不再堆塌、不再假死；报告 `uplink_stall` + 计数 |
| 控制通道掉线自愈 | ✅ 已修 | 连续 4 次失败且堆 <6 KB 主动重启 |
| **长语音 / 高抖动下完整送达** | ❌ **不达标** | 见 §1 |
| 真人语音 ASR 端到端 | ❌ 未验证 | 明明音频退化为 4 帧，未做真人测试 |
| 设备重连控制通道 | ⚠️ 2–5 s | 正常，但掉线后恢复正常 |

---

## 1. 为什么必须换 Opus：一次决定性实测

设备自带上行自测（`vendor.uplink_test`，250 帧 × 640 B = 160 KB，按 20 ms 节拍推）：

```
250/250 帧全部送出，errno=0，耗时 8,567 ms
实测持续吞吐 = 18.7 KB/s
计算：4096 B ÷ 18.7 KB/s ≈ 219 ms  ← 恰等于 RTT
```

**结论一**：`errno=0` 且 250/250 说明**对端读取器完全正常**——之前"对端没在读"的判断被推翻。
**结论二**：18.7 KB/s 不是线路极限，是**我们自己 `SO_SNDBUF`（4 KB）的上限**，
即 `吞吐 = 发送缓冲 / RTT`。

而实时 PCM16/16 kHz 需要 **31.2 KB/s**。于是：

```
在途字节数 = 码率 × RTT（BDP，带宽时延积）
```

| 码率 | 205 ms（常态） | 900 ms（实测尖峰） | 1356 ms（实测最大） |
|---|---|---|---|
| PCM16 16 kHz = 31.2 KB/s | 6.4 KB | 28.1 KB | **42.4 KB** |
| Opus 24 kbps = 2.9 KB/s | 0.6 KB | 2.6 KB | **4.0 KB** |

设备账：空闲堆 25 KB，**采集期间仅 14–21 KB**。
PCM 扛住最坏抖动需 42 KB —— 永远不可能；Opus 需 4 KB —— 现在就有。

**所以 Opus 不是"优化"，是把"内存额度"与"线路抖动"这两件事解耦的唯一可行手段。**
调参无解：加大缓冲超过整机空闲堆；PCM 降到 8 kHz 只省 2 倍仍不够；C3 无 PSRAM 接口。

---

## 2. 四段计划

### 第 0 段：队列语义对齐上游（今天可做，零依赖）

**改动**
1. 待发环从"丢最新"改为**丢最旧**（与上游一致：送出的永远是最近的声音）；
2. 环深按**时间**定义而不是帧数（建议 6 帧 = 120 ms，仍为静态 BSS 3.9 KB）；
3. `get_audio` 暴露**最老帧年龄**（ms），联调时一眼看出延迟堆积；
4. **修 state 不复位的缺陷**（见 §6）：正常收轮后 `state` 永远停在 `processing`。

**判据**：PCM 路径在 RTT 平稳时仍 `dropped=0`；人为制造 300 ms 停顿后，
`最老帧年龄` 不超过环容量、恢复后不出现"越积越旧"。

**回退**：单文件改动，`git revert` 即可。

---

### 第 1 段：Opus 编码器冒烟测试（不改链路）

**目标**：只回答"这台机器能不能编码 Opus"，不碰已跑通的采集/发送。

**做法**
- 引入 `esp_opus_enc`（Espressif esp_audio_codec）；若 5.5.3 + C3 不支持，
  退回裸 `libopus`（只开 CELT 模式）；
- 配置对齐上游：16 kHz 单声道、**60 ms 帧**、complexity **0**、DTX **on**、VBR **on**、FEC off；
- 用合成的 1 kHz 音 + 真实麦克风各跑 200 帧，实测三项：
  1. `esp_opus_enc_open()` 后堆消耗（开前/开后 `heap_caps_get_free_size`）；
  2. 单帧 `esp_opus_enc_process()` 耗时（`esp_timer_get_time` 前后差）；
  3. 连续 200 帧的堆最低点（看有无泄漏）。

**判据（不满足就停，不进入第 2 段）**
- 编码器常驻后**剩余可用堆 ≥ 10 KB**；
- 单帧编码 **< 5 ms**（否则顶掉 20 ms 采集节拍）；
- 200 帧无泄漏（首末堆差 < 512 B）。

**产出**：`docs/opus-smoke-test.md` 实测表，并据此给出**结论：可行 / 不可行**。

---

### 第 2 段：Opus 正式接入

**改动**
1. PAV1 增加 `kind=5`（Opus 包），`capture.started` 增加 `"audio_codec":"opus"/"pcm"` 字段，
   服务端据此决定解码方式；PCM 保留为回退开关（`vendor.capture_codec`）；
2. 采集任务只做采集 + 入队；新增**编码任务**（独立任务，优先级低于采集）；
3. 队列按上游语义重建：编码队列 2、发送队列 ≈ 40 × 60 ms = **2.4 s**；
4. 环形缓冲改为"Opus 包队列"（每包 ~180 B，40 包 ≈ 7 KB），不再需要 PCM 大帧环；
5. 每包带 `seq` + `timestamp_ms`（供服务端做 jitter buffer 与丢包掩蔽）。

**判据**
- 按住 3 秒：`frames ≈ 50`（60 ms/帧）、`dropped=0`、`reason=released`；
- 采集中堆稳定（不出现 < 8 KB）；
- 服务端解出的音频与 PCM 版听感一致（处暑对比 ASR 文本一致即可）。

**回退**：`vendor.capture_codec=pcm` 运行期切回，无需重新烧录。

---

### 第 3 段（可选，收益最大）：音频走 UDP

**为什么排在最后**：它需要服务端开新端口 + 实现 jitter buffer，是唯一真正的架构改动，
但也是唯一能**根治**"抖动就卡死"的手段（TCP 队头阻塞）。

**改动**
1. 控制通道（TCP 18080，认证 + 命令）**完全不动**；
2. 新增音频 UDP 通道（建议 18082）：设备端 `udp_probe` 探活 → 上行 Opus 包带 `seq/timestamp`；
3. 下行（TTS 音频）同样走 UDP，设备端加 jitter buffer（建议 200–400 ms 目标深度）；
4. 丢包掩蔽：单包缺失直接跳过（60 ms 的空洞对 ASR 可容忍），或做简单 PLC。

**判据**
- 人为注入 500 ms 抖动 + 5% 丢包，ASR 仍能出正确文本；
- 断网 3 秒恢复后无需重连控制通道即可继续（音频通道独立）。

**风险与回退**：NAT/防火墙可能拦 UDP——保留"UDP 探活失败自动回落 TCP+Opus"路径。

---

## 3. 处暑侧配合清单

| 阶段 | 需要处暑做什么 |
|---|---|
| 第 0 段 | 无需改动 |
| 第 1 段 | 无需改动 |
| 第 2 段 | 按 `capture.started.audio_codec` 分支：`opus` → libopus 解码（16 kHz 单声道 60 ms 帧）；`pcm` → 原路径。注意 DTX 会造成时间戳跳变 |
| 第 3 段 | 开 UDP 端口 + jitter buffer + 丢包容忍；控制面不变 |

**长期不变的两条**（已写入 `docs/capture-uplink.md` §3）：
1. 音频连接是**会话级**的，读取器必须**常驻**（每轮只换 `turn_id`）；
2. 轮次归属以设备的 `turn_id` 为准，不要用"自己发起过的轮次表"。

---

## 4. 明天开工顺序

1. 第 0 段（30 分钟，含编译刷机与一次 2 秒按压复测）；
2. 第 1 段冒烟测试（1–2 小时，含组件接入与踩坑）；
3. 拿冒烟结果决定是否进第 2 段——**不通过就停下来重新评估**（备选：PCM 8 kHz + 硬限长句，
   或推动服务端回国降低 RTT）。

## 5. 安全与可恢复

- 全量 flash 备份：`D:\dsh-work\flash\full-backup.bin`
  （SHA256 `E1993D959ACFF4FC05182B2A3C02FA93E0E51C99B050431AB9B72008D4C1AC9`）
- 恢复：`esptool write_flash 0x0 D:\dsh-work\flash\full-backup.bin`
- 固件源码：`D:\esp\ai-passport`（本地）↔ `itsnone-liu/ai-passport-hardware` 分支
  `feat/passport-voice-windows`（远端，REST API 推送，tree SHA 校验）
- 不要提交：`server/config/devices/<id>.secret`、任何 `sk-` / `ghp-` 凭据

---

## 6. 顺手修的小缺陷（2026-11-14 夜发现）

**现象**：收轮之后 `get_status` 的 `state` 永远显示 `processing`，不再回到 `idle`。

**证据**（`main/terminal_capture.c`）：
- `finish_turn()` 在结尾 `set_state("processing")`（第 230 行）；
- 全文件只有两处 `set_state("idle")`：按键超时分支（355）与"空轮取消"分支（408）；
- **正常收轮路径（`released` / `uplink_stall` / `heap_guard` / `max_duration` / `error`）
  之后没有任何复位**，于是状态永久停在 `processing`。

**影响**：不影响功能——`terminal_capture_start()` 的守卫看的是 `s_active || s_req_start`，
不是 `s_state`，所以下一轮仍能正常起采。但**遥测失真**：外部（含联调时的我）
会把 `processing` 误读成"仍在处理上一轮"，从而怀疑设备卡住。

**修法**：在任务进入"无事可做"的等待分支前复位：

```c
if (!s_armed && !s_active && !s_req_start) {
    set_state("idle");
    xTaskNotifyWait(0, 0xffffffff, NULL, portMAX_DELAY);
    continue;
}
```

**教训（写给自己）**：状态字符串是给外部看的接口，不是内部变量。
凡是"设了不复位"的状态机字段，第一次联调就会变成误判源。