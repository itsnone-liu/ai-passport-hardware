"""dsh connector contract.

The workplan is explicit: investigate the real dsh entry point, do not invent an
API. Until that investigation lands, the service talks to this interface and the
mock backend keeps the loop honest. A `dsh` backend is provided for the case
where dsh exposes an HTTP endpoint; it is marked unverified and refuses to run
without an explicit URL.
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol


class Brain(Protocol):
    name: str

    def ask(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        """Yield reply text chunks as they become available.

        Chunking matters: each chunk is synthesised as soon as it arrives, so a
        long answer starts speaking before it is finished.
        """
        ...