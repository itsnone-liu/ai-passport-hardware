"""Bailian realtime ASR adapter (`qwen-audio-3.0-realtime-plus`).

This is the OpenAI-Realtime-compatible WebSocket API, which is the reason the
protocol can avoid publishing device audio at a public URL: the one-shot
`qwen-audio-3.0-asr-flash` model only accepts an HTTPS media URL on the
subscription gateway, and hosting a URL for the user's own speech is exactly the
kind of leak the contract forbids.

Verified 2026-11-14 (LA host, 166 ms RTT to cn-beijing):
    session setup        0.80 s
    first partial        0.50 s after audio starts
    final transcript     0.50 s after the last frame

The device has already finished speaking by the time we call this, so the audio
is pushed faster than real time: waiting out a 4 s utterance would add 4 s of
pure latency for no benefit. Frames still go one at a time because the server
side is a streaming model, not a blob uploader.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Optional

import websockets

from server.asr.base import Transcript

SAMPLE_RATE = 16000
FRAME_BYTES = 640  # 20 ms


class BailianRealtimeASR:
    name = "bailian-realtime"

    def __init__(self, api_key: str, base_url: str, model: str,
                 language: str = "zh", speed: float = 4.0, timeout: float = 20.0):
        if not api_key:
            raise ValueError("bailian ASR needs api_key")
        self.api_key = api_key
        self.model = model
        self.language = language
        self.speed = max(1.0, speed)
        self.timeout = timeout
        ws_base = base_url.rstrip("/")
        if ws_base.startswith("https://"):
            ws_base = "wss://" + ws_base[len("https://"):]
        elif ws_base.startswith("http://"):
            ws_base = "ws://" + ws_base[len("http://"):]
        self.url = f"{ws_base}/api-ws/v1/realtime?model={model}"

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"realtime ASR expects {SAMPLE_RATE} Hz, got {sample_rate}")
        t0 = time.monotonic()
        headers = {"Authorization": f"Bearer {self.api_key}", "OpenAI-Beta": "realtime=v1"}
        final: Optional[str] = None
        async with websockets.connect(self.url, additional_headers=headers,
                                      open_timeout=10, max_size=None,
                                      ping_interval=None) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                    "input_audio_format": "pcm16",
                    "input_audio_transcription": {"model": self.model,
                                                  "language": self.language},
                    "turn_detection": None,
                },
            }))
            delay = 0.02 / self.speed
            for off in range(0, len(pcm), FRAME_BYTES):
                chunk = pcm[off:off + FRAME_BYTES]
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                }))
                await asyncio.sleep(delay)
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

            deadline = time.monotonic() + self.timeout
            while final is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("bailian ASR produced no final transcript")
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                ev = json.loads(raw)
                kind = ev.get("type", "")
                if kind == "conversation.item.input_audio_transcription.completed":
                    final = ev.get("transcript", "")
                elif kind == "error":
                    raise RuntimeError(f"bailian ASR error: {ev.get('error')}")

        return Transcript(text=(final or "").strip(), language=self.language,
                          latency_ms=int((time.monotonic() - t0) * 1000), backend=self.name)