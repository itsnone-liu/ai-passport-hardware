"""Turn agent prose into something a speaker can actually say.

Two problems, one module:

1. **Markup.** Agents write for screens: `**bold**`, `# headings`, fenced code,
   tables, URLs, `/root/...` paths. Read aloud, that becomes "star star bold
   star star" and "slash root slash". Measured: the first dsh reply spoken on the
   device contained `**`, backticks and a filesystem path.

2. **Length.** A 42-second reading of a screen-sized answer is not a
   conversation. Speech gets a budget; the overflow belongs on the device's
   screen, which already exists (`show_text`).

Nothing here is clever about prose -- it is deliberately a small set of rules
that can be explained and tested, because a speech filter that surprises you is
worse than one that is plainly imperfect.
"""
from __future__ import annotations

import re

#: Phrases used when a code block or a path is dropped from speech.
CODE_PLACEHOLDER = "（细节我放屏幕上了）"


def _strip_fences(text: str, in_fence: bool) -> tuple[str, bool]:
    """Remove ``` fences; a fenced block collapses to one placeholder."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if not in_fence:
                out.append(CODE_PLACEHOLDER + "\n")
            continue
        if not in_fence:
            out.append(line)
    return "".join(out), in_fence


_MD_PATTERNS = (
    (re.compile(r"`{1,3}"), ""),                      # inline code markers
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),            # bold
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)"), r"\1"),   # italic
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),     # headings
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),          # bullets
    (re.compile(r"^\s*>+\s*", re.M), ""),             # quotes
    (re.compile(r"\[(.+?)\]\((?:[^)]+)\)"), r"\1"),   # links → label
    (re.compile(r"https?://\S+"), "链接"),             # bare urls
    (re.compile(r"/[\w.\-]+/[\w./\-]*"), "路径"),      # absolute paths
    (re.compile(r"[ \t]{2,}"), " "),
    (re.compile(r"\n{2,}"), "\n"),
)


def clean_for_speech(text: str, *, in_fence: bool = False) -> tuple[str, bool]:
    """Return (speakable text, fence state) for one piece of agent output.

    `in_fence` must be carried across calls: a code block can span several
    chunks, and speaking the middle of it would be worse than skipping it.
    """
    if not text:
        return "", in_fence
    text, in_fence = _strip_fences(text, in_fence)
    text = text.replace("\r", "")
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    # table separators and pipes read as noise
    text = re.sub(r"^\s*\|?[\s:\-|]{4,}\|?\s*$", "", text, flags=re.M)
    text = text.replace("|", " ")
    return text.strip(), in_fence


#: Characters per second of Mandarin TTS, measured on this device's voice
#: (72,960 bytes of 16 kHz PCM for a 32-character sentence ≈ 4.4 chars/s).
CHARS_PER_SECOND = 4.4


class SpeechBudget:
    """Caps how much gets read aloud; the rest is reported back to the caller."""

    def __init__(self, max_seconds: float = 45.0):
        self.max_chars = max(20, int(max_seconds * CHARS_PER_SECOND))
        self.spoken_chars = 0
        self.truncated = False
        self.overflow = ""

    @property
    def exhausted(self) -> bool:
        return self.spoken_chars >= self.max_chars

    def take(self, text: str) -> str:
        """Return the part of `text` that fits; stash the rest as overflow."""
        if not text:
            return ""
        if self.exhausted:
            self.truncated = True
            self.overflow += text
            return ""
        room = self.max_chars - self.spoken_chars
        if len(text) <= room:
            self.spoken_chars += len(text)
            return text
        # Cut at the last sentence end inside the room so we never stop mid-word.
        cut = text[:room]
        for sep in ("。", "！", "？", "；", "\n", "，", " "):
            pos = cut.rfind(sep)
            if pos >= room // 2:
                cut = cut[: pos + 1]
                break
        rest = text[len(cut):]
        self.spoken_chars += len(cut)
        self.truncated = True
        self.overflow += rest
        return cut

    def tail_note(self) -> str:
        """Spoken closing line when the answer did not fit."""
        return "后面我放屏幕上了。" if self.truncated else ""