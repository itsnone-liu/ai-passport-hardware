# 交接：上行编码换成 ADPCM，以及两个收轮缺陷

写于合并 `feat/passport-opus` 之后。分支现状：

- `feat/passport-voice-ubuntu`（你昨天 handoff 用的分支）已并入 ADPCM 工作，
  合并提交 `bb838e7`，已在 GitHub。
- 设备固件在另一仓库 `feat/passport-voice-windows`，提交 `94d89cd`。
- 服务已按你给的 systemd 方式恢复：`systemctl status passport-voice` active，
  设备重连后 `auth ok`。实验实例（`local.json` 那个 nohup 进程）已停，别重复起。

## 1. 我做了什么

上行从 PCM 16 kHz/mono（31.2 KB/s）换成 IMA ADPCM 4:1（8.4 KB/s），
kind=5，载荷 168 字节 = `u32 seq` + 164 字节块。线格式写在
`docs/voice-protocol-v1.md` §5.4，按你的规矩先有文档再有代码。

为什么不是 Opus：实测 `opus_encoder_get_size(1) = 24716 B`，还要一块连续内存，
C3 上空闲 39 KB 但最大连续块只有 28672 B，`opus_encoder_create` 直接 -7
（OPUS_ALLOC_FAIL）；塞进 USB 链路任务还会 `Stack protection fault`。
代码留在固件 `main/terminal_opus.c` 里当记录，别再去试。

## 2. 实测（不是估值）

```
设备: frames=162 dropped=4 overflows=5 seq=166 rms=63 peak=193
服务端: adpcm turn=1 blocks=162 lost=0 bad=0 (3.24 s decoded, rms=63)
        audio END for turn 1 (103680 bytes buffered)
        103680 = 162 * 640，一个字节不少
```

设备报的 `rms=63` 和服务端解码后的 `rms=63` 完全一致 —— 4:1 压缩 + 传输整条链
没把音量弄丢，这比帧计数硬。按住 3.0 秒拿到 3.24 秒音频（含 260 ms 预录）。

## 3. 顺手修掉的两个缺陷（都在你的代码里，请复核）

**a) `end_capture` 会和自己撞车。** 音频 END 帧和松手事件是两条通道，
`on_uplink_end` 和 `on_key` 会在几毫秒内同时调进来。两边都在对方设置
`STATE_PROCESSING` 之前通过了状态检查，于是第一条 `_run_turn` 把 138880 字节
缓冲拿走，第二条打一句 `has only 0 bytes; skipping ASR`。**看着像 ASR 没收到
音频，其实是自己把自己那一轮的缓冲清空了。** 修法：`_ending_turn` 闸，
在第一个 await 之前就置位。

**b) ASR 没有外层超时。** 有一轮日志停在 `pipeline start` 之后三分钟没有任何
输出，整条流水线消失且不报错：客户端的 deadline 覆盖不到 websocket 握手。
单跑客户端 2.7 s 就出结果，所以不是网络。修法：`asyncio.wait_for(..., 30s)`，
并且每轮固定留一行 `asr done in X ms text=...`，静音轮次也能看出走完了。

（顺带一提：设备侧也有一个对称的病 —— 环形缓冲满的时候 `continue` 跳过了
`drain_pending()`，而环只能靠"发出去"腾空，于是第一次满就永久自锁，
表现为 `frames=4 dropped=92`，同样伪装成"对端不肯读"。已修，见固件提交。）

## 4. 还没做的

- **stage 3 UDP 音频通道**：现在每块自带预测器+步长，丢一块只损失 20 ms，
  不需要重同步，这是换 UDP 的前提条件，已经满足。端口没动，等你定。
- **真人说话验证**：目前所有轮次的 ASR 都返回空串，因为房间是静音
  （rms 55–75）。ASR 本身是好的（单跑 2.7 s 返回）。
- 播放侧 5–10 ms 边沿斜坡、`s_sock`/`s_connected` 的锁审计，还是原样。
