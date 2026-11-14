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

from server import pav1
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
            if not self.authed:
                await self.session.report_error("not_authed", "pcm before auth")
                return
            self.session.on_uplink_pcm(frame.turn_id, frame.payload)
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
        self.probe_end_turn = 0
        self._probe_log_at = 0.0
        self.last_pre_roll_ms = -1
        self.last_capture_frames = -1
        self.last_capture_dropped = -1
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
            # replay (straight capture.start with no key press). Reporting a
            # nominal 300 would make the server expect audio that never existed.
            self.last_pre_roll_ms = int(msg.get("pre_roll_ms", -1))
            log.info("%s capture.started turn=%s pre_roll_ms=%s", self.device_id,
                     msg.get("turn_id"), self.last_pre_roll_ms)
        elif ev == "capture.ended":
            self._last_capture_reason = str(msg.get("reason", ""))
            if "frames" in msg:
                self.last_capture_frames = int(msg.get("frames", -1))
            if "dropped" in msg:
                self.last_capture_dropped = int(msg.get("dropped", -1))
            log.info("%s capture.ended turn=%s reason=%s frames=%s dropped=%s",
                     self.device_id, msg.get("turn_id"), self._last_capture_reason,
                     self.last_capture_frames, self.last_capture_dropped)
            self._capture_ended.set()
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

    def on_uplink_pcm(self, turn_id: int, payload: bytes) -> None:
        if self.active_turn != turn_id or self.state != STATE_LISTENING:
            if self.cfg.dev_audio_probe:
                # W2-3 hands us frames before capture.start exists. Count them so
                # framing can be verified now; never play them, never ASR them.
                self.probe_frames += 1
                self.probe_bytes += len(payload)
                self.probe_last_turn = turn_id
                now = time.monotonic()
                if now - self._probe_log_at >= 1.0:
                    self._probe_log_at = now
                    log.info("%s uplink probe: %d frames / %d bytes (turn %d, %.2f s)",
                             self.device_id, self.probe_frames, self.probe_bytes,
                             turn_id, self.probe_bytes / 32000)
            return
        if len(self._capture_buf) + len(payload) > self.cfg.max_capture_bytes:
            overflow = len(self._capture_buf) + len(payload) - self.cfg.max_capture_bytes
            del self._capture_buf[:overflow]
            self._capture_dropped += overflow
        self._capture_buf += payload

    async def on_uplink_end(self, turn_id: int) -> None:
        if self.active_turn != turn_id or self.state != STATE_LISTENING:
            if self.cfg.dev_audio_probe:
                # Firmware drove capture.start itself (USB) so this link never
                # owned the turn -- still prove the END frame arrived.
                self.probe_end_frames += 1
                self.probe_end_turn = turn_id
                log.info("%s uplink probe: END seen for turn %d", self.device_id, turn_id)
            return
        self._capture_ended.set()

    # -- key handling ------------------------------------------------------
    async def on_key(self, btn: str, ev: str) -> None:
        if btn != "ok" or ev != "click":
            return
        if self.state == STATE_IDLE:
            await self.begin_capture()
        elif self.state == STATE_LISTENING:
            await self.end_capture()
        elif self.state == STATE_SPEAKING:
            await self.cancel_playback()
        else:
            await self.report_error("busy", f"state={self.state}")

    async def begin_capture(self) -> None:
        if not self.audio or not self.audio.authed:
            await self.report_error("not_supported", "audio channel not ready")
            return
        self.active_turn = self.next_turn_id()
        self.state = STATE_LISTENING
        self._capture_buf = bytearray()
        self._capture_dropped = 0
        self._capture_ended = asyncio.Event()
        self._cancel = asyncio.Event()
        reply = await self._capture_start(self.active_turn)
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
        for attempt in range(attempts):
            reply = await self.control.request("capture.start", self.cfg.request_timeout,
                                               turn_id=turn_id)
            if reply.get("ok") or reply.get("err") != "busy":
                return reply
            log.info("%s capture.start busy (attempt %d/%d), retrying in %.0f ms",
                     self.device_id, attempt + 1, attempts, delay * 1000)
            await asyncio.sleep(delay)
        return {"ok": False, "err": "busy"}

    async def _capture_watchdog(self, turn_id: int) -> None:
        try:
            await asyncio.wait_for(self._capture_ended.wait(), self.cfg.capture_timeout)
        except asyncio.TimeoutError:
            if self.active_turn == turn_id and self.state == STATE_LISTENING:
                await self.end_capture(reason="max_duration")

    async def end_capture(self, reason: str = "released") -> None:
        if self.state != STATE_LISTENING:
            return
        if self._capture_task:
            self._capture_task.cancel()
            self._capture_task = None
        turn = self.active_turn
        await self.control.request("capture.stop", self.cfg.request_timeout, turn_id=turn)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._capture_ended.wait(), 2.0)
        self.state = STATE_PROCESSING
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
        try:
            transcript = await self.service.asr.transcribe(pcm)
            m.asr_ms = self.service.now_ms()
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
        self.state = STATE_SPEAKING
        self._played_end = asyncio.Event()
        await self.control.request("playback.start", self.cfg.request_timeout,
                                  turn_id=turn_id)

        async for chunk in self.service.brain.ask(m.text, session=self.service.dsh_session):
            if m.dsh_first_ms is None:
                m.dsh_first_ms = self.service.now_ms()
            async for audio in self.service.tts.stream(chunk):
                if self._cancel.is_set():
                    break
                await self._write_audio(audio, turn_id, m)
            if self._cancel.is_set():
                break
        await self.audio.send_frame(pav1.KIND_END, turn_id)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._played_end.wait(), self.cfg.playback_tail_timeout)
        m.cancelled = self._last_end_reason == "cancelled"
        await self._to_idle(turn_id)

    async def _write_audio(self, audio: bytes, turn_id: int, m: TurnMetrics) -> None:
        step = pav1.FRAME_BYTES_20MS
        pace = 0.02 / max(0.001, self.cfg.playback_speed)
        for off in range(0, len(audio), step):
            if self._cancel.is_set():
                return
            frame = audio[off:off + step]
            if len(frame) < step and off + step > len(audio):
                break               # partial tail is dropped, not padded into the speaker
            await self.audio.send_frame(pav1.KIND_PCM, turn_id, frame)
            m.downlink_bytes += len(frame)
            if m.tts_first_ms is None:
                m.tts_first_ms = self.service.now_ms()
            await asyncio.sleep(pace)

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
        self.control_port = cfg.control_port
        self.audio_port = cfg.audio_port
        self.audio_enabled = True
        self._t0 = time.monotonic()
        self._servers: list[asyncio.AbstractServer] = []
        self._nonces: Dict[str, str] = {}
        self._seen_nonces: set[str] = set()
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
                        "turns_done": len(session.metrics)}
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
            if session is not None and session.state in (STATE_LISTENING, STATE_SPEAKING):
                session.state = STATE_IDLE
                session.active_turn = None

    # -- host API ----------------------------------------------------------
    def session_of(self, device_id: str) -> Optional[Session]:
        return self.sessions.get(device_id)