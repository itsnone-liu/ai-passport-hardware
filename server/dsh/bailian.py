"""百炼（阿里云 DashScope / token-plan）大模型连接器。

为什么不用现成的两条路：
- `dsh-cli`：实测 5.1 s/轮（node 启动 2.5 s），对话等不起；
- `dsh`（HTTP）：需要一个还没确认的 dsh 端点。
而 ASR/TTS 已经在用这个平台的凭证与域名，LLM 走同一份凭证最省事，
也少一个会失败的依赖。

走 OpenAI 兼容流式接口：第一个字一到就开始合成，用户不用等整句生成完。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import aiohttp

from server.dsh.base import Brain

DEFAULT_SYSTEM = (
    "你是一台随身语音助手的对话引擎。你的回答会被直接念出来，所以："
    "只说话，不用 Markdown、不用列表、不用括号注释；"
    "一到三句话说完，口语化，不要复述用户的问题；"
    "不确定就直说不确定。"
)


class BailianLLMBrain:
    """流式对话。按 session 保留最近若干轮，设备才能连续对话而不是每句都失忆。"""

    name = "bailian-llm"

    def __init__(self, api_key: str, base_url: str,
                 model: str = "deepseek-v4.1-flash-1017",
                 system: str = DEFAULT_SYSTEM, timeout: float = 30.0,
                 history_turns: int = 6, max_tokens: int = 300,
                 enable_thinking: bool = False):
        if not api_key:
            raise ValueError("bailian brain 需要 API key；没有就退回 mock，别假装会思考")
        self.api_key = api_key
        self.url = base_url.rstrip("/") + "/compatible-mode/v1/chat/completions"
        self.model = model
        self.system = system
        self.timeout = timeout
        self.history_turns = max(0, history_turns)
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self._history: dict[str, list[dict]] = {}
        self.calls: list[str] = []

    def _messages(self, text: str, session: str) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": self.system}]
        msgs.extend(self._history.get(session, []))
        msgs.append({"role": "user", "content": text})
        return msgs

    def _remember(self, session: str, user: str, reply: str) -> None:
        if self.history_turns <= 0:
            return
        hist = self._history.setdefault(session, [])
        hist.append({"role": "user", "content": user})
        hist.append({"role": "assistant", "content": reply})
        # 只留最近 history_turns 轮（一问一答算两条）
        keep = self.history_turns * 2
        if len(hist) > keep:
            del hist[:len(hist) - keep]

    async def astream(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        self.calls.append(text)
        body = {
            "model": self.model,
            "messages": self._messages(text, session),
            "stream": True,
            "max_tokens": self.max_tokens,
            "temperature": 0.6,
        }
        # deepseek 的思考模式默认打开，实测把首字延迟从 3~4 s 推到 1.2 s 以下；
        # 语音对话等不起思考，关掉。关掉后若平台仍只回 reasoning_content，
        # 下面会明确报错，而不是把一段"内心独白"念给用户听。
        if not self.enable_thinking:
            body["enable_thinking"] = False
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        parts: list[str] = []
        reasoning_seen = 0
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(self.url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:300]
                    raise RuntimeError(f"bailian LLM HTTP {resp.status}: {detail}")
                buf = b""
                async for raw in resp.content.iter_any():
                    buf += raw
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            buf = b""
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            parts.append(piece)
                            yield piece
                        elif delta.get("reasoning_content"):
                            # 思考模式的内容：不能拿去合成，会念出"内心独白"
                            reasoning_seen += len(delta["reasoning_content"])
        reply = "".join(parts).strip()
        if not reply and reasoning_seen:
            raise RuntimeError(
                f"bailian LLM 只回了思考内容({reasoning_seen} 字)、没有正文；"
                "enable_thinking 没生效，别把内心独白念给用户")
        if reply:
            self._remember(session, text, reply)

    def ask(self, text: str, session: str = "passport") -> AsyncIterator[str]:
        return self.astream(text, session)