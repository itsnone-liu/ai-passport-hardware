"""Mock ASR: deterministic, no network, so the pipeline can be tested anywhere.

It echoes how much audio it received rather than pretending to understand it --
a fake that invents plausible sentences makes failures look like successes.
"""
from __future__ import annotations

import time

from server.asr.base import Transcript


class MockASR:
    name = "mock"

    def __init__(self, text: str = "现在几点了"):
        self.text = text
        self.calls = 0

    async def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> Transcript:
        t0 = time.monotonic()
        self.calls += 1
        seconds = len(pcm) / (sample_rate * 2)
        if len(pcm) == 0:
            return Transcript(text="", latency_ms=0, backend=self.name)
        return Transcript(text=self.text, language="zh",
                          latency_ms=int((time.monotonic() - t0) * 1000),
                          backend=self.name)