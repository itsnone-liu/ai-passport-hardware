"""A fake Passport that speaks PAV1 so the whole loop can be tested without one.

This is not a convenience mock: it is the acceptance harness. It follows the
same state machine the firmware must implement (press -> wait for capture.start
-> stream -> END on capture.stop -> play until END -> report playback.ended), so
a protocol misunderstanding shows up here instead of on the bench.

It also has to be honest about what it *doesn't* verify: it does not emulate
I2S, DMA, jitter or the 4 KB task stacks that bit the Windows side. Those are
hardware facts; this file checks the contract.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Dict, List, Optional

from server import pav1


class MockPassport:
    def __init__(self, host: str, control_port: int, device_id: str, secret: bytes,
                 name: str = "dev", audio_frame_bytes: int = pav1.FRAME_BYTES_20MS,
                 upload_pace: float = 0.0):
        self.host = host
        self.control_port = control_port
        self.device_id = device_id
        self.secret = secret
        self.name = name
        self.audio_frame_bytes = audio_frame_bytes
        self.upload_pace = upload_pace

        self.control_reader: Optional[asyncio.StreamReader] = None
        self.control_writer: Optional[asyncio.StreamWriter] = None
        self.audio_reader: Optional[asyncio.StreamReader] = None
        self.audio_writer: Optional[asyncio.StreamWriter] = None

        self.session_id = ""
        self.server_nonce = ""
        self.device_nonce = ""
        self.audio_port = 0

        self.pending: Dict[int, asyncio.Future] = {}
        self.replies: List[dict] = []
        self.events: List[dict] = []
        self.errors: List[dict] = []
        self.commands: List[dict] = []

        self.state = "idle"
        self.turn_id = 0
        self.playback = bytearray()
        self.playback_turns: List[int] = []
        self.underruns = 0

        self._listening = asyncio.Event()
        self._playback_started = asyncio.Event()
        self.t_playback_cmd = 0.0
        self.t_first_pcm = 0.0
        self._playback_ended = asyncio.Event()
        self._end_reason = ""
        self._control_task: Optional[asyncio.Task] = None
        self._audio_task: Optional[asyncio.Task] = None
        self._audio_framer = pav1.Framer()
        self._closed = False
        self._seq = 0
        self.received_end = False
        self.server_audio_nonce = ""
        self.busy_once = False
        self.legacy_turn_end = False
        self.uplink_stall = False
        self.pre_roll_ms = 0
        self.captured_frames = 0
        self.audio_device_nonce = ""
        self.audio_authed = False

    # -- connection --------------------------------------------------------
    def _mac(self, side: str) -> str:
        return pav1.auth_mac(self.secret, side, self.device_id,
                             self.device_nonce, self.server_nonce)

    async def connect(self, timeout: float = 5.0) -> None:
        import hashlib
        self.device_nonce = hashlib.sha256(
            f"{self.device_id}-{self.name}-{time.time_ns()}".encode()).hexdigest()[:32]
        self.control_reader, self.control_writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.control_port), timeout=timeout)
        self._control_task = asyncio.create_task(self._control_loop())

        reply = await self.request("hello", proto="v1", device_id=self.device_id,
                                   nonce=self.device_nonce, timeout=timeout)
        if not reply.get("ok"):
            raise PermissionError(f"hello rejected: {reply}")
        self.session_id = reply["session_id"]
        self.server_nonce = reply["server_nonce"]
        self.audio_port = int(reply.get("audio_port") or 0)

        reply = await self.request("auth", mac=self._mac("control"), timeout=timeout)
        if not reply.get("ok"):
            raise PermissionError(f"auth rejected: {reply}")
        expected = pav1.auth_mac(self.secret, "server", self.device_id,
                                 self.device_nonce, self.server_nonce)
        assert reply.get("server_mac") == expected, "server_mac mismatch"

        if self.audio_port:
            await self._connect_audio(timeout)

    async def _connect_audio(self, timeout: float = 5.0) -> None:
        self.audio_reader, self.audio_writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.audio_port), timeout=timeout)
        self._audio_framer = pav1.Framer()
        self._audio_task = asyncio.create_task(self._audio_loop())
        # A real device sends a fresh nonce on the audio channel. Reusing the
        # control nonce here made the two possible mac inputs coincide, which is
        # exactly how a wrong implemented reading passed this suite once.
        import hashlib as _hashlib
        self.audio_device_nonce = _hashlib.sha256(
            f"{self.device_id}-{self.name}-audio-{time.time_ns()}".encode()).hexdigest()[:32]
        hello = json.dumps({"device_id": self.device_id, "proto": "v1",
                            "session_id": self.session_id,
                            "nonce": self.audio_device_nonce}).encode()
        self.audio_writer.write(pav1.encode_frame(pav1.KIND_HELLO, 0, hello))
        await self.audio_writer.drain()
        # server replies HELLO with its nonce, then we AUTH
        deadline = time.monotonic() + timeout
        while not self.server_audio_nonce:
            if time.monotonic() > deadline:
                raise TimeoutError("no audio hello ack")
            await asyncio.sleep(0.01)
        mac = pav1.auth_mac(self.secret, "audio", self.device_id,
                            self.audio_device_nonce, self.server_audio_nonce)
        self.audio_writer.write(pav1.encode_frame(
            pav1.KIND_AUTH, 0, json.dumps({"mac": mac}).encode()))
        await self.audio_writer.drain()
        deadline = time.monotonic() + timeout
        while not self.audio_authed:
            if time.monotonic() > deadline:
                raise TimeoutError("no audio auth ack")
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self._closed = True
        for task in (self._control_task, self._audio_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for w in (self.control_writer, self.audio_writer):
            if w:
                with contextlib.suppress(Exception):
                    w.close()

    # -- control channel ---------------------------------------------------
    async def _control_loop(self) -> None:
        assert self.control_reader is not None
        try:
            while True:
                line = await self.control_reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self.errors.append({"raw": line.decode("utf-8", "replace")})
                    continue
                if "ok" in msg:                       # reply to us
                    fut = self.pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue
                if msg.get("event"):
                    self.events.append(msg)
                    continue
                self.commands.append(msg)
                await self._on_command(msg)
        except (asyncio.CancelledError, ConnectionResetError):
            pass

    async def request(self, cmd: str, timeout: float = 5.0, **fields):
        self._seq += 1
        rid = self._seq
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        line = json.dumps({"id": rid, "cmd": cmd, **fields}, separators=(",", ":")) + "\n"
        assert self.control_writer is not None
        self.control_writer.write(line.encode())
        await self.control_writer.drain()
        return await asyncio.wait_for(fut, timeout=timeout)

    async def _reply(self, msg: dict, **fields) -> None:
        cmd = fields.pop("cmd", msg.get("cmd"))
        ok = fields.pop("ok", True)
        out = {"id": msg.get("id", 0), "ok": ok, "cmd": cmd}
        out.update(fields)
        assert self.control_writer is not None
        self.control_writer.write((json.dumps(out, separators=(",", ":")) + "\n").encode())
        await self.control_writer.drain()

    async def _on_command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "capture.start":
            if self.busy_once:
                self.busy_once = False
                await self._reply(msg, cmd="capture.start", **{"ok": False, "err": "busy"})
                return
            self.turn_id = int(msg.get("turn_id", 0))
            self.state = "listening"
            self.received_end = False
            await self._reply(msg, turn_id=self.turn_id)
            await self._emit({"event": "capture.started", "turn_id": self.turn_id,
                              "pre_roll_ms": self.pre_roll_ms})
            self._listening.set()
        elif cmd == "capture.stop":
            await self._reply(msg, turn_id=self.turn_id)
            await self._send_end()
            await self._emit({"event": "capture.ended", "turn_id": self.turn_id,
                              "reason": "released", "frames": self.captured_frames,
                              "dropped": 0})
        elif cmd == "playback.start":
            self.turn_id = int(msg.get("turn_id", 0))
            self.state = "speaking"
            self.playback = bytearray()
            self.received_end = False
            self.t_playback_cmd = time.monotonic()
            await self._reply(msg, turn_id=self.turn_id)
            self._playback_started.set()
        elif cmd == "playback.stop":
            await self._reply(msg, turn_id=self.turn_id)
            self.state = "idle"
            await self._emit({"event": "playback.ended", "turn_id": self.turn_id,
                              "reason": "cancelled"})
            self._end_reason = "cancelled"
            self._playback_ended.set()
        elif cmd == "state.set":
            await self._reply(msg, state=msg.get("state"))
        elif cmd == "set_volume":
            value = max(0, min(100, int(msg.get("value", 0))))
            await self._reply(msg, volume=value)
        elif cmd == "set_brightness":
            value = max(0, min(100, int(msg.get("value", 0))))
            await self._reply(msg, brightness=value)
        elif cmd == "show_text":
            await self._reply(msg)
        elif cmd == "ping":
            # Protocol §6.1: the reply's cmd is "pong", not an echo of "ping".
            await self._reply(msg, cmd="pong")
        elif cmd == "get_status":
            await self._reply(msg, battery=94, mv=4120, volume=60, brightness=80, uptime=1,
                              # real board numbers after a full boot: these are
                              # what the service should read, not estimate
                              heap=118624, heap_min=113236, heap_block=106496,
                              audio_ready=True)
        elif cmd == "scan":
            # Real hardware must disconnect the STA to scan, which drops both
            # TCP channels. Reproduce that instead of answering politely.
            await self._reply(msg, count=0, aps=[], truncated=False)
            await self._kill_session()
        elif cmd == "set_net":
            await self._reply(msg, ok=True)
            await self._kill_session()
        elif cmd == "get_net":
            await self._reply(msg, ssid="lab-5g", host="127.0.0.1", port=self.control_port,
                              password_set=True)
        else:
            await self._reply(msg)

    async def _kill_session(self) -> None:
        """What a STA rebuild looks like from the host side: both sockets die."""
        await asyncio.sleep(0.02)
        for w in (self.audio_writer, self.control_writer):
            if w:
                with contextlib.suppress(Exception):
                    w.close()

    async def _emit(self, obj: dict) -> None:
        assert self.control_writer is not None
        self.control_writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        await self.control_writer.drain()

    # -- audio channel -----------------------------------------------------
    async def _send_end(self) -> None:
        assert self.audio_writer is not None
        self.audio_writer.write(pav1.encode_frame(pav1.KIND_END, self.turn_id))
        await self.audio_writer.drain()

    async def _audio_loop(self) -> None:
        assert self.audio_reader is not None
        try:
            while True:
                chunk = await self.audio_reader.read(4096)
                if not chunk:
                    break
                for frame in self._audio_framer.feed(chunk):
                    await self._on_frame(frame)
        except (asyncio.CancelledError, ConnectionResetError, pav1.Pav1Error):
            pass

    async def _on_frame(self, frame: pav1.Frame) -> None:
        if frame.kind == pav1.KIND_HELLO:
            obj = frame.json() or {}
            # The server's first HELLO ack carries its nonce; the second (after
            # our AUTH frame) carries ok:true. A single ack can be both.
            if obj.get("server_nonce"):
                self.server_audio_nonce = obj["server_nonce"]
            if obj.get("ok") is True:
                self.audio_authed = True
            if obj.get("ok") is False:
                self.errors.append({"event": "error", "code": obj.get("err", "hello_failed")})
        elif frame.kind == pav1.KIND_PCM:
            if not self.t_first_pcm:
                self.t_first_pcm = time.monotonic()
            self.playback += frame.payload
            if frame.turn_id not in self.playback_turns:
                self.playback_turns.append(frame.turn_id)
        elif frame.kind == pav1.KIND_END:
            self.received_end = True
            # Simulate playing out what arrived, then report completion: the
            # device is the only side that knows when the speaker actually stops.
            self.state = "idle"
            self._end_reason = "completed"
            await self._emit({"event": "playback.ended", "turn_id": self.turn_id,
                              "reason": "completed"})
            self._playback_ended.set()

    # -- device actions ----------------------------------------------------
    async def press_ok(self) -> None:
        await self._emit({"event": "key", "btn": "ok", "ev": "press"})

    async def release_ok(self) -> None:
        """Release: the device closes the turn itself (no capture.stop expected)."""
        await self._emit({"event": "key", "btn": "ok", "ev": "release"})
        if getattr(self, "legacy_turn_end", False):
            return                      # old firmware waits for capture.stop
        await self._send_end()
        await self._emit({"event": "capture.ended", "turn_id": self.turn_id,
                          "reason": "released", "frames": self.captured_frames,
                          "dropped": 0, "rms": 666, "peak": 3256})

    async def stream_pcm(self, pcm: bytes, pace: float = 0.0) -> None:
        assert self.audio_writer is not None
        for off in range(0, len(pcm), self.audio_frame_bytes):
            frame = pcm[off:off + self.audio_frame_bytes]
            if len(frame) < self.audio_frame_bytes:
                frame += b"\x00" * (self.audio_frame_bytes - len(frame))
            self.captured_frames += 1
            self.audio_writer.write(pav1.encode_frame(pav1.KIND_PCM, self.turn_id, frame))
            if pace:
                await self.audio_writer.drain()
                await asyncio.sleep(pace)
        await self.audio_writer.drain()

    async def converse(self, pcm: bytes, timeout: float = 10.0) -> dict:
        """One full round: press, speak, release, listen to the answer."""
        self._listening.clear()
        self._playback_ended.clear()
        self.playback = bytearray()
        self.playback_turns = []

        await self.press_ok()
        await asyncio.wait_for(self._listening.wait(), timeout)
        await self.stream_pcm(pcm, self.upload_pace)
        if self.uplink_stall:
            await self._emit({"event": "capture.ended", "turn_id": self.turn_id,
                              "reason": "uplink_stall"})
        else:
            await self.release_ok()
        await asyncio.wait_for(self._playback_ended.wait(), timeout)
        return {
            "turn_id": self.turn_id,
            "audio": bytes(self.playback),
            "frames": len(self.playback) // self.audio_frame_bytes,
            "reason": self._end_reason,
            "turns_seen": list(self.playback_turns),
        }

    async def listen_only(self, pcm: bytes, timeout: float = 10.0) -> dict:
        await self.press_ok()
        await asyncio.wait_for(self._listening.wait(), timeout)
        await self.stream_pcm(pcm, self.upload_pace)
        return {"turn_id": self.turn_id}

    async def cancel_while_speaking(self, timeout: float = 10.0) -> dict:
        await asyncio.wait_for(self._playback_started.wait(), timeout)
        await asyncio.sleep(0.01)
        await self.press_ok()
        await asyncio.wait_for(self._playback_ended.wait(), timeout)
        return {"reason": self._end_reason, "audio": bytes(self.playback)}