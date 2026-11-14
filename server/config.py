"""Configuration for the Passport voice service.

Everything that differs between "my laptop", "the LA box" and "a China host"
lives here on purpose: moving the service is supposed to be a config change,
not a code change, because the LA box is 166 ms from Bailian while a domestic
host is ~10 ms -- a decision we expect to revisit after measuring.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict


def decode_secret(raw: bytes) -> bytes:
    """Interpret a secret exactly the way the device's `provision` does.

    Rule (agreed with the firmware, documented in the protocol §3.1):
    even length, all hex, at least 32 characters -> hex-decode to bytes;
    anything else -> use the raw bytes as they are.

    This existed as an implicit assumption on both sides, which is the worst
    kind: the service read the 64-character hex *text* as the key while the
    device decoded it to 32 bytes, and the only visible symptom was
    `auth_failed` with no hint as to which half was wrong.
    """
    text = raw.strip()
    as_text = text.decode("ascii", "replace")
    looks_hex = (len(text) >= 32 and len(text) % 2 == 0
                 and all(c in "0123456789abcdefABCDEF" for c in as_text))
    if looks_hex:
        try:
            return bytes.fromhex(as_text)
        except ValueError:
            return text
    return text


@dataclass
class Config:
    host: str = "0.0.0.0"
    control_port: int = 18080
    audio_port: int = 18081
    # 收轮时给 UDP 尾巴的等待（秒）。END 从 TCP 来，最后几包 UDP 可能
    # 还在路上；120 ms 是实测抖动上界，再大就会明显拖慢回答。
    udp_tail_ms: float = 120.0
    # device_id -> shared secret (bytes). Never the cardid: that identifies the
    # device to FoloToy, it does not authenticate it to us.
    devices: Dict[str, bytes] = field(default_factory=dict)

    # Backends: "mock" keeps the whole loop local and deterministic; "bailian"
    # uses the subscription models verified on 2026-11-14.
    asr: str = "mock"
    tts: str = "mock"
    brain: str = "mock"

    bailian_api_key: str = ""
    bailian_base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com"
    bailian_asr_model: str = "qwen-audio-3.0-realtime-plus"
    bailian_tts_model: str = "qwen-audio-3.0-tts-plus"
    bailian_llm_model: str = "deepseek-v4.1-flash-1017"   # 对话用快模型：ASR 已经吃掉 3 s，LLM 不能再添
    bailian_voice: str = "longanlingxin"

    # dsh connector (only used when brain == "dsh"); unverified against the real
    # dsh API, see server/dsh/README.md.
    dsh_url: str = ""
    dsh_token: str = ""
    dsh_session: str = "passport"
    # One-shot CLI brain (documented entry point, but a process per turn).
    dsh_binary: str = "dsh"
    dsh_profile: str = "headless"

    # Localhost-only admin socket (JSONL): list sessions, send a raw command to a
    # device, push show_text/speak. 0 disables it. Never bind this publicly.
    admin_port: int = 0

    # Compatibility escape hatch (protocol §11): accept a control connection that
    # never says hello, i.e. firmware from before W2-2. Dev only -- anonymous
    # sessions get no audio and are logged loudly. Never enable on a public host.
    allow_anonymous: bool = False

    # Dev-only: count PAV1 PCM frames that do not belong to a turn, so firmware
    # can prove frame boundaries end to end (vendor.uplink_test) before
    # capture.start exists. Counted, never played, never fed to ASR.
    dev_audio_probe: bool = False

    # Protocol timers, from docs/voice-protocol-v1.md §8.
    hello_timeout: float = 5.0
    request_timeout: float = 5.0
    capture_timeout: float = 30.0
    # After `key release` the device ends the turn itself (firmware d7cfe32).
    # Older builds wait for capture.stop instead; this is how long we wait for
    # the device's own capture.ended before assuming it wants the legacy path.
    capture_release_timeout: float = 1.5
    playback_tail_timeout: float = 10.0
    playback_stall_timeout: float = 2.0

    # How fast downlink frames are written. 1.0 = real time; tests raise it so a
    # 20-round suite does not take minutes.
    playback_speed: float = 1.0
    # How far ahead of the device's speaker we keep the stream.
    #
    # Measured on the real link (205 ms RTT, spikes to ~900 ms): a lead of 0.45 s
    # or more makes the pacer burst, which overruns the device's receive path --
    # its ring then drains and playback underruns. At 0.12 s the same reply plays
    # through 114/114 frames with zero underruns. Small lead, steady rate: the
    # device consumes ~50-65 frames/s happily but cannot absorb bursts.
    playback_lead: float = 0.12
    max_capture_bytes: int = 32000 * 30

    @classmethod
    def from_env(cls, path: str = "") -> "Config":
        cfg = cls()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            secrets = {}
            for device_id, entry in raw.pop("devices", {}).items():
                secret = entry.get("secret", "")
                if secret.startswith("file:"):
                    with open(secret[5:], "rb") as sf:
                        secrets[device_id] = decode_secret(sf.read())
                else:
                    secrets[device_id] = decode_secret(secret.encode())
            raw.pop("devices", None)
            for key, value in raw.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
            cfg.devices = secrets

        cfg.bailian_api_key = os.environ.get("DASHSCOPE_API_KEY", cfg.bailian_api_key)
        cfg.bailian_api_key = os.environ.get("TOKENPLAN_API_KEY", cfg.bailian_api_key)
        return cfg