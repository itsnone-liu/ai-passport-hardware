"""TTS adapter contract: text in, 16 kHz mono s16 PCM chunks out, as they arrive.

Streaming is part of the contract, not an optimisation: the whole point is that
the device starts speaking while the rest of the reply is still being
synthesised. An adapter that buffers the full sentence before yielding its first
byte is a protocol violation even if its audio is nicer.
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol


class TTS(Protocol):
    name: str

    def stream(self, text: str) -> AsyncIterator[bytes]:
        ...