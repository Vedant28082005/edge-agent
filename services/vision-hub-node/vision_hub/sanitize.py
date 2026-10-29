"""Everything the VLM says is untrusted data. Only closed, bounded, instruction-free values leave this module."""
from __future__ import annotations

import re
import unicodedata

_SPACES = re.compile(r" {2,}")
_LABEL_BAD = re.compile(r"[^a-z0-9 \-]")
# Words that make a label look like an instruction, a prompt fragment, a tool call, a URL or a device reference.
_INSTRUCTION_LIKE = re.compile(
    r"\b(ignore|disregard|forget|override|previous|instruction|instructions|prompt|system|assistant|user|"
    r"unlock|open the|turn (on|off)|disable|enable|execute|run|delete|send|call|tool|function|admin|password|"
    r"token|secret|http|https|www|json|script|please|must|should|you)\b")
_CTRL_OR_FORMAT = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")

MAX_LABEL_LEN = 24
MAX_LABEL_WORDS = 3
MAX_OBJS = 3


def clean_question(q: str, max_len: int = 200) -> str:
    """A caller's question as prompt DATA: control/format characters removed, whitespace collapsed, `<` `>` replaced so
    no chat-template special token can be formed, cut to max_len."""
    s = "".join(" " if ch.isspace() else ch for ch in q
                if unicodedata.category(ch) not in _CTRL_OR_FORMAT or ch.isspace())
    s = s.replace("<", " ").replace(">", " ")
    return _SPACES.sub(" ", s).strip()[:max_len].strip()


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def clean_label(raw: object) -> str | None:
    """One object label: lowercase a-z0-9 space hyphen, <= 3 words, <= 24 chars, nothing instruction-like. None = drop."""
    if not isinstance(raw, str):
        return None
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii").lower()
    if _INSTRUCTION_LIKE.search(s):
        return None
    s = _LABEL_BAD.sub(" ", s)
    s = _SPACES.sub(" ", s).strip(" -")
    if not s or len(s) > MAX_LABEL_LEN or len(s.split()) > MAX_LABEL_WORDS:
        return None
    return s


def clean_objs(raw: object) -> list[str]:
    """Labels cleaned, de-duplicated (case and trailing-s insensitive), at most 3."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        label = clean_label(item)
        if label is None:
            continue
        key = " ".join(_singular(w) for w in label.split())
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) == MAX_OBJS:
            break
    return out
