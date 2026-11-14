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


@dataclass
class Config:
    host: str = "0.0.0.0"
    control_port: int = 18080
    audio_port: int = 18081
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
    bailian_voice: str = "longanlingxin"

    # dsh connector (only used when brain == "dsh"); unverified against the real
    # dsh API, see server/dsh/README.md.
    dsh_url: str = ""
    dsh_token: str = ""
    dsh_session: str = "passport"

    # Protocol timers, from docs/voice-protocol-v1.md §8.
    hello_timeout: float = 5.0
    request_timeout: float = 5.0
    capture_timeout: float = 30.0
    playback_tail_timeout: float = 10.0
    playback_stall_timeout: float = 2.0

    # How fast downlink frames are written. 1.0 = real time; tests raise it so a
    # 20-round suite does not take minutes.
    playback_speed: float = 1.0
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
                        secrets[device_id] = sf.read().strip()
                else:
                    secrets[device_id] = secret.encode()
            raw.pop("devices", None)
            for key, value in raw.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
            cfg.devices = secrets

        cfg.bailian_api_key = os.environ.get("DASHSCOPE_API_KEY", cfg.bailian_api_key)
        cfg.bailian_api_key = os.environ.get("TOKENPLAN_API_KEY", cfg.bailian_api_key)
        return cfg