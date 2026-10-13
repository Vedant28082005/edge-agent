"""Small shared helpers: sanitizing, ids, canonical JSON, secret redaction, clock."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time
import unicodedata
from decimal import Decimal
from typing import Any

ENDPOINT_ID_RE = re.compile(r"^ep_[0-9a-f]{8}$")
SCENE_ID_RE = re.compile(r"^sc_[0-9a-f]{8}$")
NODE_ID_RE = re.compile(r"^esp32-[0-9a-f]{12}$")
LOCAL_ID_RE = re.compile(r"^[a-z0-9_]{1,16}$")
CMD_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
BY_RE = re.compile(r"^(user:[A-Za-z0-9_.-]{1,64}|agent|scene:[A-Za-z0-9_-]{1,32}|rule:[A-Za-z0-9_-]{1,32})$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class Clock:
    """Injectable clock so TTL logic is testable without sleeping."""

    def now(self) -> float:
        return time.time()


class ManualClock(Clock):
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def sanitize_text(value: Any, max_len: int = 48) -> str:
    """Make an untrusted/admin string safe to store and show: NFKC, drop control/format/bidi characters,
    collapse whitespace, strip, truncate. Never raises."""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = unicodedata.normalize("NFKC", value)
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " \t")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_len]


def safe_repr(value: Any, max_len: int = 120) -> str:
    """Bounded, escaped rendering of untrusted data for log lines."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:  # noqa: BLE001
        text = repr(value)
    text = text.encode("ascii", "backslashreplace").decode("ascii")
    text = re.sub(r"[\x00-\x1f\x7f]", "?", text)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def new_endpoint_id() -> str:
    return "ep_" + secrets.token_hex(4)


def new_scene_id() -> str:
    return "sc_" + secrets.token_hex(4)


def new_session_id() -> str:
    return "ps_" + secrets.token_hex(8)


def random_secret(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def _jcs_number(v: float | int) -> str:
    if isinstance(v, bool):
        raise TypeError("bool is not a number")
    if isinstance(v, int):
        return str(v)
    if not math.isfinite(v):
        raise ValueError("non-finite number in canonical JSON")
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if "e" in r or "E" in r:
        # ordinary magnitudes only; expand the exponent (good for 1e-05 .. 1e+20)
        return format(Decimal(r), "f")
    return r


def canonical_json(obj: Any) -> str:
    """RFC 8785 (JCS) subset sufficient for action objects: sorted keys (UTF-16 code unit order), no whitespace,
    minimal string escaping, ECMAScript-style numbers (3.0 -> 3)."""
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, (int, float)):
        return _jcs_number(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(canonical_json(x) for x in obj) + "]"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(canonical_json(k) + ":" + canonical_json(v) for k, v in items) + "}"
    raise TypeError(f"cannot canonicalise {type(obj)}")


def action_hash(action: dict) -> str:
    return hashlib.sha256(canonical_json(action).encode("utf-8")).hexdigest()


def set_state_action(endpoint_id: str, capability: str, value: Any) -> dict:
    return {"type": "set_state", "endpoint_id": endpoint_id, "capability": capability, "value": value}


def run_scene_action(scene_id: str, risky: list[dict]) -> dict:
    """risky = [{endpoint_id, capability, value}]. Sorted ascending by endpoint_id then capability; exact duplicates
    are collapsed. Entries carry exactly the three fields."""
    seen: dict[tuple, dict] = {}
    for a in risky:
        entry = {"endpoint_id": a["endpoint_id"], "capability": a["capability"], "value": a["value"]}
        seen[(entry["endpoint_id"], entry["capability"], canonical_json(entry["value"]))] = entry
    actions = sorted(seen.values(), key=lambda e: (e["endpoint_id"], e["capability"], canonical_json(e["value"])))
    return {"type": "run_scene", "scene_id": scene_id, "actions": actions}


# ------------------------------------------------------------------------------------------ secret redaction
class SecretRegistry:
    """Process-wide set of secret strings that must never reach a log line. Values are registered when they are
    created or read (MQTT passwords, service secret, Wi-Fi password, confirmation tokens). Short-lived ones (tokens,
    Wi-Fi passwords seen in requests) live in a bounded FIFO so a long-running hub does not grow without limit."""

    RECENT_MAX = 1024

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._permanent: set[str] = set()
        self._recent: dict[str, None] = {}

    def add(self, value: str | None, permanent: bool = True) -> None:
        if value and len(value) >= 6:
            with self._lock:
                if permanent:
                    self._permanent.add(value)
                else:
                    self._recent.pop(value, None)
                    self._recent[value] = None
                    while len(self._recent) > self.RECENT_MAX:
                        self._recent.pop(next(iter(self._recent)))

    def scrub(self, text: str) -> str:
        with self._lock:
            known = list(self._permanent) + list(self._recent)
        for s in known:
            if s in text:
                text = text.replace(s, "[REDACTED]")
        return text


SECRETS = SecretRegistry()

_TOKEN_LIKE = re.compile(
    r"(?i)(confirmation_token|x-service-secret|password|secret|token)(['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9_\-+/=.]{6,})"
)


def redact(text: str) -> str:
    return _TOKEN_LIKE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", SECRETS.scrub(text))


def install_log_redaction() -> None:
    """Scrub registered secrets and key=value secret shapes from every log record, whatever handler prints it
    (done in the record factory so pytest's caplog and any later handler see only the scrubbed text)."""
    old_factory = logging.getLogRecordFactory()
    if getattr(old_factory, "_dh_redacting", False):
        return

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003
        rec = old_factory(*args, **kwargs)
        try:
            msg = rec.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(rec.msg)
        rec.msg = redact(msg)
        rec.args = None
        return rec

    factory._dh_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
