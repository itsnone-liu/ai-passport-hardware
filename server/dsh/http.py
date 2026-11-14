"""HTTP connector to dsh -- UNVERIFIED against the real dsh API.

The workplan forbids inventing an API: check what dsh actually exposes before
trusting this. What is implemented here is the shape any of the candidates
(SSE, JSONL, single JSON body) can be mapped onto in a few lines, so the code
around it does not have to change again.

Refuses to start without a URL, and says so loudly, rather than silently falling
back to canned replies -- a voice assistant that answers from a mock while
claiming to be talking to the agent is worse than one that reports an error.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import aiohttp

from server.dsh.base import Brain


class DshHTTPBrain:
    name = "dsh-http"

    def __init__(self, url: str, token: str = "", timeout: float = 120.0):
        if not url:
            raise ValueError("dsh_url is empty; refusing to fake a conversation")
        self.url = url
        self.token = token
        self.timeout = timeout

    async def astream(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = {"text": text, "session": session, "stream": True}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(self.url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:300]
                    raise RuntimeError(f"dsh HTTP {resp.status}: {detail}")
                ctype = resp.headers.get("Content-Type", "")
                if "text/event-stream" in ctype:
                    async for chunk in self._sse(resp):
                        yield chunk
                elif "ndjson" in ctype or "jsonl" in ctype:
                    async for chunk in self._jsonl(resp):
                        yield chunk
                else:
                    payload = await resp.json(content_type=None)
                    for chunk in self._from_object(payload):
                        yield chunk

    @staticmethod
    async def _sse(resp) -> AsyncIterator[str]:
        async for raw in resp.content:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            for chunk in DshHTTPBrain._from_object(obj):
                yield chunk

    @staticmethod
    async def _jsonl(resp) -> AsyncIterator[str]:
        async for raw in resp.content:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for chunk in DshHTTPBrain._from_object(obj):
                yield chunk

    @staticmethod
    def _from_object(obj) -> list[str]:
        """Accept the few shapes a reply can plausibly take."""
        if isinstance(obj, str):
            return [obj]
        if not isinstance(obj, dict):
            return []
        for key in ("delta", "text", "content", "reply", "answer"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return [value]
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                return [value["text"]]
        return []