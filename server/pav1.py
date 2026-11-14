"""PAV1 reference implementation — the judge both ends are measured against.

This module is deliberately dependency-free and small: it is the Python side of
`docs/voice-protocol-v1.md`, and its job is to make the contract testable rather
than aspirational. The C implementation on the ESP32 is not expected to share
code with this file, only behaviour — `protocol/v1/vectors.json` is what forces
the two to agree.

Two layers, because they answer different questions:

    Framer      bytes → frames. Knows nothing about sessions. A malformed header
                is fatal and the channel must be closed, because resynchronising
                on a torn stream is how you play garbage into a speaker.
    SessionGate frames → accepted/dropped. Knows auth and turn state. Errors
                here are mostly recoverable: dropping one frame beats tearing
                down a working link.

Keeping them apart is what makes the vectors executable: the same bytes mean
different things before and after AUTH, and the `pre` field in each vector says
which world it starts in.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from typing import Iterable, Optional

from server import adpcm

MAGIC = b"PAV1"
HEADER_BYTES = 16
MAX_PAYLOAD = 4096

KIND_PCM = 1
KIND_HELLO = 2
KIND_END = 3
KIND_AUTH = 4
# 5 = IMA/DVI ADPCM 上行：payload = [u32 seq][4 B 块头][160 B 半字节]，
# 20 ms / 块 168 B = 8.2 KB/s。设备侧理由见 docs/voice-protocol-v1.md §5.4。
KIND_ADPCM = 5
KINDS = (KIND_PCM, KIND_HELLO, KIND_END, KIND_AUTH, KIND_ADPCM)

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2          # pcm_s16le mono
FRAME_BYTES_20MS = 640
PRE_ROLL_MS = 300
PREBUFFER_MS = 300
PLAYBACK_RING_MS = 1200
CAPTURE_START_TIMEOUT_MS = 1500
UPLINK_BACKLOG_MAX = 8192
CONTROL_LINE_MAX_IN = 512
CONTROL_LINE_MAX_OUT = 2048


class Pav1Error(Exception):
    """A protocol violation. `fatal` says whether the channel must be closed."""

    def __init__(self, code: str, detail: str = "", fatal: bool = True):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.fatal = fatal

    def as_event(self, turn_id: Optional[int] = None) -> dict:
        ev = {"event": "error", "code": self.code.lower()}
        if turn_id is not None:
            ev["turn_id"] = turn_id
        if self.detail:
            ev["detail"] = self.detail
        return ev


@dataclass(frozen=True)
class Frame:
    kind: int
    turn_id: int
    payload: bytes = b""
    flags: int = 0

    @property
    def kind_name(self) -> str:
        return {KIND_PCM: "PCM", KIND_HELLO: "HELLO", KIND_END: "END",
                KIND_AUTH: "AUTH", KIND_ADPCM: "ADPCM"}.get(
                    self.kind, f"KIND_{self.kind}")

    def json(self) -> Optional[dict]:
        """Decode a HELLO/AUTH payload; None when the frame is not JSON-bearing."""
        if self.kind not in (KIND_HELLO, KIND_AUTH):
            return None
        import json
        try:
            return json.loads(self.payload.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - any decode failure is BAD_FRAME
            raise Pav1Error("BAD_FRAME", f"json: {exc}") from exc


def encode_frame(kind: int, turn_id: int, payload: bytes = b"", flags: int = 0) -> bytes:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload {len(payload)} > {MAX_PAYLOAD}")
    header = MAGIC + struct.pack("<BBHII", kind, flags, 0, turn_id & 0xFFFFFFFF, len(payload))
    return header + payload


def encode_json_frame(kind: int, turn_id: int, obj: dict) -> bytes:
    import json
    return encode_frame(kind, turn_id, json.dumps(obj, separators=(",", ":")).encode())


class Framer:
    """Incremental PAV1 framer: feed any TCP segmentation, get whole frames.

    Only ever holds one incomplete frame, and refuses to grow past MAX_PAYLOAD.
    A bad header is fatal on purpose — see docs §5.1.
    """

    def __init__(self, max_payload: int = MAX_PAYLOAD):
        self._buf = bytearray()
        self._max_payload = max_payload

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        out: list[Frame] = []
        while len(self._buf) >= HEADER_BYTES:
            if self._buf[0:4] != MAGIC:
                raise Pav1Error("BAD_MAGIC", f"got {bytes(self._buf[0:4])!r}")
            kind = self._buf[4]
            flags = self._buf[5]
            turn_id, payload_len = struct.unpack_from("<II", self._buf, 8)
            if payload_len > self._max_payload:
                raise Pav1Error("OVERSIZE", f"payload_len={payload_len}")
            if kind not in KINDS:
                raise Pav1Error("UNKNOWN_KIND", f"kind={kind}")
            if kind == KIND_ADPCM and payload_len != adpcm.PAYLOAD_BYTES:
                raise Pav1Error(
                    "BAD_FRAME", f"adpcm payload_len={payload_len}"
                )
            if kind == KIND_PCM and payload_len == 0:
                raise Pav1Error("BAD_FRAME", "pcm with empty payload")
            if kind == KIND_END and payload_len != 0:
                raise Pav1Error("BAD_FRAME", f"end with {payload_len} bytes")
            if len(self._buf) < HEADER_BYTES + payload_len:
                break
            payload = bytes(self._buf[HEADER_BYTES:HEADER_BYTES + payload_len])
            del self._buf[:HEADER_BYTES + payload_len]
            out.append(Frame(kind=kind, turn_id=turn_id, payload=payload, flags=flags))
        return out

    def eof(self) -> None:
        """Call when the peer closed: a half frame is a protocol error, not EOF."""
        if self._buf:
            raise Pav1Error("INCOMPLETE_FRAME", f"{len(self._buf)} trailing bytes",
                            fatal=False)


class SessionGate:
    """Auth + turn state enforcement shared by device and server.

    Recoverable errors (TURN_MISMATCH, TURN_CLOSED) drop one frame; unrecoverable
    ones (NOT_AUTHED, AUTH_FAILED) must end the connection.
    """

    def __init__(self, authed: bool = False, active_turn: Optional[int] = None,
                 secret: Optional[bytes] = None, device_id: str = "",
                 device_nonce: str = "", server_nonce: str = ""):
        self.authed = authed
        self.active_turn = active_turn
        self._closed_turns: set[int] = set()
        self._secret = secret
        self._device_id = device_id
        self._device_nonce = device_nonce
        self._server_nonce = server_nonce

    # -- auth ---------------------------------------------------------------
    def expect_mac(self, side: str) -> str:
        return auth_mac(self._secret or b"", side, self._device_id,
                        self._device_nonce, self._server_nonce)

    def accept_auth(self, mac_hex: str, side: str = "audio") -> None:
        if self._secret is None:
            raise Pav1Error("AUTH_FAILED", "no secret configured")
        if not hmac.compare_digest(mac_hex.lower(), self.expect_mac(side)):
            raise Pav1Error("AUTH_FAILED", "hmac mismatch")
        self.authed = True

    # -- frames -------------------------------------------------------------
    def accept(self, frame: Frame) -> Frame:
        if frame.kind == KIND_AUTH:
            mac = (frame.json() or {}).get("mac", "")
            self.accept_auth(mac)
            return frame
        if frame.kind == KIND_HELLO:
            return frame
        if not self.authed:
            raise Pav1Error("NOT_AUTHED", f"{frame.kind_name} before AUTH")

        if frame.kind == KIND_END:
            self._closed_turns.add(frame.turn_id)
            if self.active_turn == frame.turn_id:
                self.active_turn = None
            return frame

        # PCM
        if frame.turn_id in self._closed_turns:
            raise Pav1Error("TURN_CLOSED", f"turn {frame.turn_id}", fatal=False)
        if self.active_turn is not None and frame.turn_id != self.active_turn:
            raise Pav1Error("TURN_MISMATCH",
                            f"got {frame.turn_id}, active {self.active_turn}", fatal=False)
        return frame

    def set_active_turn(self, turn_id: Optional[int]) -> None:
        self.active_turn = turn_id


def auth_mac(secret: bytes, side: str, device_id: str, device_nonce: str,
            server_nonce: str) -> str:
    """HMAC-SHA256 over the canonical string from docs §3.2/§3.3.

    No timestamps: the device has no RTC, so replay protection rides on nonces.
    """
    canonical = f"PAV1AUTH|{side}|{device_id}|{device_nonce}|{server_nonce}"
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def pcm_silence(n_bytes: int) -> bytes:
    return b"\x00" * n_bytes


def frames_of(pcm: bytes, turn_id: int, frame_bytes: int = FRAME_BYTES_20MS) -> Iterable[Frame]:
    """Split a PCM buffer into fixed-size frames (last one padded with silence)."""
    for off in range(0, len(pcm), frame_bytes):
        chunk = pcm[off:off + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + pcm_silence(frame_bytes - len(chunk))
        yield Frame(kind=KIND_PCM, turn_id=turn_id, payload=chunk)