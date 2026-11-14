# `server/` — Ubuntu 侧语音与控制服务

Passport 拨过来的两条通道（控制 JSONL + 音频 PAV1）的正式服务端，以及跑在主机上的
**mock Passport**，用来在没有真机时贯通全链路。

> 归属：Ubuntu Agent。`firmware/` 属 Windows Agent，本目录不碰固件。

## 现状

| 模块 | 状态 |
|---|---|
| `pav1.py` | ✅ PAV1 参考实现：组帧/拆帧、认证 HMAC、会话闸门 |
| `service.py` | ✅ 两条通道 + 轮次状态机 + 打断 + 断线清理 + 四项时延埋点 |
| `mock_device.py` | ✅ 假 Passport（同一状态机：按键→等 capture.start→推流→END→收播） |
| `asr/bailian.py` | ✅ `qwen-audio-3.0-realtime-plus`（OpenAI-Realtime 兼容 WS 流式） |
| `tts/bailian.py` | ✅ `qwen-audio-3.0-tts-plus`（SSE 流式，直接出 16k mono PCM） |
| `dsh/mock.py` | ✅ 确定性回复 |
| `dsh/http.py` | ⚠️ 未对真实 dsh 验证，见 `dsh/README.md` |
| `__main__.py` | ✅ CLI 入口，mock 与真实后端可切换 |

## 跑起来

```bash
pip install -r server/requirements.txt          # 仅真实后端需要

# 全 mock：不需要网络、不需要 key，3 秒内起
python3 -m server --device psp-00a1b2=devsecret --control-port 18080 --audio-port 18081

# 真实百炼（AI 部分），dsh 仍用 mock
TOKENPLAN_API_KEY=sk-sp-... python3 -m server \
    --device psp-00a1b2=devsecret --asr bailian --tts bailian

# 用配置模板
cp server/config/example.json server/config/local.json   # 该文件已在 .gitignore
python3 -m server --config server/config/local.json
```

## 跑测试

```bash
python3 -m unittest discover -s server/tests -t . -v    # 30 个用例，全 mock，约 3 s
TOKENPLAN_API_KEY=... python3 -m unittest server.tests.test_bailian_smoke -v
python3 protocol/v1/make_vectors.py --check              # 合同没被手改
```

覆盖：20 轮连续对话不串音、打断、认证失败、未认证命令、超长行、音频通道垃圾字节、
断线重连（新 session、turn 从 1 重开）、设备中途消失、主动播报、时延埋点、
TTS→ASR 真实往返。

## 设备侧怎么对接（给 Windows Agent 的最小信息）

1. 控制通道先 `hello` 再 `auth`（HMAC 见协议 §3.2）；未认证时除这两条外一律 `unauthorized`。
2. 音频通道首帧 `HELLO`（带控制通道给的 `session_id`），服务端回 `server_nonce`，
   再发 `AUTH` 帧；之后才允许 PCM。
3. 按键 → 等 `capture.start`（服务端分配 `turn_id`）→ 推 PCM → 收到 `capture.stop`
   发 `END` 帧并上报 `capture.ended`。
4. 播放：收到 `playback.start` → 预缓冲 300 ms → 收到 `END` 帧播完 → 上报
   `playback.ended{reason:"completed"}`；被打断则 `cancelled`。

## 约定

- 依赖只有 `websockets` + `aiohttp`，且只在真实后端用；其余是标准库。
- 密钥：`server/config/devices/<device_id>.secret`（0600），已在 `.gitignore`。
- 日志不出现 `secret`/`mac`/Wi-Fi `password`；默认不落原始音频。
- 时延埋点：`speech_end → asr → dsh_first → tts_first`，见 `TurnMetrics`。