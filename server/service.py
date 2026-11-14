"""The Passport voice service: two channels, one turn state machine.

Layout mirrors docs/voice-protocol-v1.md:

    control TCP  JSONL, authenticates first, then request/response + events
    audio  TCP   PAV1 frames only, bound to an authenticated control session

The device is always the client. It presses a key, the *server* names the turn,
the device uploads, the server runs ASR -> dsh -> TTS and streams the reply back
as PCM. One turn at a time, and a turn is (session_id, turn_id) -- after a
reconnect the device starts numbering from 1 again, so a bare turn_id would let
a stale frame address the new conversation.

Run:  python3 -m server --config server/config/example.json
Mock mode needs no network or API key at all.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from server import adpcm, audio_udp, pav1
from server.config import Config

log = logging.getLogger("passport.service")

STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_PROCESSING = "processing"
STATE_SPEAKING = "speaking"
STATE_ERROR = "error"

CONTROL_LINE_MAX_IN = pav1.CONTROL_LINE_MAX_IN
SERVER_ID_BASE = 1000          # server-owned request ids live above device events


def tune_socket(writer: asyncio.StreamWriter) -> None:
    """Server-side keepalive, the mirror of the firmware fix.

    Without it a device that loses Wi-Fi (no FIN, no RST) leaves a session that
    the kernel considers alive for ~2 hours: the exact failure the Windows side
    found on the device, seen from this side. 30 s idle + 3 x 10 s probes means a
    dead peer is noticed inside about a minute.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class ProtocolViolation(Exception):
    def __init__(self, code: str, detail: str = "", fatal: bool = True):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.fatal = fatal


@dataclass
class TurnMetrics:
    """The four latency points the workplan asks to record, plus wall clock."""
    turn_id: int = 0
    speech_end_ms: Optional[int] = None      # capture.ended received
    asr_ms: Optional[int] = None             # final transcript ready
    dsh_first_ms: Optional[int] = None       # first reply chunk
    tts_first_ms: Optional[int] = None       # first audio frame written
    done_ms: Optional[int] = None            # playback.ended
    cancelled: bool = False
    upload_bytes: int = 0
    downlink_bytes: int = 0
    text: str = ""

    def as_dict(self) -> Dict[str, Any]:
        def delta(a: Optional[int], b: Optional[int]) -> Optional[int]:
            return None if a is None or b is None else b - a
        return {
            "turn": self.turn_id,
            "speech_end_to_asr": delta(self.speech_end_ms, self.asr_ms),
            "asr_to_dsh": delta(self.asr_ms, self.dsh_first_ms),
            "dsh_to_tts": delta(self.dsh_first_ms, self.tts_first_ms),
            "speech_end_to_first_audio": delta(self.speech_end_ms, self.tts_first_ms),
            "total": delta(self.speech_end_ms, self.done_ms),
            "cancelled": self.cancelled,
            "upload_bytes": self.upload_bytes,
            "downlink_bytes": self.downlink_bytes,
            "text": self.text,
        }


class ControlLink:
    """One JSONL control connection: authentication, requests, events."""

    def __init__(self, session: "Session", reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.session = session
        self.reader = reader
        self.writer = writer
        self.pending: Dict[int, asyncio.Future] = {}
        self.unauthorized_attempts = 0
        self.closed = False

    async def send(self, obj: Dict[str, Any]) -> None:
        if self.closed:
            return
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line) > pav1.CONTROL_LINE_MAX_OUT:
            # Contract: never emit a reply the peer cannot parse. A command that
            # cannot fit is a bug in the caller, not something to truncate.
            log.warning("dropping oversized control reply (%d bytes)", len(line))
            return
        try:
            self.writer.write(line.encode())
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self.closed = True

    async def request(self, cmd: str, timeout: float, **fields: Any) -> Dict[str, Any]:
        """Send a command and wait for its reply; ids are server-allocated."""
        rid = self.session.next_server_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self.send({"id": rid, "cmd": cmd, **fields})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(rid, None)

    async def read_loop(self) -> None:
        try:
            while True:
                raw = await self.reader.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n") and len(raw) >= CONTROL_LINE_MAX_IN:
                    # Contract: a line that cannot fit is dropped to the next
                    # newline, never half-executed.
                    await self.send({"id": 0, "ok": False, "err": "line_too_long"})
                    continue
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if len(text) > CONTROL_LINE_MAX_IN:
                    await self.send({"id": 0, "ok": False, "err": "line_too_long"})
                    continue
                await self.session.on_control_line(self, text)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        self.closed = True
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("control channel closed"))
        self.pending.clear()
        with contextlib.suppress(Exception):
            self.writer.close()


class AudioLink:
    """One PAV1 audio connection: frames in both directions for one session."""

    def __init__(self, session: "Session", reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, framer: Optional[pav1.Framer] = None):
        self.session = session
        self.reader = reader
        self.writer = writer
        # Adopt the accept path's framer: it may already hold bytes of a frame
        # that started in the same segment as the HELLO.
        self.framer = framer if framer is not None else pav1.Framer()
        self.authed = False
        self.closed = False
        self._write_lock = asyncio.Lock()
        self._bytes_read = 0
        self._last_stats = 0.0
        self._cap_send_buffer()

    def _cap_send_buffer(self) -> None:
        """Keep the kernel from lying to us about how far ahead we are.

        With the default (often 64 KB+) send buffer, writing 30 KB of PCM
        returns instantly while the frames sit in local memory; the device only
        ever sees ~its receive window. Pacing that believes its own send buffer
        is pacing a queue nobody is reading.
        """
        sock = self.writer.get_extra_info("socket")
        if sock is None:
            return
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024)
        # The kernel option is not enough: asyncio's transport keeps its own
        # buffer (64 KB by default) and drain() only waits once *that* fills, so
        # writes still return instantly and pacing keeps lying about delivery.
        with contextlib.suppress(Exception):
            self.writer.transport.set_write_buffer_limits(high=16 * 1024, low=4 * 1024)

    async def send_frame(self, kind: int, turn_id: int, payload: bytes = b"") -> None:
        if self.closed:
            return
        async with self._write_lock:
            try:
                self.writer.write(pav1.encode_frame(kind, turn_id, payload))
                await self.writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                self.closed = True

    async def send_json(self, kind: int, obj: Dict[str, Any]) -> None:
        await self.send_frame(kind, 0, json.dumps(obj, separators=(",", ":")).encode())

    async def read_loop(self) -> None:
        try:
            while True:
                chunk = await self.reader.read(4096)
                if chunk:
                    self._bytes_read += len(chunk)
                    now = asyncio.get_running_loop().time()
                    if now - self._last_stats >= 5.0:
                        self._last_stats = now
                        log.info("%s audio link alive: %d bytes read so far",
                                 self.session.device_id, self._bytes_read)
                if not chunk:
                    break
                try:
                    frames = self.framer.feed(chunk)
                except pav1.Pav1Error as exc:
                    await self.session.report_error(exc.code, exc.detail)
                    break
                for frame in frames:
                    await self._handle(frame)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            await self.close()

    async def _handle(self, frame: pav1.Frame) -> None:
        if frame.kind == pav1.KIND_HELLO:
            await self.session.on_audio_hello(self, frame.json() or {})
        elif frame.kind == pav1.KIND_AUTH:
            await self.session.on_audio_auth(self, frame.json() or {})
        elif frame.kind == pav1.KIND_PCM:
            log.debug("%s audio frame turn=%d len=%d authed=%s", self.session.device_id,
                      frame.turn_id, len(frame.payload), self.authed)
            if not self.authed:
                await self.session.report_error("not_authed", "pcm before auth")
                return
            self.session.on_uplink_pcm(frame.turn_id, frame.payload)
        elif frame.kind == pav1.KIND_ADPCM:
            if not self.authed:
                await self.session.report_error("not_authed", "adpcm before auth")
                return
            self.session.on_uplink_adpcm(frame.turn_id, frame.payload)
        elif frame.kind == pav1.KIND_END:
            if not self.authed:
                await self.session.report_error("not_authed", "end before auth")
                return
            await self.session.on_uplink_end(frame.turn_id)

    async def close(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            self.writer.close()


class Session:
    """One device conversation: control + audio + the current turn."""

    

    def __init__(self, service: "Service", device_id: str, session_id: str):
        self.service = service
        self.cfg = service.cfg
        self.device_id = device_id
        self.session_id = session_id
        self.control: Optional[ControlLink] = None
        self.audio: Optional[AudioLink] = None
        self.device_nonce = ""            # control-channel HELLO
        self.device_nonce_audio = ""      # audio-channel HELLO -- a different value
        self.server_nonce_control = ""
        self.server_nonce_audio = ""
        self.authed = False
        self.auth_mode = "hmac"
        self.state = STATE_IDLE
        self.turn_seq = 0
        self.active_turn: Optional[int] = None
        self.metrics: list[TurnMetrics] = []
        self._server_ids = SERVER_ID_BASE
        self._capture_buf = bytearray()
        self._capture_dropped = 0
        self._capture_task: Optional[asyncio.Task] = None
        self._pipeline_task: Optional[asyncio.Task] = None
        self._cancel = asyncio.Event()
        self._played_end = asyncio.Event()
        self._capture_ended = asyncio.Event()
        self._last_capture_reason = ""
        self.probe_frames = 0
        self.probe_bytes = 0
        self.probe_last_turn = 0
        self.probe_end_frames = 0
        self._rx_frames = 0
        self._rx_bytes = 0
        self._rx_log_t0 = 0.0
        self._audio_end = asyncio.Event()
        # Frames can arrive before capture.started: the device flushes its
        # pre-roll the instant it starts, while the control event is still in
        # flight. Dropping those loses exactly the 300 ms the pre-roll exists to
        # preserve, so hold them until a turn claims them.
        self._pending_pcm: Dict[int, bytearray] = {}
        self._pending_since: Dict[int, float] = {}
        self._pace_t0: Optional[float] = None
        self._pace_log_t0: Optional[float] = None
        self._max_send_block = 0.0
        self._pace_sent = 0.0
        self.probe_end_turn = 0
        self._probe_log_at = 0.0
        self.last_pre_roll_ms = -1
        self.last_audio_codec = "pcm-s16le"
        self._adpcm_turn = 0
        self._adpcm_seq: Optional[int] = None
        self._adpcm_blocks = 0
        self._adpcm_lost = 0
        self._adpcm_bad = 0
        self.last_audio_codec = "pcm-s16le"
        self._adpcm_turn = 0
        self._ending_turn: Optional[int] = None
        # UDP 路径（第 3 段）：重排缓冲 + 自己的计数。_udp_used 决定
        # 收轮时要不要等尾巴，别给 TCP 轮次平白加 120 ms。
        self._udp = audio_udp.Reorder()
        self._udp_used = False
        self.udp_blocks = 0
        self._adpcm_seq: Optional[int] = None
        self._adpcm_blocks = 0
        self._adpcm_lost = 0
        self._adpcm_bad = 0
        self.last_capture_frames = -1
        self.last_capture_dropped = -1
        self.last_rms = -1
        self.last_peak = -1
        self._last_end_reason = ""
        self._current_prefix = b""

    # -- auth material -----------------------------------------------------
    def mac_for(self, side: str) -> str:
        """HMAC for one side of the handshake.

        The audio channel has its own server nonce (issued in its HELLO ack):
        reusing the control nonce would make an audio handshake replayable into
        a control handshake, which is the one thing nonces are here to prevent.
        """
        if side == "audio":
            # The audio channel authenticates with the pair *it* exchanged in its
            # own HELLO. Mixing one side from control and one from audio also
            # produces a valid-looking digest, which is why this was ambiguous
            # for a whole round: see protocol §3.3.
            device_nonce, server_nonce = self.device_nonce_audio, self.server_nonce_audio
        else:
            device_nonce, server_nonce = self.device_nonce, self.server_nonce_control
        return pav1.auth_mac(self.service.cfg.devices[self.device_id], side,
                             self.device_id, device_nonce, server_nonce)

    # -- ids ---------------------------------------------------------------
    def next_server_id(self) -> int:
        self._server_ids += 1
        return self._server_ids

    def next_turn_id(self) -> int:
        self.turn_seq += 1
        return self.turn_seq

    # -- control channel ---------------------------------------------------
    async def on_control_line(self, link: ControlLink, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            await link.send({"id": 0, "ok": False, "err": "bad_json"})
            return
        if not isinstance(msg, dict):
            await link.send({"id": 0, "ok": False, "err": "bad_json"})
            return

        if "ok" in msg:                      # reply to a server request
            fut = link.pending.get(msg.get("id", -1))
            if fut and not fut.done():
                fut.set_result(msg)
            return

        if msg.get("event"):
            if not self.authed:
                # ready used to be emitted on connect, i.e. before auth, which
                # collided with "everything but hello/auth is unauthorized".
                # Now ready arrives after auth; a stray event here is ignored
                # (and must never start a turn) rather than counted as abuse.
                log.debug("ignoring event before auth: %s", msg.get("event"))
                return
            # Never await event handling inline: it issues requests whose replies
            # arrive on this very read loop, so awaiting would deadlock the link.
            asyncio.create_task(self.on_device_event(msg))
            return

        cmd = msg.get("cmd")
        if cmd == "hello":
            await self._hello(link, msg)
            return
        if cmd == "auth":
            await self._auth(link, msg)
            return
        if not self.authed:
            link.unauthorized_attempts += 1
            await link.send({"id": msg.get("id", 0), "ok": False, "err": "unauthorized"})
            if link.unauthorized_attempts >= 3:
                await link.close()
            return
        await link.send({"id": msg.get("id", 0), "ok": False, "err": "unknown_cmd"})

    async def _hello(self, link: ControlLink, msg: Dict[str, Any]) -> None:
        rid = msg.get("id", 0)
        device_id = str(msg.get("device_id", ""))
        if msg.get("proto") != "v1":
            await link.send({"id": rid, "ok": False, "err": "unsupported_proto"})
            await link.close()
            return
        if device_id != self.device_id:
            await link.send({"id": rid, "ok": False, "err": "device_mismatch"})
            await link.close()
            return
        await link.send({
            "id": rid, "ok": True, "cmd": "hello", "proto": "v1",
            "server_nonce": self.service.new_nonce(), "session_id": self.session_id,
            "audio_port": self.service.audio_port,
            "audio_enabled": self.service.audio_enabled,
        })

    async def _auth(self, link: ControlLink, msg: Dict[str, Any]) -> None:
        rid = msg.get("id", 0)
        import hmac as _hmac
        if not _hmac.compare_digest(self.mac_for("control"), str(msg.get("mac", "")).lower()):
            await link.send({"id": rid, "ok": False, "cmd": "auth", "err": "auth_failed"})
            await link.close()
            return
        self.authed = True
        link.authed = True
        log.info("auth ok for %s session=%s", self.device_id, self.session_id[:8])
        await link.send({"id": rid, "ok": True, "cmd": "auth",
                         "server_mac": self.mac_for("server")})

    async def on_device_event(self, msg: Dict[str, Any]) -> None:
        ev = msg.get("event")
        if ev == "key":
            await self.on_key(str(msg.get("btn", "")), str(msg.get("ev", "")))
        elif ev == "capture.started":
            # pre_roll_ms is the *measured* pre-roll, 0 when there was nothing to
            # replay. Reporting a nominal 300 would make the server expect audio
            # that never existed.
            self.last_pre_roll_ms = int(msg.get("pre_roll_ms", -1))
            self.last_audio_codec = str(msg.get("audio_codec", "pcm-s16le"))
            # The device owns turn ids: it can start a turn by itself on a key
            # press. Adopting its id is what makes those frames land in the
            # pipeline instead of being dropped as "no such turn" -- the bug that
            # cost one round of "no PCM reached the server".
            dev_turn = msg.get("turn_id")
            if dev_turn is not None and int(dev_turn) != self.active_turn:
                log.info("%s adopting device turn %s (server had %s)", self.device_id,
                         dev_turn, self.active_turn)
            self.active_turn = int(dev_turn) if dev_turn is not None else self.active_turn
            early = self._pending_pcm.pop(self.active_turn, None)
            if early:
                self._capture_buf += early
                self._pending_since.pop(self.active_turn, None)
                log.info("%s claimed %d bytes of pre-arrival audio for turn %d",
                         self.device_id, len(early), self.active_turn)
            if self.state in (STATE_IDLE, STATE_LISTENING):
                self.state = STATE_LISTENING
                if self._capture_ended.is_set():
                    self._capture_ended = asyncio.Event()
            log.info("%s capture.started turn=%s pre_roll_ms=%s codec=%s",
                     self.device_id, self.active_turn, self.last_pre_roll_ms,
                     self.last_audio_codec)
        elif ev == "capture.ended":
            self._last_capture_reason = str(msg.get("reason", "")) or "released"
            if "frames" in msg:
                self.last_capture_frames = int(msg.get("frames", -1))
            if "dropped" in msg:
                self.last_capture_dropped = int(msg.get("dropped", -1))
            # rms/peak prove the frames carry sound, not silence: frame counts
            # stay perfect on a stream of zeros.
            self.last_rms = int(msg.get("rms", -1))
            self.last_peak = int(msg.get("peak", -1))
            log.info("%s capture.ended turn=%s reason=%s frames=%s dropped=%s rms=%s peak=%s",
                     self.device_id, msg.get("turn_id"), self._last_capture_reason,
                     self.last_capture_frames, self.last_capture_dropped,
                     self.last_rms, self.last_peak)
            self._capture_ended.set()
            # The device ends the turn on release; the server no longer asks.
            if self.state == STATE_LISTENING:
                asyncio.create_task(self.end_capture(self._last_capture_reason))
        elif ev == "playback.ended":
            self._last_end_reason = str(msg.get("reason", ""))
            self._played_end.set()
        elif ev == "error":
            log.warning("device error: %s %s", msg.get("code"), msg.get("detail", ""))

    def on_transport_lost(self, why: str = "transport lost") -> None:
        """The peer disappeared (or killed the session deliberately).

        scan/set_net make the device drop both sockets, so this is a normal path,
        not an exception: end the turn, release every waiter, drop the queue.
        """
        log.info("%s: %s (state=%s turn=%s)", self.device_id, why, self.state,
                 self.active_turn)
        self._last_end_reason = "cancelled"
        self._played_end.set()
        self._capture_ended.set()
        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
        self.state = STATE_IDLE
        self.active_turn = None

    async def report_error(self, code: str, detail: str = "") -> None:
        if self.control and not self.control.closed:
            await self.control.send({"event": "error", "code": code.lower(),
                                     "detail": detail, "turn_id": self.active_turn})

    # -- audio channel -----------------------------------------------------
    async def on_audio_hello(self, link: AudioLink, obj: Dict[str, Any]) -> None:
        if obj.get("ok") is not None:      # HELLO frame used as an ack shape
            return
        if obj.get("session_id") != self.session_id or obj.get("device_id") != self.device_id:
            await link.send_json(pav1.KIND_HELLO, {"ok": False, "err": "session_mismatch"})
            await link.close()
            return
        self.audio = link
        self.device_nonce_audio = str(obj.get("nonce", ""))
        if not self.device_nonce_audio:
            await link.send_json(pav1.KIND_HELLO, {"ok": False, "err": "bad_frame"})
            await link.close()
            return
        self.service.note_audio_nonce(self.session_id, self.device_nonce_audio)
        self.server_nonce_audio = self.service.new_nonce()
        await link.send_json(pav1.KIND_HELLO, {"ok": True,
                                               "server_nonce": self.server_nonce_audio})

    async def on_audio_auth(self, link: AudioLink, obj: Dict[str, Any]) -> None:
        import hmac as _hmac
        if not _hmac.compare_digest(self.mac_for("audio"), str(obj.get("mac", "")).lower()):
            await link.send_json(pav1.KIND_HELLO, {"ok": False, "err": "auth_failed"})
            await link.close()
            return
        link.authed = True
        await link.send_json(pav1.KIND_HELLO, {"ok": True})

    def on_udp_adpcm(self, turn_id: int, seq: int, payload: bytes) -> None:
        """UDP 收到的 kind=5：先按 seq 重排，再交给 TCP 那条完全相同的解码路径。

        重排好的每一包直接喂给 on_uplink_adpcm —— 它本来就把'seq 缺口'
        算成丢失，所以 UDP 的丢包统计和 TCP 口径一致，不需要第二套账。
        """
        self._udp_used = True
        if self._udp.expected == 0 and not self._udp.buf and seq != 0:
            # 这一轮的起点：第一包就不是 0 说明前面真丢了，别为它永久等待。
            self._udp.reset(seq)
        for item in self._udp.push(seq, payload):
            self.udp_blocks += 1
            self.on_uplink_adpcm(turn_id, item)

    def _udp_tail(self, turn_id: int) -> None:
        """收轮前把重排缓冲吐干净。END 走 TCP，尾巴还在 UDP 路上。"""
        rest = self._udp.flush()
        for item in rest:
            self.udp_blocks += 1
            self.on_uplink_adpcm(turn_id, item)
        if self._udp_used:
            log.info("%s udp turn=%d blocks=%d %s tail=%d", self.device_id,
                     turn_id, self.udp_blocks, self._udp.stats(), len(rest))
        self._udp.reset(self._udp.expected)

    def on_uplink_adpcm(self, turn_id: int, payload: bytes) -> None:
        """kind=5：payload = [u32 seq][164 B IMA 块]。

        解成 PCM16 之后调用 on_uplink_pcm，走与 PCM 完全相同的路径——缓冲、
        轮次判定、ASR 一行都不用改。这就是"只换体格式、不换骨架"的目的。
        seq 用来算丢块/乱序：设备每轮从 0 起，所以换轮要重置基准。
        """
        try:
            seq, block = adpcm.split_payload(payload)
        except ValueError as exc:
            self._adpcm_bad += 1
            log.warning("%s bad adpcm payload: %s", self.device_id, exc)
            return
        if self._adpcm_turn != turn_id:
            self._adpcm_turn = turn_id
            self._adpcm_seq = None
        if self._adpcm_seq is not None:
            gap = (seq - self._adpcm_seq - 1) & 0xFFFFFFFF
            if 0 < gap < 1000:
                self._adpcm_lost += gap
        self._adpcm_seq = seq
        self._adpcm_blocks += 1
        self.on_uplink_pcm(turn_id, adpcm.decode_block(block))

    def on_uplink_pcm(self, turn_id: int, payload: bytes) -> None:
        now = time.monotonic()
        if self.active_turn == turn_id and self.state == STATE_LISTENING:
            self._rx_frames += 1
            self._rx_bytes += len(payload)
            if now - self._rx_log_t0 >= 1.0:
                self._rx_log_t0 = now
                log.info("%s uplink turn=%d %d frames / %d bytes (%.2f s)",
                         self.device_id, turn_id, self._rx_frames, self._rx_bytes,
                         self._rx_bytes / 32000)
        if self.active_turn != turn_id or self.state != STATE_LISTENING:
            if self.cfg.dev_audio_probe:
                # W2-3 hands us frames before capture.start exists. Count them so
                # framing can be verified now; never play them, never ASR them.
                self.probe_frames += 1
                self.probe_bytes += len(payload)
                self.service.totals["frames"] += 1
                self.service.totals["bytes"] += len(payload)
                self.probe_last_turn = turn_id
                now = time.monotonic()
                if now - self._probe_log_at >= 1.0:
                    self._probe_log_at = now
                    log.info("%s uplink probe: %d frames / %d bytes (turn %d, %.2f s)",
                             self.device_id, self.probe_frames, self.probe_bytes,
                             turn_id, self.probe_bytes / 32000)
            else:
                # Unknown turn: keep a bounded pre-roll, drop the rest.
                buf = self._pending_pcm.setdefault(turn_id, bytearray())
                log.debug("%s parking %d bytes for unclaimed turn %d", self.device_id,
                          len(payload), turn_id)
                if len(buf) < 32000 * 2:          # at most 2 s held per turn
                    buf += payload
                    self._pending_since.setdefault(turn_id, now)
                if now - self._pending_since.setdefault(turn_id, now) > 5.0:
                    self._pending_pcm.pop(turn_id, None)
                    self._pending_since.pop(turn_id, None)
            return
        if len(self._capture_buf) + len(payload) > self.cfg.max_capture_bytes:
            overflow = len(self._capture_buf) + len(payload) - self.cfg.max_capture_bytes
            del self._capture_buf[:overflow]
            self._capture_dropped += overflow
        self._capture_buf += payload

    async def on_uplink_end(self, turn_id: int) -> None:
        if self._adpcm_blocks:
            # 解码后的 RMS 要和设备 capture.ended 里的 rms 对得上：|Δ| 小说明
            # 4:1 压缩+传输整条链没把音量弄丢，这是比帧计数更硬的证据。
            log.info("%s adpcm turn=%s blocks=%d lost=%d bad=%d (%.2f s decoded, "
                     "rms=%d)", self.device_id, self._adpcm_turn,
                     self._adpcm_blocks, self._adpcm_lost, self._adpcm_bad,
                     self._adpcm_blocks * adpcm.FRAME_MS / 1000.0,
                     adpcm.pcm_rms(self._capture_buf))
            self._adpcm_blocks = self._adpcm_lost = self._adpcm_bad = 0
        if self.active_turn == turn_id:
            # The audio stream is self-delimiting: PCM* END. The control-channel
            # event can arrive before, after, or interleaved with it, so the END
            # frame -- not the event -- is what closes the turn.
            if self._udp_used:
                # 等一个'尾巴'：END 从 TCP 来，最后几包 UDP 可能晚到 20–80 ms。
                # 实测 RTT 205 ms，抖动远小于它；这个值可配。
                await asyncio.sleep(self.cfg.udp_tail_ms / 1000.0)
            self._udp_tail(turn_id)
            self._audio_end.set()
            log.info("%s audio END for turn %d (%d bytes buffered)", self.device_id,
                     turn_id, len(self._capture_buf))
            if self.active_turn == turn_id and self.state == STATE_LISTENING:
                asyncio.create_task(self.end_capture("released"))
        if self.active_turn != turn_id or self.state != STATE_LISTENING:
            if self.cfg.dev_audio_probe:
                # Firmware drove capture.start itself (USB) so this link never
                # owned the turn -- still prove the END frame arrived.
                self.probe_end_frames += 1
                self.probe_end_turn = turn_id
                self.service.totals["ends"] += 1
                log.info("%s uplink probe: END seen for turn %d", self.device_id, turn_id)
            return
        self._capture_ended.set()

    # -- key handling ------------------------------------------------------
    async def on_key(self, btn: str, ev: str) -> None:
        """press starts capture, release ends it.

        The device owns the button semantics since firmware d7cfe32: a human
        holding a button for a sentence always exceeds the 500 ms long-press
        threshold, so keying capture off `click` meant it never started at all
        -- we spent a round investigating 340 ms of room noise as if it were
        speech because of exactly that.
        """
        if btn != "ok":
            return
        if ev == "press":
            log.info("%s key press (state=%s)", self.device_id, self.state)
            if self.state == STATE_IDLE:
                await self.begin_capture()
            elif self.state == STATE_SPEAKING:
                await self.cancel_playback()
            else:
                await self.report_error("busy", f"state={self.state}")
        elif ev == "release":
            log.info("%s key release (state=%s)", self.device_id, self.state)
            if self.state == STATE_LISTENING:
                await self.end_capture("released")

    async def begin_capture(self) -> None:
        if not self.audio or not self.audio.authed:
            await self.report_error("not_supported", "audio channel not ready")
            return
        self.active_turn = self.next_turn_id()
        self.state = STATE_LISTENING
        self._capture_buf = bytearray()
        self._capture_dropped = 0
        self._capture_ended = asyncio.Event()
        self._audio_end = asyncio.Event()
        self._cancel = asyncio.Event()
        self._ending_turn = None
        log.info("%s capture.start turn=%d sent (audio_link=%s)", self.device_id,
                 self.active_turn, bool(self.audio and self.audio.authed))
        reply = await self._capture_start(self.active_turn)
        log.info("%s capture.start reply: %s", self.device_id, reply)
        if not reply.get("ok"):
            # busy == the audio channel is still dialling: retry is right.
            # Anything else (not_supported, error) means retrying is pointless.
            await self.report_error(str(reply.get("err", "capture_start_failed")),
                                    f"turn {self.active_turn}")
            self.state = STATE_IDLE
            self.active_turn = None
            return
        self._capture_task = asyncio.create_task(self._capture_watchdog(self.active_turn))

    async def _capture_start(self, turn_id: int, attempts: int = 4,
                             delay: float = 0.3) -> Dict[str, Any]:
        """capture.start with the retry the contract asks for on `busy`.

        `ready` is emitted when control auth succeeds, but the audio channel
        finishes its own handshake later, so a capture.start that lands in that
        window is answered `busy`. Treating that as fatal would abandon a
        perfectly good device at the one moment it was about to become ready.
        """
        # not_supported (audio channel still dialling), busy (previous turn closing)
        # and no_mem (device heap pressure) are all transient: retrying is right.
        # Treating any of them as fatal abandons a healthy device mid-handshake.
        retryable = {"busy", "not_supported", "no_mem"}
        for attempt in range(attempts):
            reply = await self.control.request("capture.start", self.cfg.request_timeout,
                                               turn_id=turn_id)
            if reply.get("ok") or reply.get("err") not in retryable:
                return reply
            log.info("%s capture.start %s (attempt %d/%d), retrying in %.0f ms",
                     self.device_id, reply.get("err"), attempt + 1, attempts, delay * 1000)
            await asyncio.sleep(delay)
        return {"ok": False, "err": "busy"}

    async def _capture_watchdog(self, turn_id: int) -> None:
        try:
            await asyncio.wait_for(self._capture_ended.wait(), self.cfg.capture_timeout)
        except asyncio.TimeoutError:
            if self.active_turn == turn_id and self.state == STATE_LISTENING:
                await self.end_capture(reason="max_duration")

    async def end_capture(self, reason: str = "released") -> None:
        """Close the turn: wait for the device's ending, then drain the audio.

        Two different sockets are in play and they race. `capture.ended` rides
        the control channel; the PCM and the in-band END ride the audio channel.
        Control routinely arrives first, so:
          1. wait for capture.ended (with a legacy fallback to capture.stop),
          2. keep LISTENING and wait for the audio END frame -- it is the only
             proof that every byte queued for this turn has been read,
          3. only then flip to PROCESSING and hand the buffer to ASR.
        Getting (2) and (3) the wrong way round turned a complete sentence into
        an empty transcript, which then looked like a broken ASR.
        """
        if self.state != STATE_LISTENING:
            return
        # 一次轮次只允许一条收尾流水线。音频 END 和松手事件是两条独立的通道，
        # on_uplink_end 与 on_key 会几乎同时调到这里；两条都往下走，第一条会把
        # _capture_buf 拿走跑 ASR，第二条看到 0 字节，日志里就是那句
        # "has only 0 bytes; skipping ASR"——看着像 ASR 没收到音频，其实是自己
        # 把自己那一轮的缓冲清空了。
        if self._ending_turn == self.active_turn:
            return
        self._ending_turn = self.active_turn
        if self._capture_task:
            self._capture_task.cancel()
            self._capture_task = None
        turn = self.active_turn

        # Wait for the audio END (the authoritative end-of-turn marker) while
        # still LISTENING so late frames are not dropped. capture.ended may come
        # earlier or later; only its `reason` matters here.
        if not self._audio_end.is_set():
            try:
                await asyncio.wait_for(self._audio_end.wait(),
                                       self.cfg.capture_release_timeout)
            except asyncio.TimeoutError:
                log.warning("%s no audio END for turn %s within %.1fs; using %d bytes",
                            self.device_id, turn, self.cfg.capture_release_timeout,
                            len(self._capture_buf))
                if not self._capture_ended.is_set():
                    # Legacy firmware: it only sends END when asked.
                    with contextlib.suppress(Exception):
                        await self.control.request("capture.stop",
                                                   self.cfg.request_timeout, turn_id=turn)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._audio_end.wait(), 2.0)

        self.state = STATE_PROCESSING
        reason = self._last_capture_reason or reason
        if reason == "heap_guard":
            log.warning("%s device ended the turn on its heap guard (%d frames); "
                        "processing what arrived", self.device_id, len(self._capture_buf))
        if reason in ("uplink_stall", "cap_start_timeout"):
            # The device aborted deliberately; transcribing a truncated buffer
            # produces plausible text and looks like an ASR defect.
            await self.report_error(reason, f"turn {turn}")
            await self._to_idle(turn)
            return

        await self.control.send({"event": "state", "state": STATE_PROCESSING,
                                 "turn_id": turn})
        await self.control.request("state.set", self.cfg.request_timeout,
                                   state=STATE_PROCESSING, turn_id=turn)
        self._pipeline_task = asyncio.create_task(self._run_turn(turn, reason))

    # -- pipeline ----------------------------------------------------------
    async def _run_turn(self, turn_id: int, reason: str) -> None:
        m = TurnMetrics(turn_id=turn_id, upload_bytes=len(self._capture_buf),
                        speech_end_ms=self.service.now_ms())
        m.upload_dropped = self._capture_dropped
        self.metrics.append(m)
        pcm = bytes(self._capture_buf)
        self._capture_buf = bytearray()
        log.info("%s turn %d pipeline start: %d bytes (reason=%s)",
                 self.device_id, turn_id, len(pcm), reason)
        if len(pcm) < 3200:                     # < 0.1 s: not speech, not worth a call
            log.info("%s turn %d has only %d bytes; skipping ASR", self.device_id,
                     turn_id, len(pcm))
            await self._to_idle(turn_id)
            return
        try:
            # 外面套一层超时：ASR 客户端自己虽然有 deadline，但它可能卡在
            # websockets 握手/发送上（实测服务内那一步可以一直不返回，
            # 日志停在 pipeline start 之后再无输出，整条流水线就此消失）。
            # 单跑客户端 2.7 s 就出结果，所以超时上限给 30 s 足够宽松。
            _asr_t0 = self.service.now_ms()
            transcript = await asyncio.wait_for(
                self.service.asr.transcribe(pcm),
                getattr(self.cfg, "asr_timeout", 30.0))
            m.asr_ms = self.service.now_ms()
            log.info("%s turn %d asr done in %d ms text=%r",
                     self.device_id, turn_id,
                     m.asr_ms - _asr_t0, transcript.text)
            m.text = transcript.text
            if not transcript.text:
                await self.control.request("show_text", self.cfg.request_timeout,
                                           text="(没有听清)")
                await self._to_idle(turn_id)
                return
            await self.control.request("show_text", self.cfg.request_timeout,
                                       text=transcript.text)
            await self._speak(turn_id, m)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001 - report, don't die
            log.exception("turn %s failed", turn_id)
            await self.report_error("internal", str(exc))
            await self._to_idle(turn_id)
        finally:
            m.done_ms = self.service.now_ms()

    async def _speak(self, turn_id: int, m: TurnMetrics) -> None:
        """Stream a reply: start synthesising first, announce playback second.

        Order matters for the device. Sending playback.start and only then
        calling TTS hands the device a 1.1 s silence after an announcement that
        sounds like "audio is coming" -- measured, and it is why a 300 ms
        prebuffer looked impossible to honour. Synthesising into a queue and
        announcing when the first chunk already exists makes that delay vanish
        from the device's point of view (it still has to tolerate a slow TTS, but
        the stall timer, not the prebuffer, is what covers it).
        """
        self.state = STATE_SPEAKING
        self._played_end = asyncio.Event()
        self._pace_t0 = None
        self._pace_sent = 0.0
        self._pace_log_t0 = None
        self._max_send_block = 0.0
        queue: asyncio.Queue = asyncio.Queue()
        producer_done = object()

        async def produce() -> None:
            try:
                log.info("%s speak: asking brain for turn %d", self.device_id, turn_id)
                async for chunk in self.service.brain.ask(m.text,
                                                          session=self.service.dsh_session):
                    if m.dsh_first_ms is None:
                        m.dsh_first_ms = self.service.now_ms()
                        log.info("%s speak: first text chunk at +%d ms", self.device_id,
                                 m.dsh_first_ms - m.speech_end_ms)
                    async for audio in self.service.tts.stream(chunk):
                        if self._cancel.is_set():
                            return
                        await queue.put(audio)
            except Exception as exc:                       # noqa: BLE001
                log.exception("synthesis failed")
                await queue.put(exc)
            finally:
                await queue.put(producer_done)

        producer = asyncio.create_task(produce())
        try:
            first = await queue.get()
            log.info("%s speak: first audio chunk %d B", self.device_id,
                     len(first) if not isinstance(first, Exception) else -1)
            if isinstance(first, Exception):
                raise first
            if first is producer_done:
                return
            reply = await self.control.request("playback.start", self.cfg.request_timeout,
                                               turn_id=turn_id)
            log.info("%s playback.start turn=%d -> %s", self.device_id, turn_id, reply)
            await self._write_audio(first, turn_id, m)
            while True:
                starve_t0 = asyncio.get_running_loop().time()
                item = await queue.get()
                starve = asyncio.get_running_loop().time() - starve_t0
                if starve > 0.2:
                    log.info("%s downlink starved %.0f ms waiting for TTS "
                             "(lead %.0f ms)", self.device_id, starve * 1000,
                             self.cfg.playback_lead * 1000)
                if item is producer_done or self._cancel.is_set():
                    break
                if isinstance(item, Exception):
                    raise item
                await self._write_audio(item, turn_id, m)
            await self.audio.send_frame(pav1.KIND_END, turn_id)
            log.info("%s downlink done: %d bytes, worst send block %.0f ms",
                     self.device_id, m.downlink_bytes, self._max_send_block * 1000)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._played_end.wait(),
                                       self.cfg.playback_tail_timeout)
            m.cancelled = self._last_end_reason == "cancelled"
            await self._to_idle(turn_id)
        finally:
            if not producer.done():
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer

    async def _write_audio(self, audio: bytes, turn_id: int, m: TurnMetrics) -> None:
        """Push 640 B frames, holding a lead of ~400 ms over real time.

        Pacing exactly at real time leaves zero slack for a 166 ms RTT link: one
        delayed packet and the device underruns, which it can only report as
        `audio_underrun` long after the listener heard the gap. Sending flat out
        is worse -- it fills the 1.2 s ring and half the reply is dropped.

        So: fill to LEAD_SECONDS ahead, then hold that lead. The device gets its
        300 ms prebuffer immediately, jitter is absorbed by the lead, and the
        ring stays at roughly a third of capacity.
        """
        step = pav1.FRAME_BYTES_20MS
        speed = max(0.001, self.cfg.playback_speed)
        if self._pace_t0 is None:
            self._pace_t0 = asyncio.get_running_loop().time()
            self._pace_sent = 0.0
        for off in range(0, len(audio), step):
            if self._cancel.is_set():
                return
            frame = audio[off:off + step]
            if len(frame) < step and off + step > len(audio):
                break               # partial tail is dropped, not padded into the speaker
            t_send = asyncio.get_running_loop().time()
            await self.audio.send_frame(pav1.KIND_PCM, turn_id, frame)
            blocked = asyncio.get_running_loop().time() - t_send
            if blocked > self._max_send_block:
                self._max_send_block = blocked
            if m.downlink_bytes == 0:
                log.info("%s first PCM frame turn=%d len=%d", self.device_id, turn_id,
                         len(frame))
            m.downlink_bytes += len(frame)
            if m.tts_first_ms is None:
                m.tts_first_ms = self.service.now_ms()
            self._pace_sent += len(frame) / 32000.0
            elapsed_virtual = (asyncio.get_running_loop().time() - self._pace_t0) * speed
            ahead = self._pace_sent - elapsed_virtual
            lead = self.cfg.playback_lead
            if self._pace_log_t0 is None:
                self._pace_log_t0 = asyncio.get_running_loop().time()
            if asyncio.get_running_loop().time() - self._pace_log_t0 > 1.0:
                self._pace_log_t0 = asyncio.get_running_loop().time()
                log.info("%s downlink: sent=%.2fs ahead=%.2fs (lead %.2fs) turn=%d",
                         self.device_id, self._pace_sent, ahead, lead, turn_id)
            # Fill at 2x until the lead is established, then pace at real time.
            # A single burst that dumps 2 s into a 1.2 s ring is not "filling the
            # buffer", it is overflowing it -- the device drops the excess and
            # then drains to empty, which it can only report as audio_underrun.
            if ahead >= lead:
                await asyncio.sleep(min(0.5, (ahead - lead) / speed))
            else:
                await asyncio.sleep(max(0.0, 0.01 / speed))

    async def cancel_playback(self) -> None:
        self._cancel.set()
        if self.control and not self.control.closed:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.control.request("playback.stop", self.cfg.request_timeout,
                                         turn_id=self.active_turn),
                    timeout=self.cfg.request_timeout)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._played_end.wait(), 2.0)

    async def _to_idle(self, turn_id: int) -> None:
        self.state = STATE_IDLE
        self.active_turn = None
        if self.control and not self.control.closed:
            with contextlib.suppress(Exception):
                await self.control.request("state.set", self.cfg.request_timeout,
                                           state=STATE_IDLE, turn_id=turn_id)

    # -- host-facing API ---------------------------------------------------
    async def show_text(self, text: str) -> Dict[str, Any]:
        return await self.control.request("show_text", self.cfg.request_timeout, text=text)

    async def set_volume(self, value: int) -> Dict[str, Any]:
        return await self.control.request("set_volume", self.cfg.request_timeout, value=value)

    async def set_brightness(self, value: int) -> Dict[str, Any]:
        return await self.control.request("set_brightness", self.cfg.request_timeout,
                                          value=value)

    async def get_status(self) -> Dict[str, Any]:
        """Includes the device's own heap numbers (heap/heap_min/heap_block).

        Read them instead of estimating: the budget in the protocol was written
        from an estimate that turned out ~50 KB optimistic.
        """
        return await self.control.request("get_status", self.cfg.request_timeout)

    async def scan(self) -> Dict[str, Any]:
        """Diagnostic only. **Ends the session**: the device must disconnect the
        STA to scan, which drops both TCP channels and invalidates session_id."""
        if self.state != STATE_IDLE:
            await self.cancel_playback()
            await asyncio.sleep(0.05)
        return await self.control.request("scan", timeout=15.0)

    async def set_net(self, **fields: Any) -> Dict[str, Any]:
        """Re-point the device. **Ends the session** for the same reason."""
        if self.state != STATE_IDLE:
            await self.cancel_playback()
            await asyncio.sleep(0.05)
        return await self.control.request("set_net", timeout=10.0, **fields)

    async def speak_notification(self, text: str) -> None:
        """dsh-initiated utterance: its own turn, same pipeline, no capture."""
        if self.state != STATE_IDLE:
            await self.report_error("busy", "cannot start notification while busy")
            return
        turn = self.next_turn_id()
        self.active_turn = turn
        self._cancel = asyncio.Event()
        m = TurnMetrics(turn_id=turn, speech_end_ms=self.service.now_ms())
        self.metrics.append(m)
        self._pipeline_task = asyncio.create_task(self._notification_turn(turn, text, m))

    async def _notification_turn(self, turn_id: int, text: str, m: TurnMetrics) -> None:
        try:
            m.text = text
            m.asr_ms = m.dsh_first_ms = self.service.now_ms()
            await self._speak(turn_id, m)
        except Exception as exc:                       # noqa: BLE001
            log.exception("notification failed")
            await self.report_error("internal", str(exc))
            await self._to_idle(turn_id)
        finally:
            m.done_ms = self.service.now_ms()


class Service:
    def __init__(self, cfg: Config, asr, tts, brain):
        self.cfg = cfg
        self.asr = asr
        self.tts = tts
        self.brain = brain
        self.sessions: Dict[str, Session] = {}
        self.by_session_id: Dict[str, Session] = {}
        # Live audio links by device: the socket outlives a control reconnect, so
        # it needs a home that is not a Session.
        self.audio_links: Dict[str, "AudioLink"] = {}
        self.control_port = cfg.control_port
        self.audio_port = cfg.audio_port
        self.audio_enabled = True
        # UDP 打洞寄存器：源地址 -> 设备 id。设备永远主动打洞，所以这里
        # 学到的地址就是回程地址，服务端不需要任何入站端口配置。
        self._udp_addrs: Dict[tuple, str] = {}
        self._udp_transport = None
        self.udp_hello = 0
        self.udp_datagrams = 0
        self.udp_bad = 0
        self.udp_errors = 0
        self._t0 = time.monotonic()
        self._servers: list[asyncio.AbstractServer] = []
        self._nonces: Dict[str, str] = {}
        self._seen_nonces: set[str] = set()
        # Process-lifetime counters: per-session numbers reset on every
        # reconnect, so acceptance evidence needs a total that does not.
        self.totals = {"frames": 0, "bytes": 0, "ends": 0, "sessions": 0}
        self._audio_nonces: Dict[str, str] = {}
        self.dsh_session = cfg.dsh_session

    # -- helpers -----------------------------------------------------------
    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    @staticmethod
    def new_nonce() -> str:
        return secrets.token_hex(16)

    def mac_for(self, device_id: str, side: str) -> str:
        secret = self.cfg.devices[device_id]
        return pav1.auth_mac(secret, side, device_id,
                             self._nonces.get(device_id, ""),
                             self._nonces.get(device_id + ":server", ""))

    def note_control_nonce(self, device_id: str, device_nonce: str,
                           server_nonce: str) -> None:
        if device_nonce in self._seen_nonces:
            raise PermissionError("replayed nonce")
        self._seen_nonces.add(device_nonce)
        self._nonces[device_id] = device_nonce
        self._nonces[device_id + ":server"] = server_nonce

    def note_audio_nonce(self, session_id: str, device_nonce: str) -> None:
        # Deliberately not deduplicated: the audio channel is already bound to an
        # authenticated control session and its own HMAC, and a device that
        # reuses one nonce for both channels is behaving correctly, not replaying.
        self._audio_nonces[session_id] = device_nonce

    def verify_mac(self, device_id: str, side: str, mac_hex: str) -> None:
        import hmac
        expected = self.mac_for(device_id, side)
        if not hmac.compare_digest(expected, str(mac_hex).lower()):
            raise PermissionError("hmac mismatch")

    def on_udp_datagram(self, data: bytes, addr, transport) -> None:
        """UDP 数据报入口。必须同步、必须快：慢了内核缓冲就被后面的包冲掉。"""
        self.udp_datagrams += 1
        try:
            kind, turn, payload = audio_udp.parse(data)
        except audio_udp.BadDatagram:
            self.udp_bad += 1
            return
        if kind == audio_udp.KIND_HELLO:
            dev = payload.decode("ascii", "replace").strip()
            fresh = self._udp_addrs.get(addr) != dev
            self._udp_addrs[addr] = dev
            self.udp_hello += 1
            transport.sendto(audio_udp.build(audio_udp.KIND_HELLO, 0), addr)
            if fresh:
                log.info("%s udp hello from %s:%d", dev, addr[0], addr[1])
            return
        dev = self._udp_addrs.get(addr)
        if not dev:
            # 没打过洞就发音频：当作坏包，不猜测来源。
            self.udp_bad += 1
            return
        session = self.sessions.get(dev)
        if session is None:
            self.udp_bad += 1
            return
        if kind == audio_udp.KIND_ADPCM:
            session.on_udp_adpcm(turn, int.from_bytes(payload[:4], "little"),
                                 payload)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        # Keep each server's own socket: reading self._servers[-1] after a later
        # bind silently reported the wrong port (the admin socket), which made
        # every client connect to the wrong listener.
        control_srv = await asyncio.start_server(
            self._on_control, self.cfg.host, self.cfg.control_port)
        self._servers.append(control_srv)
        self.control_port = control_srv.sockets[0].getsockname()[1]
        if self.audio_enabled:
            audio_srv = await asyncio.start_server(
                self._on_audio, self.cfg.host, self.cfg.audio_port)
            self._servers.append(audio_srv)
            self.audio_port = audio_srv.sockets[0].getsockname()[1]
        if self.audio_enabled:
            loop = asyncio.get_running_loop()
            self._udp_transport, _ = await loop.create_datagram_endpoint(
                lambda: audio_udp.DatagramProtocol(self),
                local_addr=(self.cfg.host, self.audio_port))
            log.info("audio udp on %s:%d", self.cfg.host, self.audio_port)
        if self.cfg.admin_port:
            self._servers.append(await asyncio.start_server(
                self._on_admin, "127.0.0.1", self.cfg.admin_port))
        log.info("control on %s:%d audio on %s:%d", self.cfg.host, self.control_port,
                 self.cfg.host, self.audio_port)

    async def stop(self) -> None:
        for srv in self._servers:
            srv.close()
            with contextlib.suppress(Exception):
                await srv.wait_closed()
        self._servers.clear()

    async def send_raw(self, device_id: str, obj: Dict[str, Any],
                       timeout: float = 20.0) -> Dict[str, Any]:
        """Write an arbitrary control line to a device and wait for its reply.

        The id in `obj` is preserved on purpose: a test that checks
        request/response routing has to assert the same id came back, so the
        server must not renumber it.
        """
        session = self.sessions.get(device_id)
        if session is None or session.control is None:
            raise KeyError(f"no live control session for {device_id}")
        link = session.control
        rid = obj.get("id")
        if rid is None:
            raise ValueError("admin send requires an explicit id")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        link.pending[rid] = fut
        try:
            await link.send(obj)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            link.pending.pop(rid, None)

    async def _on_admin(self, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
        """Localhost admin: one JSON object per line, one reply per line.

        Deliberately dumb and local-only. Anything that can reach this socket can
        drive a speaker in someone's room, so it binds 127.0.0.1 and never speaks
        over the network.
        """
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    writer.write(b'{"ok":false,"err":"bad_json"}\n')
                    await writer.drain()
                    continue
                resp = await self._admin_handle(req)
                writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _admin_handle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        op = req.get("op")
        try:
            if op == "list":
                return {"ok": True, "sessions": [{
                    "device_id": s.device_id, "session_id": s.session_id,
                    "state": s.state, "turn_id": s.active_turn, "authed": s.authed,
                    "audio": bool(s.audio and s.audio.authed),
                    "auth": s.auth_mode,
                    "control_open": bool(s.control and not s.control.closed),
                    "turns_done": len(s.metrics),
                } for s in self.sessions.values()]}
            if op == "send":
                payload = req.get("json") or {}
                reply = await self.send_raw(str(req.get("device", "")), payload,
                                            timeout=float(req.get("timeout", 20)))
                return {"ok": True, "reply": reply}
            if op == "devices":
                # Ops view: which devices are known and how the secret was read.
                # "64 chars, still text" was a real bug that only auth_failed
                # reported; this turns it into one admin call.
                return {"ok": True, "devices": [{
                    "device_id": dev,
                    "key_bytes": len(key),
                    # A 64-byte all-hex key is the signature of "we read the hex
                    # text instead of decoding it" -- the bug that cost this
                    # project an auth round trip.
                    "looks_like_undecoded_hex": (len(key) == 64 and all(
                        c in b"0123456789abcdefABCDEF" for c in key)),
                } for dev, key in self.cfg.devices.items()]}
            if op == "push_frames":
                # Bench aid: push raw PAV1 frames with no playback.start, so the
                # device can be measured reading the socket in isolation.
                session = self.sessions[str(req.get("device", ""))]
                if session.audio is None:
                    return {"ok": False, "err": "no_audio_link"}
                n = int(req.get("frames", 100))
                turn = int(req.get("turn_id", 7))
                pace = float(req.get("pace_ms", 20)) / 1000.0
                payload = bytes(req.get("frame_bytes", 640))
                sent = 0
                for _ in range(n):
                    await session.audio.send_frame(pav1.KIND_PCM, turn, payload)
                    sent += 1
                    if pace:
                        await asyncio.sleep(pace)
                return {"ok": True, "pushed": sent, "turn_id": turn, "pace_ms": pace * 1000}
            if op == "metrics":
                session = self.sessions[str(req.get("device", ""))]
                return {"ok": True, "turns": [m.as_dict() for m in session.metrics[-5:]]}
            if op == "audio":
                session = self.sessions[str(req.get("device", ""))]
                return {"ok": True, "device_id": session.device_id,
                        "state": session.state, "turn_id": session.active_turn,
                        "audio_link": bool(session.audio and session.audio.authed),
                        "rx_frames": session.probe_frames,
                        "rx_bytes": session.probe_bytes,
                        "rx_seconds": round(session.probe_bytes / 32000, 2),
                        "last_turn_seen": session.probe_last_turn,
                        "rx_end_frames": session.probe_end_frames,
                        "last_end_turn": session.probe_end_turn,
                        "last_pre_roll_ms": session.last_pre_roll_ms,
                        "last_capture_frames": session.last_capture_frames,
                        "last_capture_dropped": session.last_capture_dropped,
                        "last_rms": session.last_rms, "last_peak": session.last_peak,
                        "turns_done": len(session.metrics),
                        "totals": dict(self.totals)}
            if op == "show":
                session = self.sessions[str(req.get("device", ""))]
                return {"ok": True, "reply": await session.show_text(str(req.get("text", "")))}
            if op == "speak":
                session = self.sessions[str(req.get("device", ""))]
                await session.speak_notification(str(req.get("text", "")))
                return {"ok": True}
            return {"ok": False, "err": "unknown_op", "op": op}
        except KeyError as exc:
            return {"ok": False, "err": "no_session", "detail": str(exc),
                    "devices": list(self.sessions)}
        except asyncio.TimeoutError:
            return {"ok": False, "err": "timeout"}
        except Exception as exc:                      # noqa: BLE001 - admin must answer
            log.exception("admin op failed")
            return {"ok": False, "err": "internal", "detail": str(exc)}

    # -- connection handlers ----------------------------------------------
    async def _on_control(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        session: Optional[Session] = None
        tune_socket(writer)
        try:
            first = await asyncio.wait_for(reader.readline(), timeout=self.cfg.hello_timeout)
            msg = json.loads(first.decode() or "{}")
            if msg.get("cmd") != "hello" and self.cfg.allow_anonymous:
                # Protocol §11 escape hatch: firmware that predates hello/auth can
                # still be tested against the real service. No audio, no HMAC, and
                # the log makes it obvious this is not a normal deployment.
                peer = writer.get_extra_info("peername")
                device_id = f"anon-{peer[0] if peer else 'unk'}".replace(".", "-")
                log.warning("ANONYMOUS control session from %s (dev mode; no auth, no audio)",
                            peer)
                session = Session(self, device_id, secrets.token_hex(8))
                link = ControlLink(session, reader, writer)
                session.control = link
                session.authed = True
                link.authed = True
                self.sessions[device_id] = session
                self.by_session_id[session.session_id] = session
                await session.on_control_line(link, first.decode().strip())
                await link.read_loop()
                return
            if msg.get("cmd") != "hello":
                writer.write(b'{"id":0,"ok":false,"err":"hello_required"}\n')
                await writer.drain()
                return
            device_id = str(msg.get("device_id", ""))
            rid = msg.get("id", 0)
            provisioned = device_id in self.cfg.devices
            if not provisioned and not self.cfg.allow_anonymous:
                # Reply with the caller's id and close: a half-open socket that
                # says nothing is worse than a refusal, because the device has
                # to wait out its own handshake timeout to learn the same thing.
                writer.write((json.dumps({"id": rid, "ok": False,
                                          "err": "unknown_device"}) + "\n").encode())
                await writer.drain()
                writer.close()
                return

            session = Session(self, device_id, secrets.token_hex(8))
            link = ControlLink(session, reader, writer)
            session.control = link
            live_audio = self.audio_links.get(device_id)
            if live_audio is not None and live_audio.authed:
                # The device dials its audio channel once per boot and keeps it
                # across control reconnects. Leaving that link bound to the dead
                # session means nobody reads it: the peer's window stays shut and
                # every frame is refused with EAGAIN while the socket still looks
                # alive on both ends.
                session.audio = live_audio
                live_audio.session = session
                log.info("%s adopted the live audio link into session %s",
                         device_id, session.session_id[:8])
            if not provisioned:
                # Dev compatibility (protocol §11): an unprovisioned device still
                # needs a working link, and it needs it under its real device id
                # so an operator can address it. It gets no HMAC: auth == "none".
                log.warning("hello from unprovisioned %s: anonymous session (dev mode)",
                            device_id)
                session.auth_mode = "none"
                session.authed = True
                link.authed = True
            self.sessions[device_id] = session
            self.by_session_id[session.session_id] = session
            server_nonce = self.new_nonce()
            try:
                self.note_control_nonce(device_id, str(msg.get("nonce", "")), server_nonce)
            except PermissionError:
                # A replayed nonce is an auth problem, not a crash: answer in a
                # way the peer can act on instead of dropping the socket silently.
                log.warning("hello from %s rejected: nonce %s… already used "
                            "(a device that generates one nonce per boot will loop "
                            "here; it must be fresh per connection)",
                            device_id, str(msg.get("nonce", ""))[:8])
                writer.write((json.dumps({"id": rid, "ok": False, "cmd": "hello",
                                          "err": "replay"}) + "\n").encode())
                await writer.drain()
                writer.close()
                return
            session.device_nonce = str(msg.get("nonce", ""))
            session.server_nonce_control = server_nonce
            log.info("hello %s auth=%s nonce=%s… session=%s", device_id,
                     "hmac" if provisioned else "none", str(msg.get("nonce", ""))[:8],
                     session.session_id[:8])
            await link.send({"id": rid, "ok": True, "cmd": "hello",
                             "proto": "v1", "server_nonce": server_nonce,
                             "session_id": session.session_id,
                             "audio_port": self.audio_port,
                             "audio_enabled": self.audio_enabled,
                             "auth": "hmac" if provisioned else "none"})
            await link.read_loop()
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                writer.close()
        except Exception:                                  # noqa: BLE001
            log.exception("control connection failed")
            with contextlib.suppress(Exception):
                writer.close()
        finally:
            if session:
                session.on_transport_lost("control channel closed")
                # A reconnect replaces the registry entry; only remove our own.
                if self.sessions.get(session.device_id) is session:
                    self.sessions.pop(session.device_id, None)
                if self.by_session_id.get(session.session_id) is session:
                    self.by_session_id.pop(session.session_id, None)
                for task in (session._pipeline_task, session._capture_task):
                    if task and not task.done():
                        task.cancel()

    async def _on_audio(self, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
        """Accept an audio channel: read the HELLO, then bind it to a session.

        Three ways this used to end in a silent close, all of them seen on real
        hardware: a HELLO split across TCP segments (one `read()` is not a
        frame), a bogus session_id (the error reply was itself malformed, so it
        raised before being sent), and a peer that connects and says nothing.
        Silence is indistinguishable from "the server is not there", so every
        exit here now says something first.
        """
        session: Optional[Session] = None
        link: Optional[AudioLink] = None
        framer = pav1.Framer()
        tune_socket(writer)

        def reply(err: str, ok: bool = False) -> None:
            with contextlib.suppress(Exception):
                writer.write(pav1.encode_json_frame(
                    pav1.KIND_HELLO, 0, {"ok": ok, "err": err}))

        try:
            # Collect until a whole frame is available; TCP may split the header
            # from the payload, which is exactly how the first HELLO was lost.
            head: Optional[pav1.Frame] = None
            rest: list[pav1.Frame] = []
            deadline = asyncio.get_running_loop().time() + self.cfg.hello_timeout
            while head is None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    reply("timeout")
                    await writer.drain()
                    writer.close()
                    return
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), remaining)
                except asyncio.TimeoutError:
                    reply("timeout")
                    await writer.drain()
                    writer.close()
                    return
                if not chunk:
                    writer.close()          # peer left before saying anything
                    return
                try:
                    frames = framer.feed(chunk)
                except pav1.Pav1Error as exc:
                    reply(exc.code.lower())
                    await writer.drain()
                    writer.close()
                    return
                if frames:
                    head, rest = frames[0], frames[1:]

            if head.kind != pav1.KIND_HELLO:
                reply("bad_frame")
                await writer.drain()
                writer.close()
                return

            obj = head.json() or {}
            session = self.by_session_id.get(str(obj.get("session_id", "")))
            if session is None:
                reply("session_mismatch")
                await writer.drain()
                writer.close()
                return

            if session.audio is not None:
                # One audio channel per session: the newest wins, because after a
                # reconnect two clients must not compete for the same speaker.
                await session.audio.close()
            link = AudioLink(session, reader, writer, framer=framer)
            session.audio = link
            # The socket outlives control reconnects, so it needs a home that is
            # not a Session: a dead session's audio link would otherwise be
            # adopted by nobody and every send would be refused with EAGAIN while
            # both ends still see a healthy connection.
            self.audio_links[session.device_id] = link
            try:
                await session.on_audio_hello(link, obj)
            except (PermissionError, ProtocolViolation) as exc:
                await link.send_json(pav1.KIND_HELLO, {"ok": False, "err": str(exc)})
                await link.close()
                return
            # Frames that arrived in the same packet as the HELLO (an AUTH frame
            # sent eagerly) still belong to this link.
            for frame in rest:
                await link._handle(frame)
            await link.read_loop()
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                writer.close()
        except Exception:                                  # noqa: BLE001
            log.exception("audio connection failed")
            with contextlib.suppress(Exception):
                writer.close()
        finally:
            if session is not None and session.audio is link:
                session.audio = None
            if session is not None and self.audio_links.get(session.device_id) is link:
                self.audio_links.pop(session.device_id, None)
            if session is not None and session.state in (STATE_LISTENING, STATE_SPEAKING):
                session.state = STATE_IDLE
                session.active_turn = None

    # -- host API ----------------------------------------------------------
    def session_of(self, device_id: str) -> Optional[Session]:
        return self.sessions.get(device_id)