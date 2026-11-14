"""Bailian streaming TTS adapter (`qwen-audio-3.0-tts-plus`).

Endpoint and shape verified 2026-11-14 against the Token Plan subscription:

    POST {base}/api/v1/services/audio/tts/SpeechSynthesizer
    X-DashScope-SSE: enable
    {"model": "...", "input": {"text": ..., "voice": ...},
     "parameters": {"format": "pcm", "sample_rate": 16000}}

The SSE stream sends `sentence-begin` first and then `sentence-synthesis` events
whose `output.audio.data` is base64 PCM. Asking for 16000 Hz directly means the
device never sees a foreign sample rate, and no resampling happens anywhere.

Measured on the LA host: sentence-begin 0.94 s, first audio chunk 1.10 s, and
6.8 s of audio produced in 2.28 s -- faster than real time, which is what makes
"start speaking while the rest is still generating" work.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import AsyncIterator

import aiohttp

SAMPLE_RATE = 16000


class BailianTTS:
    name = "bailian-sse"

    def __init__(self, api_key: str, base_url: str, model: str, voice: str,
                 timeout: float = 30.0):
        if not api_key:
            raise ValueError("bailian TTS needs api_key")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.timeout = timeout
        self.url = f"{base_url.rstrip('/')}/api/v1/services/audio/tts/SpeechSynthesizer"

    async def astream(self, text: str) -> AsyncIterator[bytes]:
        body = {
            "model": self.model,
            "input": {"text": text, "voice": self.voice},
            "parameters": {"format": "pcm", "sample_rate": SAMPLE_RATE},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-SSE": "enable",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(self.url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:300]
                    raise RuntimeError(f"bailian TTS HTTP {resp.status}: {detail}")
                async for raw in resp.content:
                    line = raw.strip()
                    if not line.startswith(b"data:"):
                        continue
                    try:
                        ev = json.loads(line[5:])
                    except json.JSONDecodeError:
                        continue
                    output = ev.get("output") or {}
                    data = (output.get("audio") or {}).get("data")
                    if data:
                        yield base64.b64decode(data)
                    if output.get("finish_reason") in ("stop", "interrupted"):
                        return

    def stream(self, text: str):
        return self.astream(text)


async def synthesize_to_bytes(adapter: BailianTTS, text: str) -> bytes:
    """Convenience for tests/tools: collect a full reply into one PCM buffer."""
    chunks = [chunk async for chunk in adapter.astream(text)]
    return b"".join(chunks)
