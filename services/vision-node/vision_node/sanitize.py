"""Output hygiene for everything a model produced. The contract's ASCII allowlist limits the alphabet, not the meaning,
so the agent still treats the text as untrusted data; here we only guarantee the alphabet, one line and the bounds."""
from __future__ import annotations

import re
import unicodedata

_DISALLOWED = re.compile(r"[^A-Za-z0-9 .,;:!?'()%/_+\-]")
_SPACES = re.compile(r" {2,}")
ALLOWED_FULL = re.compile(r"[A-Za-z0-9 .,;:!?'()%/_+\-]*")


def safe_line(text: str, max_len: int) -> tuple[str, bool]:
    """ASCII allowlist, whitespace (incl. newlines/tabs) -> single space, collapsed, stripped, cut to max_len.
    Returns (text, was_cut). Control characters, quotes, brackets, backticks, <>, $, @ and non-ASCII are removed."""
    s = re.sub(r"\s+", " ", text)
    s = _DISALLOWED.sub("", s)
    s = _SPACES.sub(" ", s).strip()
    cut = len(s) > max_len
    return s[:max_len].rstrip(), cut


_CTRL_OR_FORMAT = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def clean_question(q: str) -> str:
    """The user's question as VLM prompt DATA: control/format characters removed, whitespace collapsed, and `<` `>`
    replaced so it can never form a chat-template special token (llama-server parses special tokens in the rendered
    prompt) or a closing delimiter of the prompt's data block."""
    s = "".join(" " if ch.isspace() else ch for ch in q if unicodedata.category(ch) not in _CTRL_OR_FORMAT or ch.isspace())
    s = s.replace("<", " ").replace(">", " ")
    return _SPACES.sub(" ", s).strip()


_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_JUNK = {"", "you", ".", "..", "..."}


def filter_transcript(raw: str, max_len: int = 1000) -> tuple[str, bool]:
    """Whisper junk filter. Returns (text, filtered). Non-speech annotations ([BLANK_AUDIO], (music)), music notes and the
    classic silence hallucination `you` are dropped; the rest goes through the allowlist."""
    s = _BRACKETED.sub(" ", raw)
    s = re.sub(r"[♪♫♬♩]+", " ", s)
    text, _ = safe_line(s, max_len)
    if text.lower().strip(" .!?,") in {j.strip(" .") for j in _JUNK}:
        return "", True
    return text, False
