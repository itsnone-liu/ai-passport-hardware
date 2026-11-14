"""Mock TTS: a deterministic tone, so pacing and cancellation are testable.

It emits a sine wave with a per-turn amplitude so a test can prove that what the
device received belongs to *this* turn and not a stale one -- the failure mode
that "cross-talk between turns" describes, which is invisible if the audio is
constant.
"""
from __future__ import annotations

import math
import struct
from typing import AsyncIterator

SAMPLE_RATE = 16000
FRAME_BYTES = 640


def tone(seconds: float, freq: float = 440.0, amp: float = 0.3) -> bytes:
    n = int(SAMPLE_RATE * seconds)
    out = bytearray()
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


class MockTTS:
    name = "mock"

    def __init__(self, seconds: float = 0.4, freq: float = 440.0, frames_per_chunk: int = 2):
        self.seconds = seconds
        self.freq = freq
        self.frames_per_chunk = max(1, frames_per_chunk)

    async def astream(self, text: str) -> AsyncIterator[bytes]:
        audio = tone(self.seconds, self.freq)
        step = FRAME_BYTES * self.frames_per_chunk
        for off in range(0, len(audio), step):
            yield audio[off:off + step]

    def stream(self, text: str) -> AsyncIterator[bytes]:
        return self.astream(text)