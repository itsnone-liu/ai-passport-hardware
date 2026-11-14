#!/usr/bin/env python3
"""Generate the golden byte vectors for PAV1 v1 (docs/voice-protocol-v1.md).

Both ends must regenerate this file and diff it before signing the contract; a
mismatch means the two implementations disagree about the wire format, which is
exactly what this file exists to catch before any hardware is involved.

    python3 make_vectors.py            # rewrite vectors.json next to this file
    python3 make_vectors.py --check    # fail if vectors.json is out of date

Every stream below is what a receiver actually sees from recv(): a list of
segments, because TCP is free to split and coalesce the same bytes any way it
likes. `expect` says what the parser must conclude — the frames it must emit and
the error it must raise (if any), in order.

Nothing here is secret. The auth vectors use a throwaway shared secret so that
the HMAC can be checked by both sides.
"""
import argparse
import hashlib
import hmac
import json
import os
import struct

MAGIC = b"PAV1"
KIND_PCM, KIND_HELLO, KIND_END, KIND_AUTH = 1, 2, 3, 4
MAX_PAYLOAD = 4096
DEFAULT_ACTIVE_TURN = 12  # the turn the session is already inside when a stream starts

# Throwaway values, test-only; the real device secret never appears in this repo.
TEST_SECRET = b"vector-secret-0001"
TEST_DEVICE_ID = "psp-00a1b2"
TEST_DEVICE_NONCE = "00112233445566778899aabbccddeeff"
TEST_SERVER_NONCE = "ffeeddccbbaa99887766554433221100"


def frame(kind, turn_id, payload=b"", magic=MAGIC, flags=0, reserved=0, declared_len=None):
    """Serialise one PAV1 frame; declared_len overrides the header length field."""
    n = len(payload) if declared_len is None else declared_len
    hdr = magic + struct.pack("<BBHII", kind, flags, reserved, turn_id, n)
    assert len(hdr) == 16, len(hdr)
    return hdr + payload


def pcm(n_bytes, turn_id, base=0):
    """A deterministic, easy-to-eyeball waveform: 16-bit ramp starting at base."""
    samples = [(base + i) & 0xFFFF for i in range(n_bytes // 2)]
    return struct.pack("<%dH" % len(samples), *samples)


def mac(side, device_id, device_nonce, server_nonce):
    canonical = f"PAV1AUTH|{side}|{device_id}|{device_nonce}|{server_nonce}"
    return hmac.new(TEST_SECRET, canonical.encode(), hashlib.sha256).hexdigest()


def j(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def frame_entry(name, intent, kind, turn_id, payload, expect_frames, expect_error=None,
                magic=MAGIC, declared_len=None, **hdr):
    wire = frame(kind, turn_id, payload, magic=magic, declared_len=declared_len, **hdr)
    return {
        "name": name,
        "intent": intent,
        "wire_hex": wire.hex(),
        "bytes": len(wire),
        "header": {
            "magic": magic.decode("ascii", "replace"),
            "kind": kind,
            "flags": hdr.get("flags", 0),
            "reserved": hdr.get("reserved", 0),
            "turn_id": turn_id,
            "payload_len": len(payload) if declared_len is None else declared_len,
        },
        "expect": {"frames": expect_frames, "error": expect_error},
    }


def stream_entry(name, intent, segments, expect_frames, expect_error=None, pre=None):
    """pre = session-layer precondition; the bytes alone cannot express it.

    A framer only sees bytes, so "PCM before AUTH" and "PCM after END" are
    indistinguishable from a normal turn without knowing the session state the
    test starts in. Recording that here keeps the vectors executable rather
    than descriptive.
    """
    return {
        "name": name,
        "intent": intent,
        "pre": pre if pre is not None else {"authed": True, "active_turn": DEFAULT_ACTIVE_TURN},
        "segments_hex": [s.hex() for s in segments],
        "bytes": sum(len(s) for s in segments),
        "expect": {"frames": expect_frames, "error": expect_error},
    }


def build():
    TURN = 12
    hello_payload = j({"device_id": TEST_DEVICE_ID, "proto": "v1", "nonce": TEST_DEVICE_NONCE,
                       "control_session": "s-0001"})
    hello_ack_payload = j({"ok": True, "server_nonce": TEST_SERVER_NONCE, "proto": "v1",
                           "turn_id_max": 1})
    auth_payload = j({"mac": mac("audio", TEST_DEVICE_ID, TEST_DEVICE_NONCE, TEST_SERVER_NONCE)})
    bad_auth_payload = j({"mac": "00" * 32})

    pcm20 = pcm(640, TURN)
    pcm40 = pcm(1280, TURN, base=1000)
    end = frame(KIND_END, TURN)

    frames = [
        frame_entry("hello", "handshake opening frame on the audio channel", KIND_HELLO, 0,
                    hello_payload, [{"kind": KIND_HELLO, "turn_id": 0,
                                     "payload_len": len(hello_payload)}]),
        frame_entry("pcm_20ms", "normal 20 ms frame: 640 B = 320 samples @16 kHz", KIND_PCM, TURN,
                    pcm20, [{"kind": KIND_PCM, "turn_id": TURN, "payload_len": 640}]),
        frame_entry("pcm_40ms", "allowed 40 ms payload: 1280 B, same turn", KIND_PCM, TURN, pcm40,
                    [{"kind": KIND_PCM, "turn_id": TURN, "payload_len": 1280}]),
        frame_entry("end", "uplink audio for this turn is finished; payload_len MUST be 0",
                    KIND_END, TURN, b"", [{"kind": KIND_END, "turn_id": TURN, "payload_len": 0}]),
        frame_entry("auth", "HMAC-SHA256 over PAV1AUTH|audio|... as 64 lowercase hex chars",
                    KIND_AUTH, 0, auth_payload, [{"kind": KIND_AUTH, "turn_id": 0,
                                                  "payload_len": len(auth_payload)}]),
    ]

    streams = [
        stream_entry("turn_normal_coalesced",
                     "PCM x2 + END arriving in one recv(): parser must emit three frames",
                     [frame(KIND_PCM, TURN, pcm20) + frame(KIND_PCM, TURN, pcm40) + end],
                     [{"kind": KIND_PCM, "turn_id": TURN, "payload_len": 640},
                      {"kind": KIND_PCM, "turn_id": TURN, "payload_len": 1280},
                      {"kind": KIND_END, "turn_id": TURN, "payload_len": 0}]),
        stream_entry("split_inside_header",
                     "header torn after 7 bytes; parser must buffer and wait, not resync",
                     [frame(KIND_PCM, TURN, pcm20)[:7], frame(KIND_PCM, TURN, pcm20)[7:]],
                     [{"kind": KIND_PCM, "turn_id": TURN, "payload_len": 640}]),
        stream_entry("split_inside_payload",
                     "payload torn across three recv() calls",
                     [frame(KIND_PCM, TURN, pcm20)[:16], frame(KIND_PCM, TURN, pcm20)[16:400],
                      frame(KIND_PCM, TURN, pcm20)[400:]],
                     [{"kind": KIND_PCM, "turn_id": TURN, "payload_len": 640}]),
        stream_entry("bad_magic",
                     "wrong magic: drop, report BAD_MAGIC, close the channel (no resync)",
                     [frame(KIND_PCM, TURN, pcm20, magic=b"PAV2")], [],
                     "BAD_MAGIC"),
        stream_entry("oversize_payload",
                     "payload_len 4097 > 4096 ceiling; must not allocate 4 KB+ blindly",
                     [frame(KIND_PCM, TURN, pcm20, declared_len=MAX_PAYLOAD + 1)], [],
                     "OVERSIZE"),
        stream_entry("unknown_kind",
                     "kind 9 has no meaning in v1", [frame(9, TURN, b"\x00\x00")], [],
                     "UNKNOWN_KIND"),
        stream_entry("pcm_zero_length",
                     "PCM frame with an empty payload is malformed",
                     [frame(KIND_PCM, TURN, b"")], [], "BAD_FRAME"),
        stream_entry("end_with_payload",
                     "END must carry no payload; anything else is BAD_FRAME",
                     [frame(KIND_END, TURN, b"\x00\x00\x00\x00")], [], "BAD_FRAME"),
        stream_entry("duplicate_end",
                     "framer emits both frames; the session layer must ignore the second END "
                     "(idempotent) and must not emit playback.ended twice",
                     [end, end],
                     [{"kind": KIND_END, "turn_id": TURN, "payload_len": 0},
                      {"kind": KIND_END, "turn_id": TURN, "payload_len": 0}]),
        stream_entry("pcm_after_end",
                     "audio for a turn that already ended must be rejected, not played",
                     [end, frame(KIND_PCM, TURN, pcm20)],
                     [{"kind": KIND_END, "turn_id": TURN, "payload_len": 0}], "TURN_CLOSED"),
        stream_entry("turn_mismatch",
                     "PCM for turn 13 while turn 12 is the active playback turn",
                     [frame(KIND_PCM, TURN, pcm20), frame(KIND_PCM, 13, pcm20)],
                     [{"kind": KIND_PCM, "turn_id": TURN, "payload_len": 640}], "TURN_MISMATCH"),
        stream_entry("preauth_pcm",
                     "audio before a successful AUTH must be refused",
                     [frame(KIND_PCM, TURN, pcm20)], [], "NOT_AUTHED",
                     pre={"authed": False, "active_turn": None}),
        stream_entry("auth_bad_mac",
                     "well-formed AUTH frame carrying a wrong HMAC",
                     [frame(KIND_AUTH, 0, bad_auth_payload)], [], "AUTH_FAILED",
                     pre={"authed": False, "active_turn": None}),
        stream_entry("truncated_stream",
                     "connection dies mid-header; the incomplete frame is dropped on close",
                     [frame(KIND_PCM, TURN, pcm20)[:9]], [], "INCOMPLETE_FRAME"),
        stream_entry("interrupt_playback",
                     "playback.stop: receiver drops the ring, stops I2S, reports cancelled — "
                     "frames already buffered for that turn are discarded, not flushed to audio",
                     [frame(KIND_PCM, TURN, pcm20), frame(KIND_PCM, TURN, pcm40)],
                     [{"kind": KIND_PCM, "turn_id": TURN, "payload_len": 640},
                      {"kind": KIND_PCM, "turn_id": TURN, "payload_len": 1280}]),
    ]

    control = [
        {
            "name": "control_hello",
            "intent": "first line on the control channel; nothing else is accepted before it",
            "request": {"id": 1, "cmd": "hello", "proto": "v1", "device_id": TEST_DEVICE_ID,
                        "nonce": TEST_DEVICE_NONCE},
            "expect_response": {"id": 1, "ok": True, "cmd": "hello", "proto": "v1",
                                "server_nonce": TEST_SERVER_NONCE, "audio_port": 18081,
                                "session_id": "<server-assigned hex>"},
        },
        {
            "name": "control_auth",
            "intent": "HMAC over PAV1AUTH|control|device_id|device_nonce|server_nonce",
            "request": {"id": 2, "cmd": "auth",
                        "mac": mac("control", TEST_DEVICE_ID, TEST_DEVICE_NONCE,
                                   TEST_SERVER_NONCE)},
            "expect_response": {"id": 2, "ok": True, "cmd": "auth",
                                "server_mac": mac("server", TEST_DEVICE_ID, TEST_DEVICE_NONCE,
                                                  TEST_SERVER_NONCE)},
        },
        {
            "name": "control_auth_bad",
            "intent": "wrong HMAC: ok:false then the peer closes the connection",
            "request": {"id": 2, "cmd": "auth", "mac": "00" * 32},
            "expect_response": {"id": 2, "ok": False, "cmd": "auth", "err": "auth_failed"},
        },
        {
            "name": "capture_start",
            "intent": "server owns turn_id; device flushes its pre-roll on receipt",
            "request": {"id": 101, "cmd": "capture.start", "turn_id": 12},
            "expect_response": {"id": 101, "ok": True, "cmd": "capture.start", "turn_id": 12},
        },
        {
            "name": "capture_stop",
            "intent": "device answers the END frame on the audio channel and emits capture.ended",
            "request": {"id": 102, "cmd": "capture.stop", "turn_id": 12},
            "expect_response": {"id": 102, "ok": True, "cmd": "capture.stop", "turn_id": 12},
        },
        {
            "name": "playback_start",
            "intent": "device prebuffers PREBUFFER_MS then starts I2S",
            "request": {"id": 103, "cmd": "playback.start", "turn_id": 12},
            "expect_response": {"id": 103, "ok": True, "cmd": "playback.start", "turn_id": 12},
        },
        {
            "name": "playback_stop_cancel",
            "intent": "interrupt: drop the ring, silence I2S, do not touch dsh's task",
            "request": {"id": 104, "cmd": "playback.stop", "turn_id": 12},
            "expect_response": {"id": 104, "ok": True, "cmd": "playback.stop", "turn_id": 12},
        },
        {
            "name": "state_set",
            "intent": "server drives the visible state; device must not invent its own",
            "request": {"id": 105, "cmd": "state.set", "state": "processing", "turn_id": 12},
            "expect_response": {"id": 105, "ok": True, "cmd": "state.set", "state": "processing"},
        },
        {
            "name": "set_volume_clamped",
            "intent": "out-of-range value is clamped, never an error",
            "request": {"id": 106, "cmd": "set_volume", "value": 250},
            "expect_response": {"id": 106, "ok": True, "cmd": "set_volume", "volume": 100},
        },
        {
            "name": "unauthorized_command",
            "intent": "any command other than hello/auth before AUTH is refused",
            "request": {"id": 107, "cmd": "show_text", "text": "hi"},
            "expect_response": {"id": 107, "ok": False, "err": "unauthorized"},
        },
        {
            "name": "line_too_long",
            "intent": "inbound JSONL line over 512 bytes is dropped to the next newline and "
                      "reported once, never executed partially",
            "request": "<600-byte line>",
            "expect_response": {"id": 0, "ok": False, "err": "line_too_long"},
            "note": "device-side error event; the offending line must not be half-executed",
        },
    ]

    return {
        "protocol": "PAV1",
        "version": 1,
        "status": "draft for sign-off (Ubuntu side, 2026-11-14)",
        "spec": "docs/voice-protocol-v1.md",
        "constants": {
            "magic": "PAV1",
            "header_bytes": 16,
            "kinds": {"PCM": KIND_PCM, "HELLO": KIND_HELLO, "END": KIND_END, "AUTH": KIND_AUTH},
            "max_payload": MAX_PAYLOAD,
            "pcm_bytes_per_20ms": 640,
            "sample_rate_hz": 16000,
            "bytes_per_second": 32000,
            "frame_ms_preferred": 20,
            "frame_ms_allowed": [20, 40],
            "frame_ms_used_by_v1_devices": [20],
            "frame_ms_negotiated_in_hello": False,
            "pre_roll_ms": 300,
            "prebuffer_ms": 300,
            "playback_ring_ms": 1200,
            "capture_timeout_ms": 1500,
            "control_line_max_in": 512,
            "control_line_max_out": 2048,
        },
        "auth_vectors": {
            "test_secret": TEST_SECRET.decode(),
            "device_id": TEST_DEVICE_ID,
            "device_nonce": TEST_DEVICE_NONCE,
            "server_nonce": TEST_SERVER_NONCE,
            "canonical": {
                "control": f"PAV1AUTH|control|{TEST_DEVICE_ID}|{TEST_DEVICE_NONCE}|{TEST_SERVER_NONCE}",
                "audio": f"PAV1AUTH|audio|{TEST_DEVICE_ID}|{TEST_DEVICE_NONCE}|{TEST_SERVER_NONCE}",
                "server": f"PAV1AUTH|server|{TEST_DEVICE_ID}|{TEST_DEVICE_NONCE}|{TEST_SERVER_NONCE}",
            },
            "hmac_sha256_hex": {
                "control": mac("control", TEST_DEVICE_ID, TEST_DEVICE_NONCE, TEST_SERVER_NONCE),
                "audio": mac("audio", TEST_DEVICE_ID, TEST_DEVICE_NONCE, TEST_SERVER_NONCE),
                "server": mac("server", TEST_DEVICE_ID, TEST_DEVICE_NONCE, TEST_SERVER_NONCE),
            },
        },
        "frames": frames,
        "streams": streams,
        "control": control,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify vectors.json is current")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "vectors.json"))
    args = ap.parse_args()

    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
    if args.check:
        with open(args.out, "r", encoding="utf-8") as f:
            current = f.read()
        if current != text:
            raise SystemExit(f"{args.out} is out of date; run make_vectors.py")
        print(f"{args.out}: up to date ({len(text)} bytes)")
        return
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {args.out} ({len(text)} bytes)")


if __name__ == "__main__":
    main()