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

class TestSignedOffBehaviour(unittest.IsolatedAsyncioTestCase):
    """The three things the Windows side corrected in the contract, as tests.

    Sign-off is not a document edit: each clarification is a behaviour the
    service must actually have, or the next round discovers the gap on hardware.
    """

    async def asyncSetUp(self):
        self.svc = await make_service()
        self.dev = MockPassport("127.0.0.1", self.svc.control_port, DEVICE, SECRET)
        await self.dev.connect()

    async def asyncTearDown(self):
        await self.dev.close()
        await self.svc.stop()

    async def test_event_before_auth_is_ignored_not_counted(self):
        """`ready` used to arrive pre-auth and would have been judged abuse."""
        import hashlib
        reader, writer = await asyncio.open_connection("127.0.0.1", self.svc.control_port)
        nonce = hashlib.sha256(b"ready-test").hexdigest()[:32]
        writer.write((json.dumps({"id": 1, "cmd": "hello", "proto": "v1",
                                  "device_id": DEVICE, "nonce": nonce}) + "\n").encode())
        await writer.drain()
        await reader.readline()
        for _ in range(5):                       # five stray events, not three
            writer.write(b'{"event":"ready","link":"net"}\n')
        await writer.drain()
        await asyncio.sleep(0.1)
        # If they had counted as abuse the socket would be closed by now.
        writer.write(b'{"id":9,"cmd":"state.set","state":"idle"}\n')
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
        self.assertFalse(reply["ok"])
        self.assertEqual("unauthorized", reply["err"], "still connected, just unauthenticated")
        writer.close()

    async def test_scan_ends_the_session_and_next_connect_is_new(self):
        """scan forces a STA disconnect: both sockets go, session_id dies."""
        session = self.svc.session_of(DEVICE)
        old_session_id = session.session_id
        await session.scan()                     # answers, then the device drops away
        for _ in range(100):
            if self.svc.session_of(DEVICE) is None:
                break
            await asyncio.sleep(0.02)
        self.assertIsNone(self.svc.session_of(DEVICE), "dead session must be reaped")

        dev2 = MockPassport("127.0.0.1", self.svc.control_port, DEVICE, SECRET, name="post-scan")
        await dev2.connect()
        self.assertNotEqual(old_session_id, dev2.session_id)
        result = await dev2.converse(UPLOAD)
        self.assertGreater(len(result["audio"]), 0)
        await dev2.close()

    async def test_peer_vanishing_while_speaking_ends_the_turn(self):
        """The device disappearing mid-playback is a normal ending, not a hang."""
        session = self.svc.session_of(DEVICE)
        await session.speak_notification("一段会被网络中断打断的播报")
        await asyncio.wait_for(self.dev._playback_started.wait(), 5)
        await self.dev.close()                   # yank the cable
        for _ in range(100):
            if self.svc.session_of(DEVICE) is None:
                break
            await asyncio.sleep(0.02)
        self.assertIsNone(self.svc.session_of(DEVICE))
        self.assertEqual(STATE_IDLE, session.state)
        self.assertIsNone(session.active_turn)

    async def test_get_status_carries_the_real_heap_numbers(self):
        """The protocol's memory budget was ~50 KB optimistic; read, don't guess."""
        session = self.svc.session_of(DEVICE)
        status = await session.get_status()
        self.assertEqual(118624, status["heap"])
        self.assertEqual(106496, status["heap_block"])
        self.assertLess(38400, status["heap_block"], "playback ring must fit one block")


class TestLegacyFirmwareCompat(unittest.IsolatedAsyncioTestCase):
    """Protocol §11: firmware from before hello/auth must still be testable.

    The Windows side has the control channel working today but no handshake yet
    (that is W2-2), so without this they cannot point a device at the real
    service at all. It is opt-in, audio-less and loudly logged -- a debugging
    aid, not a supported deployment.
    """

    async def test_anonymous_control_session_when_enabled(self):
        svc = await make_service(allow_anonymous=True)
        reader, writer = await asyncio.open_connection("127.0.0.1", svc.control_port)
        # The session is created from the first line, so a legacy device speaks first.
        writer.write(b'{"id":1,"cmd":"ping"}\n')
        await writer.drain()
        first = json.loads(await asyncio.wait_for(reader.readline(), 3))
        self.assertFalse(first["ok"])
        self.assertEqual("unknown_cmd", first["err"], "connected, just unauthenticated")

        async def device():
            """Answer one server request, the way real firmware would."""
            line = await asyncio.wait_for(reader.readline(), 3)
            msg = json.loads(line)
            writer.write((json.dumps({"id": msg["id"], "ok": True,
                                      "cmd": msg["cmd"], "battery": 94}) + "\n").encode())
            await writer.drain()

        task = asyncio.create_task(device())
        for _ in range(100):
            if svc.sessions:
                break
            await asyncio.sleep(0.01)
        session = next(iter(svc.sessions.values()))
        self.assertTrue(session.device_id.startswith("anon-"))
        status = await session.get_status()
        self.assertEqual(94, status["battery"])
        await task
        writer.close()
        await svc.stop()

    async def test_anonymous_is_refused_by_default(self):
        svc = await make_service()
        reader, writer = await asyncio.open_connection("127.0.0.1", svc.control_port)
        writer.write(b'{"id":1,"cmd":"get_status"}\n')
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 3))
        self.assertFalse(reply["ok"])
        self.assertEqual("hello_required", reply["err"])
        writer.close()
        await svc.stop()


class TestAdminSocket(unittest.IsolatedAsyncioTestCase):
    """The admin socket is how a human drives a device; it must stay local-only."""

    async def asyncSetUp(self):
        import socket as _s
        with _s.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.admin_port = probe.getsockname()[1]
        self.svc = await make_service(admin_port=self.admin_port)
        self.dev = MockPassport("127.0.0.1", self.svc.control_port, DEVICE, SECRET)
        await self.dev.connect()

    async def asyncTearDown(self):
        await self.dev.close()
        await self.svc.stop()

    async def _admin(self, req):
        import socket
        loop = asyncio.get_running_loop()

        def run():
            with socket.create_connection(("127.0.0.1", self.admin_port), timeout=10) as s:
                s.sendall((json.dumps(req) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                return json.loads(buf)

        return await loop.run_in_executor(None, run)

    async def test_list_and_send_preserve_the_request_id(self):
        listed = await self._admin({"op": "list"})
        self.assertTrue(listed["ok"])
        self.assertEqual(DEVICE, listed["sessions"][0]["device_id"])
        reply = await self._admin({"op": "send", "device": DEVICE,
                                   "json": {"id": 900, "cmd": "ping"}})
        self.assertTrue(reply["ok"])
        self.assertEqual(900, reply["reply"]["id"], "admin must not renumber ids")
        self.assertEqual("pong", reply["reply"]["cmd"])

    async def test_send_to_unknown_device_reports_available_ones(self):
        reply = await self._admin({"op": "send", "device": "nope",
                                   "json": {"id": 1, "cmd": "ping"}})
        self.assertFalse(reply["ok"])
        self.assertEqual("no_session", reply["err"])
        self.assertIn(DEVICE, reply["devices"])


class TestServerKeepalive(unittest.IsolatedAsyncioTestCase):
    """A device that loses Wi-Fi sends no FIN; without keepalive we hang for hours."""

    async def test_accepted_control_socket_gets_keepalive(self):
        import socket as _socket
        from server.service import tune_socket

        held: dict = {}

        async def handler(reader, writer):          # noqa: ARG001 - keep both alive
            held["writer"] = writer

        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(100):
            if "writer" in held:
                break
            await asyncio.sleep(0.01)
        server_writer = held["writer"]

        tune_socket(server_writer)
        sock = server_writer.get_extra_info("socket")
        self.assertEqual(1, sock.getsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE))
        self.assertEqual(30, sock.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE))
        self.assertEqual(10, sock.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL))
        self.assertEqual(3, sock.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT))

        client_writer.close()
        server_writer.close()
        srv.close()
        await srv.wait_closed()


class TestHelloFallback(unittest.IsolatedAsyncioTestCase):
    """W2-2: a device that speaks hello must never be left guessing.

    The first version answered an unprovisioned hello with `unknown_device` and
    then *kept the socket open and silent*, so firmware that only reads valid
    replies saw "no answer" and burned its own 5 s timeout, twice a minute.
    """

    async def _hello(self, port, device_id, rid=7):
        import hashlib
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        nonce = hashlib.sha256(device_id.encode()).hexdigest()[:32]
        writer.write((json.dumps({"id": rid, "cmd": "hello", "proto": "v1",
                                  "device_id": device_id, "nonce": nonce}) + "\n").encode())
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 3))
        return reader, writer, reply

    async def test_unprovisioned_device_gets_anonymous_session_not_refusal(self):
        svc = await make_service(allow_anonymous=True)
        reader, writer, reply = await self._hello(svc.control_port, "psp-f53038")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(7, reply["id"], "reply id must echo the request")
        self.assertEqual("none", reply["auth"], "no secret configured -> no HMAC")
        self.assertIn("session_id", reply)
        await asyncio.sleep(0.05)
        self.assertIn("psp-f53038", svc.sessions,
                      "operator must be able to address it by its real id")
        writer.close()
        await svc.stop()

    async def test_unprovisioned_device_refused_and_socket_closed_without_anonymous(self):
        svc = await make_service()                     # allow_anonymous defaults False
        reader, writer, reply = await self._hello(svc.control_port, "psp-f53038")
        self.assertFalse(reply["ok"])
        self.assertEqual(7, reply["id"], "a refusal must still be correlatable")
        self.assertEqual("unknown_device", reply["err"])
        tail = await asyncio.wait_for(reader.read(10), 2)
        self.assertEqual(b"", tail, "silence after a refusal is a bug, not a retry cue")
        writer.close()
        await svc.stop()

    async def test_provisioned_device_still_requires_hmac(self):
        svc = await make_service()                     # DEVICE has a secret
        reader, writer, reply = await self._hello(svc.control_port, DEVICE)
        self.assertTrue(reply["ok"])
        self.assertEqual("hmac", reply["auth"])
        self.assertIn("server_nonce", reply)
        writer.close()
        await svc.stop()
