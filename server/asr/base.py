"""ASR adapter contract.

One method, one unit: whole utterance in, text out. Streaming partials are a
later optimisation -- the device lets go of the key before anything is sent, so
there is no partial to show the user yet, and a batch call keeps the interface
honest (the realtime adapter opens a WebSocket internally, the mock returns a
canned line; the pipeline cannot tell them apart).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Transcript:
    text: str
    language: str = "zh"
    latency_ms: int = 0
    backend: str = ""


class ASR(Protocol):
    name: str

    async def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> Transcript:
        ...