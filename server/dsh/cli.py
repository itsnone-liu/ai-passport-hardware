"""dsh connector via the documented one-shot CLI: `dsh --profile headless "<task>"`.

This is the entry point dsh actually documents ("answer one task, print the
result, and exit"), so it needs no invented API -- but it is a *process per
turn*, which is expensive: node boot plus a full model round trip. Whether that
fits a voice loop is a measurement, not an opinion, so the adapter records its
own latency and the smoke test reports it.

If the measured cost is too high for conversation, the fix is a persistent
session (the Feishu bridge or an ACP session), not a faster spawn: the interface
below stays the same.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import AsyncIterator

from server.dsh.base import Brain

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class DshCLIBrain:
    name = "dsh-cli"

    def __init__(self, profile: str = "headless", binary: str = "dsh",
                 timeout: float = 120.0, extra_args: list[str] | None = None,
                 cwd: str | None = None):
        self.profile = profile
        self.binary = binary
        self.timeout = timeout
        self.extra_args = extra_args or []
        self.cwd = cwd
        self.last_latency_ms = 0

    async def astream(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        cmd = [self.binary, "--profile", self.profile, *self.extra_args, text]
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=self.cwd)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            assert proc.stdout is not None
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
                if not line:
                    break
                clean = ANSI.sub("", line.decode("utf-8", "replace")).rstrip()
                if clean:
                    yield clean
            await asyncio.wait_for(proc.wait(), timeout=self.timeout)
        finally:
            self.last_latency_ms = int((loop.time() - t0) * 1000)
            if proc.returncode is None:
                proc.kill()
                with asyncio_timeout_suppress():
                    await proc.wait()


class asyncio_timeout_suppress:
    """contextlib.suppress(asyncio.TimeoutError) as a context manager alias."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is asyncio.TimeoutError