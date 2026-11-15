"""P2 tests: the bridge brain, against a fake unix-socket bridge.

No Feishu, no dsh, no device. What is pinned here is the contract the voice
pipeline depends on:

  1. deltas are re-cut into whole sentences (TTS needs sentences, not tokens);
  2. a fragment without a terminator is still spoken at `done`;
  3. stopping the consumer (barge-in) sends `cancel` to the bridge;
  4. `error` frames and dead sockets surface as BrainUnavailable, so the pipeline
     can say something instead of going silent.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from server.dsh.bridge import BrainUnavailable, BridgeBrain, split_sentences  # noqa: E402


class FakeBridge:
    """Minimal stand-in for dsh-feishu's voice socket."""

    def __init__(self, script, path=None):
        self.script = script            # list of dicts to send after the ask
        self.received = []
        self.socket_path = path or os.path.join(
            tempfile.mkdtemp(prefix="vbridge-"), "voice.sock")
        self._server = None

    async def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(self._handle, self.socket_path)
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                msg = json.loads(line)
                self.received.append(msg)
                if msg.get("op") == "ask":
                    writer.write((json.dumps({"type": "ack", "id": msg["id"]}) + "\n").encode())
                    await writer.drain()
                    for item in self.script:
                        if item.get("wait"):
                            await asyncio.sleep(item["wait"])
                            continue
                        writer.write((json.dumps({**item, "id": msg["id"]},
                                                 ensure_ascii=False) + "\n").encode())
                        await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            try:
                writer.close()
            except Exception:                    # noqa: BLE001
                pass


class TestSplitSentences(unittest.TestCase):
    def test_cuts_on_cjk_terminators(self):
        out, rest = split_sentences("今天天气不错。要不要出去走走？")
        self.assertEqual(["今天天气不错。", "要不要出去走走？"], out)
        self.assertEqual("", rest)

    def test_keeps_incomplete_tail(self):
        out, rest = split_sentences("第一句。第二句还没说")
        self.assertEqual(["第一句。"], out)
        self.assertEqual("第二句还没说", rest)

    def test_lone_terminator_is_not_a_sentence(self):
        out, _ = split_sentences("。")
        self.assertEqual([], out)


class TestBridgeBrain(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeBridge([])
        await self.fake.start()
        self.brain = BridgeBrain(self.fake.socket_path, timeout=10, first_event_timeout=5)

    async def asyncTearDown(self):
        await self.fake.stop()

    async def test_deltas_are_reassembled_into_sentences(self):
        self.fake.script = [
            {"type": "delta", "text": "今天"},
            {"type": "delta", "text": "天气不错。"},
            {"type": "delta", "text": "要不要"},
            {"type": "delta", "text": "出去走走？"},
            {"type": "done", "cancelled": False},
        ]
        got = [s async for s in self.brain.astream("今天天气怎么样")]
        self.assertEqual(["今天天气不错。", "要不要出去走走？"], got)
        self.assertEqual("今天天气怎么样", self.fake.received[0]["text"])
        self.assertEqual("ask", self.fake.received[0]["op"])

    async def test_tail_without_terminator_is_still_spoken(self):
        self.fake.script = [
            {"type": "delta", "text": "我查到了"},
            {"type": "done", "cancelled": False},
        ]
        got = [s async for s in self.brain.astream("查一下")]
        self.assertEqual(["我查到了"], got)

    async def test_barge_in_sends_cancel_to_the_bridge(self):
        self.fake.script = [
            {"type": "delta", "text": "这是一段很长的话，"},
            {"wait": 0.05},
            {"type": "delta", "text": "还没说完"},
            {"type": "delta", "text": "。"},
            {"type": "done", "cancelled": False},
        ]
        stream = self.brain.astream("讲个故事")
        first = await stream.__anext__()
        self.assertIn("很长的话", first)
        await stream.aclose()                     # what a barge-in looks like
        await asyncio.sleep(0.15)
        ops = [m.get("op") for m in self.fake.received]
        self.assertIn("cancel", ops, f"bridge never told to stop: {ops}")

    async def test_error_frame_becomes_brain_unavailable(self):
        self.fake.script = [{"type": "error", "message": "上一轮还没结束"}]
        with self.assertRaises(BrainUnavailable) as ctx:
            [s async for s in self.brain.astream("你好")]
        self.assertIn("上一轮还没结束", str(ctx.exception))

    async def test_missing_socket_is_reported_not_hung(self):
        brain = BridgeBrain(os.path.join(tempfile.mkdtemp(), "nope.sock"),
                            connect_retries=0, connect_retry_delay=0)
        with self.assertRaises(BrainUnavailable):
            [s async for s in brain.astream("你好")]

    async def test_silent_bridge_times_out(self):
        self.fake.script = [{"wait": 1.0}]
        brain = BridgeBrain(self.fake.socket_path, first_event_timeout=0.2, timeout=1.0)
        with self.assertRaises(BrainUnavailable):
            [s async for s in brain.astream("你好")]


if __name__ == "__main__":
    unittest.main(verbosity=2)