"""Mock brain: canned, deterministic replies.

Long replies matter for the pipeline (they exercise chunked synthesis and
cancellation), so the mock can be told to answer with N sentences.
"""
from __future__ import annotations

from typing import AsyncIterator

from server.dsh.base import Brain


class MockBrain:
    name = "mock"

    def __init__(self, reply: str = "现在是下午三点二十分。", sentences: int = 1):
        self.reply = reply
        self.sentences = max(1, sentences)
        self.calls: list[str] = []

    async def astream(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        self.calls.append(text)
        if self.sentences > 1:
            for i in range(self.sentences):
                yield f"第{i + 1}句回复内容。"
        else:
            yield self.reply

    def ask(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        return self.astream(text, session)