"""Secret handling, log redaction, untrusted-string sanitising, token estimate, canonical JSON."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
from pathlib import Path

# ----------------------------------------------------------------------------------------------- secrets / logs
_SECRETS: set[str] = set()
# opaque confirmation tokens are 256-bit base64url (43 chars); never let anything that looks like one reach a log
_TOKENISH = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return _TOKENISH.sub("***", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg, record.args = redact(msg), None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return True


def install_log_redaction() -> None:
    f = RedactingFilter()
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx INFO lines contain URLs only, keep them quiet
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def read_secret_file(path: str) -> str:
    """Secrets are read from a file only (never env/argv) and registered with the log redactor."""
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("secret file is empty")
    register_secret(value)
    return value


# ----------------------------------------------------------------------------------------------- untrusted strings
_CTRL_TOKENS = re.compile(r"<\|[^|>]{0,40}\|>|\[/?INST\]|<<?/?SYS>>?|</?s>")
_LABEL_OK = re.compile(r"[^\w .'&-]", re.UNICODE)


def clean_label(value: object, max_len: int = 32) -> str:
    """Admin labels (names, rooms, tags) and anything device-derived: NFKC, drop everything but letters, digits,
    space and . ' & - _ (so no newlines, quotes, brackets, pipes, backticks, angle brackets or template tokens),
    collapse whitespace, length-limit. The result is data, never an instruction: it is only ever placed inside a
    fenced device-list line."""
    if not isinstance(value, str):
        return ""
    s = unicodedata.normalize("NFKC", value)
    s = _CTRL_TOKENS.sub(" ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = _LABEL_OK.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len].strip()


def clean_user_text(value: str, max_chars: int) -> str:
    """The user's own words: kept readable, but chat-template control tokens and control characters are removed and
    the length is capped (it is the 'current' prompt section, <= 100 tokens)."""
    s = unicodedata.normalize("NFKC", value or "")
    s = _CTRL_TOKENS.sub(" ", s)
    s = "".join(ch if (ch in "\n\t" or unicodedata.category(ch)[0] != "C") else " " for ch in s)
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:max_chars]


def clean_tag(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]{1,24}", value) else ""


EP_RE = re.compile(r"^ep_[0-9a-f]{8}$")
ENUM_VAL_RE = re.compile(r"^[a-z0-9_]{1,24}$")


# ----------------------------------------------------------------------------------------------- tokens [est]
def est_tokens(text: str) -> int:
    """[est] Conservative token estimate for Qwen-style BPE on this prompt mix (ids, JSON, short labels): 1 token per
    3 UTF-8 bytes, rounded up. Over-counts plain English (about 4 chars/token) on purpose, so the budgets hold with
    margin. The real count is measured with llama-server /tokenize in the llama test."""
    return math.ceil(len(text.encode("utf-8")) / 3.0)


def truncate_tokens(text: str, limit: int) -> str:
    if est_tokens(text) <= limit:
        return text
    cut = text.encode("utf-8")[: max(0, limit * 3 - 1)].decode("utf-8", "ignore")
    return cut.rstrip()


# ----------------------------------------------------------------------------------------------- RFC 8785 subset
def _jcs_num(v: int | float) -> str:
    if isinstance(v, bool):
        raise TypeError
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e21):
        return str(int(v))
    r = repr(float(v))
    if "e" in r or "E" in r:
        raise ValueError("exponent form not supported for action values")
    return r


def jcs(obj: object) -> str:
    """RFC 8785 canonical JSON for the value types an action contains (strings, ints, finite floats, bools, lists,
    dicts). Keys sorted by UTF-16 code unit."""
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if obj is None:
        return "null"
    if isinstance(obj, (int, float)):
        return _jcs_num(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(jcs(x) for x in obj) + "]"
    if isinstance(obj, dict):
        keys = sorted(obj, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + jcs(obj[k]) for k in keys) + "}"
    raise TypeError(type(obj))


def action_hash(action: dict) -> str:
    return hashlib.sha256(jcs(action).encode("utf-8")).hexdigest()
