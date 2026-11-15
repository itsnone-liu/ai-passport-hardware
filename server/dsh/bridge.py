"""Brain adapter: talk to the Feishu bridge's voice socket.

This is the P2 piece. The device turn is
    capture → ASR → **this adapter** → (bridge → same dsh session) → TTS → playback

Why go through the bridge instead of a direct model call: the conversation has to
land in the same session the user types into — same memory, same tools, same
permissions — and the bridge is the only owner of that session. Measured P0
result: headless dsh delivers `text` per step (one blob), while the bridge's
session feed carries real `text-delta`s, which is what makes sentence-level TTS
possible.

Wire protocol (owned by `dsh-feishu/src/voice_in.js`):
    → {"id":N,"op":"ask","text":"..."}
    ← {"type":"ack","id":N,...}
    ← {"type":"delta","id":N,"text":"..."}
    ← {"type":"done","id":N,"cancelled":false}
    ← {"type":"error","id":N,"message":"..."}

Design notes:
- Deltas are accumulated and re-cut at sentence boundaries. TTS needs whole
  sentences (prosody), the bridge sends fragments (tokens).
- Cancellation propagates: if the consumer stops iterating (the user pressed the
  button), we send `cancel` so the bridge stops generating instead of talking to
  an empty room.
- Every failure is a `BrainUnavailable` with a human-sized message, because the
  device needs to say *something* rather than go silent.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import AsyncIterator, Optional

# Sentence terminators, kept together with the trailing quote/bracket forms that
# Chinese speech commonly ends on ("好的。」").
_TERMINATORS = "。！？；!?;\n"


class BrainUnavailable(RuntimeError):
    """The bridge is not reachable / not answering; speak an apology, not silence."""


def split_sentences(buf: str, *, min_len: int = 2) -> tuple[list[str], str]:
    """Cut complete sentences off the front of `buf`; return (sentences, rest).

    `min_len` avoids emitting a lone "。" as its own utterance when the model
    streams punctuation separately from the words before it.
    """
    out: list[str] = []
    start = 0
    for i, ch in enumerate(buf):
        if ch in _TERMINATORS:
            piece = buf[start:i + 1].strip()
            if len(piece) >= min_len:
                out.append(piece)
                start = i + 1
    return out, buf[start:]


class BridgeBrain:
    """Streams sentence-sized text from the bridge's voice socket."""

    name = "bridge"

    def __init__(self, socket_path: str, *, timeout: float = 180.0,
                 first_event_timeout: float = 60.0, connect_retries: int = 2,
                 connect_retry_delay: float = 0.5):
        if not socket_path:
            raise ValueError("bridge brain needs a socket path")
        self.socket_path = socket_path
        self.timeout = timeout
        self.first_event_timeout = first_event_timeout
        self.connect_retries = connect_retries
        self.connect_retry_delay = connect_retry_delay
        self._seq = 0
        self.last_latency_ms: Optional[int] = None

    # -- connection --------------------------------------------------------
    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last: Optional[Exception] = None
        for attempt in range(self.connect_retries + 1):
            if not os.path.exists(self.socket_path):
                last = FileNotFoundError(self.socket_path)
            else:
                try:
                    return await asyncio.open_unix_connection(self.socket_path)
                except OSError as exc:            # noqa: PERF203 - retry is the point
                    last = exc
            if attempt < self.connect_retries:
                await asyncio.sleep(self.connect_retry_delay)
        raise BrainUnavailable(
            f"语音入口不可用（{self.socket_path}）：{type(last).__name__}"
            if last else "语音入口不可用")

    # -- main path ---------------------------------------------------------
    async def astream(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        reader, writer = await self._connect()
        self._seq += 1
        rid = self._seq
        started = time.monotonic()
        buf = ""
        first_seen = False
        finished = False

        try:
            writer.write((json.dumps({"id": rid, "op": "ask", "text": text},
                                     ensure_ascii=False) + "\n").encode())
            await writer.drain()

            deadline = started + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrainUnavailable("语音入口超时未答")
                wait = self.first_event_timeout if not first_seen else remaining
                try:
                    line = await asyncio.wait_for(reader.readline(), min(remaining, wait))
                except asyncio.TimeoutError as exc:
                    raise BrainUnavailable(
                        "语音入口没有回应" if not first_seen else "语音入口中途停住") from exc
                if not line:
                    raise BrainUnavailable("语音入口断开了连接")
                first_seen = True
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = msg.get("type")
                if kind == "delta":
                    buf += str(msg.get("text") or "")
                    sentences, buf = split_sentences(buf)
                    for s in sentences:
                        yield s
                elif kind == "done":
                    finished = True
                    if buf.strip():
                        yield buf.strip()
                    self.last_latency_ms = int((time.monotonic() - started) * 1000)
                    return
                elif kind == "error":
                    raise BrainUnavailable(str(msg.get("message") or "语音入口报错"))
                # ack / pong / anything else: ignore
        finally:
            if not finished:
                # Barge-in or an error: tell the bridge to stop generating, so a
                # cancelled turn does not keep burning tokens (or tools) for a
                # listener who has moved on.
                try:
                    writer.write((json.dumps({"id": rid, "op": "cancel"}) + "\n").encode())
                    await writer.drain()
                except Exception:                 # noqa: BLE001 - best effort
                    pass
            try:
                writer.close()
            except Exception:                     # noqa: BLE001
                pass

    def ask(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        return self.astream(text, session)