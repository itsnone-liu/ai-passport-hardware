"""End-to-end tests: two channels, one turn machine, no hardware.

Everything here runs against the real service code over real TCP sockets with a
fake Passport on the other end. The mock ASR/TTS/brain make it deterministic and
free; a separate opt-in test hits Bailian for real.

    python3 -m unittest discover -s server/tests -t server -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(SERVER))

from server.config import Config               # noqa: E402
from server.mock_device import MockPassport    # noqa: E402
from server.service import Service, STATE_IDLE  # noqa: E402
from server.asr.mock import MockASR            # noqa: E402
from server.tts.mock import MockTTS, tone      # noqa: E402
from server.dsh.mock import MockBrain          # noqa: E402

SECRET = b"unit-test-secret-0123456789abcdef"
DEVICE = "psp-00a1b2"
UPLOAD = tone(0.2, freq=220)


async def make_service(**overrides) -> Service:
    kwargs = dict(host="127.0.0.1", control_port=0, audio_port=0,
                  devices={DEVICE: SECRET}, playback_speed=200.0)
    kwargs.update(overrides)
    cfg = Config(**kwargs)
    svc = Service(cfg, MockASR(), MockTTS(seconds=0.2), MockBrain())
    await svc.start()
    return svc


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.svc = await make_service()
        self.dev = MockPassport("127.0.0.1", self.svc.control_port, DEVICE, SECRET)
        await self.dev.connect()

    async def asyncTearDown(self):
        await self.dev.close()
        await self.svc.stop()


class TestBasicRounds(ServiceCase):
    async def test_one_round_returns_audio(self):
        result = await self.dev.converse(UPLOAD)
        self.assertEqual(1, result["turn_id"])
        self.assertGreater(len(result["audio"]), 0, "no audio came back")
        self.assertEqual("completed", result["reason"])
        self.assertEqual([1], result["turns_seen"])

    async def test_twenty_rounds_no_crosstalk(self):
        """The stability bar in the workplan: 20 consecutive turns, no stray audio."""
        seen = []
        for i in range(20):
            result = await self.dev.converse(UPLOAD)
            self.assertEqual(i + 1, result["turn_id"], "turn ids must be monotonic")
            self.assertGreater(len(result["audio"]), 0, f"round {i+1} got no audio")
            self.assertEqual([i + 1], result["turns_seen"],
                             f"round {i+1} played audio from another turn")
            seen.append(result["frames"])
        self.assertEqual(20, len(seen))
        self.assertTrue(all(f == seen[0] for f in seen), "audio length drifted")

    async def test_show_text_and_status_reach_the_device(self):
        session = self.svc.session_of(DEVICE)
        reply = await session.show_text("hello from dsh")
        self.assertTrue(reply["ok"])
        reply = await session.set_volume(35)
        self.assertEqual(35, reply["volume"])
        self.assertIn({"id": reply["id"], "cmd": "set_volume", "value": 35},
                      [c for c in self.dev.commands if c["cmd"] == "set_volume"])

    async def test_metrics_record_the_four_latency_points(self):
        await self.dev.converse(UPLOAD)
        await asyncio.sleep(0.05)
        session = self.svc.session_of(DEVICE)
        self.assertTrue(session.metrics, "no metrics recorded")
        m = session.metrics[-1].as_dict()
        for key in ("speech_end_to_asr", "asr_to_dsh", "dsh_to_tts",
                    "speech_end_to_first_audio"):
            self.assertIsNotNone(m[key], key)


class TestAudioStateMachine(ServiceCase):
    async def test_capture_without_audio_channel_is_refused(self):
        dev = MockPassport("127.0.0.1", self.svc.control_port, DEVICE, SECRET, name="noaudio")
        # connect() opens the audio channel; drop it to simulate a device that
        # only got the control channel up.
        await dev.connect()
        dev.audio_writer.close()            # StreamWriter.close() is not a coroutine
        dev.audio_authed = False
        await asyncio.sleep(0.1)
        await dev.press_ok()
        await asyncio.sleep(0.05)
        codes = [e.get("code") for e in dev.events if e.get("event") == "error"]
        self.assertIn("not_supported", codes)
        await dev.close()

    async def test_notification_is_a_turn_without_capture(self):
        session = self.svc.session_of(DEVICE)
        await session.speak_notification("提醒：水开了")
        await asyncio.wait_for(self.dev._playback_ended.wait(), 5)
        self.assertGreater(len(self.dev.playback), 0)
        self.assertIn(1, self.dev.playback_turns)
        for _ in range(50):                  # the idle transition follows the event
            if session.state == STATE_IDLE:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(STATE_IDLE, session.state)

    async def test_cancel_stops_playback_and_reports_cancelled(self):
        svc = await make_service(playback_speed=1.0)     # real time so cancel has room
        dev = MockPassport("127.0.0.1", svc.control_port, DEVICE, SECRET)
        await dev.connect()
        svc.tts = MockTTS(seconds=1.0)
        svc.brain = MockBrain(sentences=4)
        session = svc.session_of(DEVICE)
        await session.speak_notification("这是一段会被打断的长回复")
        result = await dev.cancel_while_speaking(timeout=10)
        self.assertEqual("cancelled", result["reason"])
        self.assertLess(len(result["audio"]), 4 * 32000 * 1.0,
                        "cancel did not actually stop the stream")
        await asyncio.sleep(0.1)
        self.assertEqual(STATE_IDLE, session.state)
        await dev.close()
        await svc.stop()


class TestSecurity(ServiceCase):
    async def test_wrong_secret_is_rejected_and_connection_closed(self):
        bad = MockPassport("127.0.0.1", self.svc.control_port, DEVICE, b"wrong-secret")
        with self.assertRaises(Exception):
            await bad.connect()
        await bad.close()

    async def test_command_before_auth_is_unauthorized(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.svc.control_port)
        import hashlib
        nonce = hashlib.sha256(b"n").hexdigest()[:32]
        writer.write((json.dumps({"id": 1, "cmd": "hello", "proto": "v1",
                                  "device_id": DEVICE, "nonce": nonce}) + "\n").encode())
        await writer.drain()
        hello = json.loads(await reader.readline())
        self.assertTrue(hello["ok"])
        writer.write(b'{"id":2,"cmd":"show_text","text":"hi"}\n')
        await writer.drain()
        reply = json.loads(await reader.readline())
        self.assertFalse(reply["ok"])
        self.assertEqual("unauthorized", reply["err"])
        writer.close()

    async def test_oversized_line_is_dropped_not_executed(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.svc.control_port)
        import hashlib
        nonce = hashlib.sha256(b"n2").hexdigest()[:32]
        writer.write((json.dumps({"id": 1, "cmd": "hello", "proto": "v1",
                                  "device_id": DEVICE, "nonce": nonce}) + "\n").encode())
        await writer.drain()
        await reader.readline()
        big = "x" * 600
        writer.write((json.dumps({"id": 3, "cmd": "show_text", "text": big}) + "\n").encode())
        await writer.drain()
        reply = json.loads(await reader.readline())
        self.assertFalse(reply["ok"])
        self.assertEqual("line_too_long", reply["err"])
        writer.close()

    async def test_garbage_on_audio_channel_is_fatal_and_reported(self):
        assert self.dev.audio_writer is not None
        self.dev.audio_writer.write(b"XXXXXXXX" + b"\x00" * 32)
        await self.dev.audio_writer.drain()
        await asyncio.sleep(0.15)
        codes = [e.get("code") for e in self.dev.events + self.dev.errors
                 if isinstance(e, dict) and e.get("event") == "error"]
        self.assertIn("bad_magic", codes, self.dev.events)


class TestReconnect(ServiceCase):
    async def test_reconnect_gets_new_session_and_turns_restart(self):
        first = await self.dev.converse(UPLOAD)
        self.assertEqual(1, first["turn_id"])
        session_id = self.dev.session_id
        await self.dev.close()
        await asyncio.sleep(0.1)

        dev2 = MockPassport("127.0.0.1", self.svc.control_port, DEVICE, SECRET, name="re")
        await dev2.connect()
        self.assertNotEqual(session_id, dev2.session_id)
        result = await dev2.converse(UPLOAD)
        self.assertEqual(1, result["turn_id"], "turn ids restart in a new session")
        self.assertEqual(1, len(result["turns_seen"]))
        await dev2.close()


class TestServiceTeardown(unittest.IsolatedAsyncioTestCase):
    async def test_service_survives_client_vanishing_mid_turn(self):
        svc = await make_service()
        dev = MockPassport("127.0.0.1", svc.control_port, DEVICE, SECRET)
        await dev.connect()
        await dev.press_ok()
        await asyncio.wait_for(dev._listening.wait(), 5)
        await dev.stream_pcm(UPLOAD)
        await dev.close()                     # device dies without saying END
        await asyncio.sleep(0.2)
        self.assertIsNone(svc.session_of(DEVICE))
        # and the service still accepts the next device
        dev2 = MockPassport("127.0.0.1", svc.control_port, DEVICE, SECRET, name="again")
        await dev2.connect()
        result = await dev2.converse(UPLOAD)
        self.assertGreater(len(result["audio"]), 0)
        await dev2.close()
        await svc.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)