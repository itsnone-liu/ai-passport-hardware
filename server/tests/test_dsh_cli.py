"""Real dsh connector smoke test (opt-in: spawns a model call).

    PASSPORT_DSH_SMOKE=1 DEEPSEEK_API_KEY=... \
      DSH_BIN=/opt/dsh-021/node_modules/.bin/dsh \
      python3 -m unittest server.tests.test_dsh_cli -v

Two facts this pins down, both of which shape the architecture:

1. Which dsh entry point actually works here. The `dsh` on PATH is a newer build
   that refuses `/root/.dsh/.credentials.yaml` (`version: 1` is an int, the plugin
   wants a string), so the *running* stack's binary (/opt/dsh-021, what the Feishu
   bridge uses) is the one to call.
2. What a turn costs. A process per turn pays node boot before the model even
   starts; that number decides whether the CLI can back a conversation.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from server.dsh.cli import DshCLIBrain  # noqa: E402

SMOKE = os.environ.get("PASSPORT_DSH_SMOKE") == "1"
BIN = os.environ.get("DSH_BIN", "/opt/dsh-021/node_modules/.bin/dsh")
KEY = os.environ.get("DEEPSEEK_API_KEY", "")


@unittest.skipUnless(SMOKE and KEY, "set PASSPORT_DSH_SMOKE=1 and DEEPSEEK_API_KEY")
class TestDshCLI(unittest.IsolatedAsyncioTestCase):
    async def test_one_shot_reply(self):
        brain = DshCLIBrain(binary=BIN, timeout=120)
        t0 = time.monotonic()
        chunks = [c async for c in brain.astream("只回答四个字：链路正常")]
        wall = time.monotonic() - t0
        text = "".join(chunks)
        print(f"\n  dsh-cli: {wall:.2f} s wall, {len(chunks)} lines -> {text[-60:]!r}")
        self.assertTrue(chunks, "no output from dsh")
        self.assertIn("链路正常", text)
        self.assertLess(wall, 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)