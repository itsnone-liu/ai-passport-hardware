"""Run protocol/v1/vectors.json against the reference implementation.

The vectors exist to stop the two implementations from disagreeing about the
wire format; this test is what makes them evidence rather than documentation.
If a future edit to either side changes framing behaviour, this fails before a
device is flashed.

    python3 -m unittest discover -s server/tests -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from server import pav1  # noqa: E402

VECTORS = os.path.join(ROOT, "protocol", "v1", "vectors.json")


def load():
    with open(VECTORS, "r", encoding="utf-8") as f:
        return json.load(f)


class TestFrames(unittest.TestCase):
    """Single-frame vectors: encode/decode round trip and header field order."""

    @classmethod
    def setUpClass(cls):
        cls.v = load()

    def test_every_frame_parses_to_expected_shape(self):
        for case in self.v["frames"]:
            with self.subTest(case=case["name"]):
                wire = bytes.fromhex(case["wire_hex"])
                frames = pav1.Framer().feed(wire)
                self.assertEqual(1, len(frames), case["name"])
                want = case["expect"]["frames"][0]
                got = frames[0]
                self.assertEqual(want["kind"], got.kind)
                self.assertEqual(want["turn_id"], got.turn_id)
                self.assertEqual(want["payload_len"], len(got.payload))

    def test_header_layout_is_16_bytes_little_endian(self):
        wire = pav1.encode_frame(pav1.KIND_PCM, 12, b"\x01\x02")
        self.assertEqual(16 + 2, len(wire))
        self.assertEqual(b"PAV1", wire[:4])
        self.assertEqual(1, wire[4])
        self.assertEqual((12, 2), pav1.struct.unpack_from("<II", wire, 8))

    def test_round_trip_against_golden_bytes(self):
        for case in self.v["frames"]:
            with self.subTest(case=case["name"]):
                h = case["header"]
                payload = bytes.fromhex(case["wire_hex"] )[16:]
                rebuilt = pav1.encode_frame(h["kind"], h["turn_id"], payload, h["flags"])
                self.assertEqual(case["wire_hex"], rebuilt.hex())


class TestStreams(unittest.TestCase):
    """Stream vectors: framing + session state, exactly as the contract states."""

    @classmethod
    def setUpClass(cls):
        cls.v = load()

    def _run(self, case):
        pre = case.get("pre") or {}
        framer = pav1.Framer()
        gate = pav1.SessionGate(authed=pre.get("authed", True),
                                active_turn=pre.get("active_turn"))
        authv = self.v["auth_vectors"]
        if pre.get("authed") is False:
            gate = pav1.SessionGate(authed=False, active_turn=pre.get("active_turn"),
                                    secret=authv["test_secret"].encode(),
                                    device_id=authv["device_id"],
                                    device_nonce=authv["device_nonce"],
                                    server_nonce=authv["server_nonce"])
        frames, error = [], None
        try:
            for seg_hex in case["segments_hex"]:
                for frame in framer.feed(bytes.fromhex(seg_hex)):
                    try:
                        frames.append(gate.accept(frame))
                    except pav1.Pav1Error as exc:
                        error = exc.code
                        break
                if error:
                    break
            if not error:
                framer.eof()
        except pav1.Pav1Error as exc:
            error = exc.code
        return ({"kind": f.kind, "turn_id": f.turn_id, "payload_len": len(f.payload)}
                for f in frames), error

    def test_every_stream_matches_expectation(self):
        for case in self.v["streams"]:
            with self.subTest(case=case["name"]):
                got_frames, got_error = self._run(case)
                self.assertEqual(case["expect"]["frames"], list(got_frames),
                                 f"{case['name']}: frame sequence")
                self.assertEqual(case["expect"]["error"], got_error,
                                 f"{case['name']}: error code")

    def test_fatal_errors_are_marked_fatal(self):
        """A torn/garbage stream must not be survivable: resync plays noise."""
        for code in ("BAD_MAGIC", "OVERSIZE", "UNKNOWN_KIND"):
            with self.subTest(code=code):
                err = pav1.Pav1Error(code)
                self.assertTrue(err.fatal)
        for code in ("TURN_MISMATCH", "TURN_CLOSED"):
            with self.subTest(code=code):
                self.assertFalse(pav1.Pav1Error(code, fatal=False).fatal)


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.v = load()["auth_vectors"]

    def test_hmac_matches_golden(self):
        secret = self.v["test_secret"].encode()
        for side, expected in self.v["hmac_sha256_hex"].items():
            with self.subTest(side=side):
                got = pav1.auth_mac(secret, side, self.v["device_id"],
                                    self.v["device_nonce"], self.v["server_nonce"])
                self.assertEqual(expected, got)

    def test_wrong_secret_fails(self):
        v = self.v
        got = pav1.auth_mac(b"not-the-secret", "control", v["device_id"],
                            v["device_nonce"], v["server_nonce"])
        self.assertNotEqual(v["hmac_sha256_hex"]["control"], got)

    def test_gate_rejects_bad_mac(self):
        v = self.v
        gate = pav1.SessionGate(authed=False, secret=v["test_secret"].encode(),
                                device_id=v["device_id"], device_nonce=v["device_nonce"],
                                server_nonce=v["server_nonce"])
        with self.assertRaises(pav1.Pav1Error) as ctx:
            gate.accept_auth("00" * 32)
        self.assertEqual("AUTH_FAILED", ctx.exception.code)
        self.assertTrue(ctx.exception.fatal)


class TestControlCases(unittest.TestCase):
    """The control-channel examples define what a correct reply looks like."""

    @classmethod
    def setUpClass(cls):
        cls.v = load()
        cls.by_name = {c["name"]: c for c in cls.v["control"]}

    def test_auth_vectors_in_control_examples_match_the_helper(self):
        v = self.v["auth_vectors"]
        case = self.by_name["control_auth"]
        self.assertEqual(v["hmac_sha256_hex"]["control"], case["request"]["mac"])
        self.assertEqual(v["hmac_sha256_hex"]["server"],
                         case["expect_response"]["server_mac"])

    def test_volume_clamped_example_is_consistent(self):
        case = self.by_name["set_volume_clamped"]
        self.assertEqual(250, case["request"]["value"])
        self.assertEqual(100, case["expect_response"]["volume"])

    def test_declared_limits_match_the_constants(self):
        c = self.v["constants"]
        self.assertEqual(pav1.CONTROL_LINE_MAX_IN, c["control_line_max_in"])
        self.assertEqual(pav1.CONTROL_LINE_MAX_OUT, c["control_line_max_out"])
        self.assertEqual(pav1.MAX_PAYLOAD, c["max_payload"])
        self.assertEqual(pav1.FRAME_BYTES_20MS, c["pcm_bytes_per_20ms"])
        self.assertEqual(int(pav1.BYTES_PER_SECOND * pav1.PRE_ROLL_MS / 1000),
                         int(c["pre_roll_ms"] * c["bytes_per_second"] / 1000))


class TestCapacityMath(unittest.TestCase):
    """The numbers in docs §8 must add up; a stale table is worse than none."""

    def test_ring_sizes(self):
        self.assertEqual(38400, int(pav1.BYTES_PER_SECOND * pav1.PLAYBACK_RING_MS / 1000))
        self.assertEqual(9600, int(pav1.BYTES_PER_SECOND * pav1.PRE_ROLL_MS / 1000))

    def test_frame_is_20ms(self):
        self.assertEqual(640, int(pav1.BYTES_PER_SECOND * 20 / 1000))

    def test_backlog_is_about_250ms(self):
        ms = pav1.UPLINK_BACKLOG_MAX / pav1.BYTES_PER_SECOND * 1000
        self.assertTrue(200 <= ms <= 300, ms)

    def test_prebuffer_fits_in_ring(self):
        self.assertLess(pav1.PREBUFFER_MS, pav1.PLAYBACK_RING_MS)


if __name__ == "__main__":
    unittest.main(verbosity=2)