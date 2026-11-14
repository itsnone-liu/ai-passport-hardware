# 上机操作手册（给接手本机的另一位 Agent）

> 本文件写给**在本机（米线 = `154.64.231.1`）上做固件/联调的另一位 Agent**。
> 目标：不用问任何人就能跑起来，也不会踩到"两边同时改同一份文件"或"抢同一个端口"。

---

## 1. 身份与访问

| 项 | 值 |
|---|---|
| 这台机器 | `RainYun-c438TDGn`，公网 `154.64.231.1`，tailnet `100.94.254.42`，内网 `172.18.0.1` |
| 登录 | `ssh root@154.64.231.1`（或 tailnet IP） |
| 认证现状 | **`/root/.ssh/authorized_keys` 为空**，只能用密码；要免密请先把公钥加进去 |
| 仓库（我的工作区） | `/root/dsh-ws/ai-passport-hardware`，分支 `feat/passport-voice-ubuntu` |
| **给你准备的独立工作区** | **`/root/dsh-ws/passport-opus-lab`**，分支 `feat/passport-opus`，配置已拷好 |

> 两个工作区**不要互相 `git checkout`**：共用一个目录会让两边的未提交改动互相覆盖。
> 你的改动请在 `passport-opus-lab` 里做，推自己的分支。

---

## 2. 服务现在跑在哪

服务已从 `nohup` 迁到 **systemd**，可重启、可看日志、开机自启：

```bash
systemctl status passport-voice          # 看状态
systemctl restart passport-voice         # 重启（设备会在 1–3 s 内自动重连）
journalctl -u passport-voice -f          # 跟日志
journalctl -u passport-voice -n 100      # 最近 100 行
```

| 端口 | 用途 | 绑定 |
|---|---|---|
| `18080` | 控制通道（JSONL） | `0.0.0.0` |
| `18081` | 音频通道（PAV1 二进制） | `0.0.0.0` |
| `18090` | 管理口（list/send/show/speak/push_frames/audio/metrics） | **仅 127.0.0.1** |

常用管理命令：

```bash
cd /root/dsh-ws/ai-passport-hardware
python3 -m server.admin list
python3 -m server.admin send psp-f53038 '{"id":900,"cmd":"ping"}'
python3 -m server.admin send psp-f53038 '{"id":901,"cmd":"get_audio"}'
python3 -m server.admin speak psp-f53038 "测试一段语音"
```

设备：`psp-f53038`（密钥在 `server/config/devices/psp-f53038.secret`，**不要提交、不要打印**）。

---

## 3. 跑测试

```bash
cd /root/dsh-ws/passport-opus-lab
python3 -m unittest discover -s server/tests -t . -v      # 65 个，约 6 s，不需要网络
python3 protocol/v1/make_vectors.py --check               # 协议向量闸门

# 需要真实后端的两个（默认 skip）
TOKENPLAN_API_KEY=$(grep -h TOKENPLAN server/config/voice.env | cut -d= -f2) \
  python3 -m unittest server.tests.test_bailian_smoke -v
PASSPORT_DSH_SMOKE=1 DEEPSEEK_API_KEY=$(grep -m1 -oP 'DEEPSEEK_API_KEY="?\K[^"]+' /root/.bashrc) \
  python3 -m unittest server.tests.test_dsh_cli -v
```

测试用**临时端口**（`control_port=0`），和线上 18080/18081 不冲突，可以随时跑。

---

## 4. 硬规矩（踩过，别再踩）

1. **不要两条通道共用一个端口**，也不要改 `PAV1` 头（16 字节、`1=PCM 2=HELLO 3=END 4=AUTH`）。
   要加 Opus 载荷，走"新增 `kind` + 版本协商"，并且先改 `docs/voice-protocol-v1.md` 与向量。
2. **轮次权威在设备**：`capture.started.turn_id` 是唯一的轮次号来源，服务端只采纳，不重新分配。
3. **跨通道顺序不可假设**：PCM 可能早于 `capture.started`，`capture.ended` 可能早于 `END` 帧。
   边界以数据流为准（`PCM* END`），事件只提供 `reason`。相关代码在 `server/service.py` 的
   `on_uplink_pcm` / `on_uplink_end` / `end_capture`，**改动前先读注释，那里写着为什么**。
4. **任何"通过"都要能指出哪个计数器在动**：`frames×50=秒数`、`push_ok+rej_*=rx`、
   `SO_SNDBUF/RTT=吞吐`。指不出计数器的"通过"下一轮必被打回。
5. **沉默先怀疑有一端没把话说完**，而不是协议不兼容；拒绝路径必须"回显 id + 明确错误码 + 关闭"。
6. 不要 `pkill -f "python3 -m server"`——那条命令会匹配到你自己的 shell（我已踩过两次）。
   停服务用 `systemctl stop passport-voice`。

---

## 5. 当前已知数字（改代码前先对一遍，别重复测）

| 项 | 值 |
|---|---|
| 上行瓶颈 | **`SO_SNDBUF / RTT`**：8 KB ÷ 219 ms ≈ **18.7 KB/s**，而 PCM16 需 **32 KB/s** |
| 抖动位置 | 设备国际出口（设备→223.5.5.5 17 ms；→8.8.8.8 平均 386 / 最大 1356 ms）；服务器侧 mdev ≤1 ms |
| 在途预算 | 设备可用 10.5 KB（SNDBUF 8 KB + 环 2.5 KB）；900 ms 尖峰需 PCM 28.8 KB、Opus 24 kbps 仅 2.7 KB |
| 设备现状 | `boot=29`，`heap≈29,904`，`block=14,848`，`auth=ok`，`audio_ready=True` |
| 下行节奏 | 领先 **120 ms** 匀速，不突发（0.9 s → stall；0.45 s → underrun） |
| 服务端 | 65 测试全绿；HEAD `abb71a2`（我的分支） |

---

## 6. 你这一轮（0–3 段）的落点

| 段 | 内容 | 我的侧需要配合的点 |
|---|---|---|
| 0 | 队列丢最旧 + 按时间定深 + 最老帧年龄 + 修 `state` 停在 `processing` | 无（我只观察 `capture.*` 语义是否变形） |
| 1 | Opus 冒烟（堆 ≥10 KB、单帧 <5 ms、200 帧无泄漏） | 无 |
| 2 | Opus 接入 | 我这边把解码接上（ffmpeg/opus → 16 kHz PCM），走现有 ASR 管线；协议升 v1.1 |
| 3 | UDP 音频 | 需要新通道语义（乱序/丢包/重排），先出协议再动代码 |

第 2 段开工前请先告诉我你选的**帧长与码率**（建议 20 ms / 24 kbps，与现有 20 ms 采集节拍对齐），
我按这个准备解码与测试向量。

---

## 7. 出事怎么退

- 服务起不来：`journalctl -u passport-voice -n 200`，多半是端口占用或 `server/config/local.json` 被改坏。
- 设备连不上：`systemctl restart passport-voice`；仍不行则让设备重启（它会从 `boot` 计数器自证）。
- 仓库回滚：`/root/dsh-ws/ai-passport-hardware-20261114-f141a3e.bundle`（完整历史，可离线还原）。
- 固件回滚：**不在这台机器上做**，按 Windows 侧的 8 MB 备份流程走。