"""Real Bailian round trip: TTS speaks, ASR listens, and the text must come back.

Opt-in because it costs money and needs network. Skipped unless TOKENPLAN_API_KEY
is set:

    TOKENPLAN_API_KEY=sk-sp-... python3 -m unittest server.tests.test_bailian_smoke -v

This is the test that keeps the two adapters honest: it exercises the exact
endpoint shapes verified on 2026-11-14 (SSE SpeechSynthesizer for TTS, the
OpenAI-Realtime WebSocket for ASR) and would fail loudly if either model moves.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from server.tts.bailian import BailianTTS        # noqa: E402
from server.asr.bailian import BailianRealtimeASR  # noqa: E402

KEY = os.environ.get("TOKENPLAN_API_KEY", "") or os.environ.get("DASHSCOPE_API_KEY", "")
BASE = os.environ.get("BAILIAN_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com")
PHRASE = "你好，我是护照，现在测试语音往返。"


@unittest.skipUnless(KEY, "set TOKENPLAN_API_KEY to run the real-Bailian smoke test")
class TestBailianRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def test_tts_then_asr(self):
        tts = BailianTTS(KEY, BASE, "qwen-audio-3.0-tts-plus", "longanlingxin")
        t0 = time.monotonic()
        chunks, first = [], None
        async for chunk in tts.astream(PHRASE):
            if first is None:
                first = time.monotonic() - t0
            chunks.append(chunk)
        pcm = b"".join(chunks)
        self.assertGreater(len(pcm), 16000, "less than half a second of audio")
        self.assertEqual(0, len(pcm) % 2, "pcm_s16le must be an even byte count")
        print(f"\n  TTS: first chunk {first*1000:.0f} ms, {len(pcm)/32000:.2f} s audio")

        asr = BailianRealtimeASR(KEY, BASE, "qwen-audio-3.0-realtime-plus")
        t1 = time.monotonic()
        result = await asr.transcribe(pcm)
        print(f"  ASR: {result.latency_ms} ms -> {result.text!r}")
        self.assertGreater(len(result.text), 4, "empty transcript")
        self.assertIn("护照", result.text, "round trip lost the distinguishing word")
        self.assertLess(time.monotonic() - t1, 30)

    async def test_tts_streams_faster_than_realtime(self):
        """If TTS cannot beat real time, 'start speaking while generating' is a lie."""
        tts = BailianTTS(KEY, BASE, "qwen-audio-3.0-tts-plus", "longanlingxin")
        text = "早上好。现在是七点二十分，室外温度十二度，今天有小雨，出门记得带伞。"
        t0 = time.monotonic()
        total = 0
        async for chunk in tts.astream(text):
            total += len(chunk)
        wall = time.monotonic() - t0
        audio = total / 32000
        print(f"\n  TTS: {audio:.2f} s audio in {wall:.2f} s wall")
        self.assertGreater(audio, wall * 1.2, "TTS slower than real time")


if __name__ == "__main__":
    unittest.main(verbosity=2)