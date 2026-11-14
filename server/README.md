# `server/` — Ubuntu 侧语音与控制服务

Passport 拨过来的两条通道（控制 JSONL + 音频 PAV1）的正式服务端，以及跑在主机上的
**mock Passport**，用来在没有真机时贯通全链路。

> 归属：Ubuntu Agent。`firmware/` 属 Windows Agent，本目录不碰固件。

## 现状

| 模块 | 状态 |
|---|---|
| `pav1.py` | ✅ PAV1 参考实现：组帧/拆帧、认证 HMAC、会话闸门 |
| `tests/test_pav1.py` | ✅ 用 `protocol/v1/vectors.json` 做一致性测试（15 用例全绿） |
| 控制服务 `control.py` | ⏳ 下一步 |
| 音频服务 `audio.py` | ⏳ |
| ASR 适配器 `asr/` | ⏳ 目标后端：百炼 `qwen-audio-3.0-realtime-plus`（WS 流式） |
| TTS 适配器 `tts/` | ⏳ 目标后端：百炼 `qwen-audio-3.0-tts-plus`（SSE 流式 PCM） |
| dsh 连接器 `dsh/` | ⏳ |
| mock 设备 `mock_device.py` | ⏳ |

## 跑测试

```bash
# PAV1 与向量的字节级一致性（无依赖，纯标准库）
python3 -m unittest discover -s server/tests -t server -v

# 向量是否被手改（禁止手改；只能由生成器重建）
python3 protocol/v1/make_vectors.py --check
```

两条都应进 CI：前者保证实现没漂，后者保证合同没漂。

## 约定

- 依赖尽量只用标准库；确需第三方（`websockets` 等）时写进 `server/requirements.txt`。
- 密钥：`server/config/devices/<device_id>.secret`，0600，已在 `.gitignore` 里，永不入库。
- 日志不得出现 `secret` / `mac` / Wi-Fi `password`，默认不落原始音频。
- 时延埋点统一记四个点：`speech_end → asr_final → dsh_first → tts_first_frame`。