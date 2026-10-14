"""Contract access: JSON Schema validators loaded from the repo's contracts/ folder, the capability vocabulary,
hub-enforced announce rules (capabilities.md) and value coercion/range checks shared by executor, scenes and state."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .util import LOCAL_ID_RE, NODE_ID_RE, safe_repr

MAX_PAYLOAD_BYTES = 16 * 1024
ENDPOINT_TYPES = ("light", "fan", "outlet", "lock", "cover", "thermostat", "sensor", "generic")

# Fixed vocabulary (capabilities.md). Defaults fill what the announce may omit.
VOCAB: dict[str, dict[str, Any]] = {
    "power": {"kind": "actuator", "type": "bool"},
    "brightness": {"kind": "actuator", "type": "int", "min": 0, "max": 100},
    "color_temp": {"kind": "actuator", "type": "int"},
    "speed": {"kind": "actuator", "type": "int"},
    "position": {"kind": "actuator", "type": "int", "min": 0, "max": 100},
    "lock": {"kind": "actuator", "type": "enum", "values": ["locked", "unlocked"]},
    "target_temp": {"kind": "actuator", "type": "float"},
    "mode": {"kind": "actuator", "type": "enum"},
    "temperature": {"kind": "sensor", "type": "float"},
    "humidity": {"kind": "sensor", "type": "float"},
    "illuminance": {"kind": "sensor", "type": "float"},
    "motion": {"kind": "sensor", "type": "bool"},
    "contact": {"kind": "sensor", "type": "enum", "values": ["open", "closed"]},
    "power_w": {"kind": "sensor", "type": "float"},
    "current_a": {"kind": "sensor", "type": "float"},
    "voltage_v": {"kind": "sensor", "type": "float"},
    "battery": {"kind": "sensor", "type": "int", "min": 0, "max": 100},
}


class AnnounceRejected(Exception):
    """Raised with a short, log-safe reason."""


def find_contracts_dir(hint: str | None = None) -> Path:
    candidates = []
    if hint:
        candidates.append(Path(hint))
    if os.environ.get("EDGE_CONTRACTS_DIR"):
        candidates.append(Path(os.environ["EDGE_CONTRACTS_DIR"]))
    here = Path(__file__).resolve()
    candidates += [p / "contracts" for p in here.parents]
    for c in candidates:
        if (c / "announce.schema.json").is_file():
            return c
    raise FileNotFoundError("contracts/ folder not found (set config.contracts_dir or EDGE_CONTRACTS_DIR)")


class Contracts:
    def __init__(self, contracts_dir: str | Path | None = None) -> None:
        self.dir = find_contracts_dir(str(contracts_dir) if contracts_dir else None)
        self.announce = self._load("announce.schema.json")
        self.state = self._load("state.schema.json")
        self.cmd = self._load("cmd.schema.json")
        self.ack = self._load("ack.schema.json")

    def _load(self, name: str) -> Draft202012Validator:
        schema = json.loads((self.dir / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    @staticmethod
    def _first_error(validator: Draft202012Validator, doc: Any) -> str | None:
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
        if not errors:
            return None
        e = errors[0]
        path = "/".join(str(p) for p in e.absolute_path) or "(root)"
        # e.message may embed device-supplied values: bound and escape it
        return f"{path}: {safe_repr(e.message, 100)}"

    def validate_state(self, doc: Any) -> str | None:
        return self._first_error(self.state, doc)

    def validate_ack(self, doc: Any) -> str | None:
        return self._first_error(self.ack, doc)

    def validate_cmd(self, doc: Any) -> str | None:
        return self._first_error(self.cmd, doc)

    # ------------------------------------------------------------------------------------------ announce
    def parse_announce(self, payload: bytes, topic_node_id: str) -> dict:
        """Full announce validation: size, JSON, schema 2, hub rules. Returns the normalised announce or raises
        AnnounceRejected(reason). Never trusts anything beyond what the schema allows."""
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise AnnounceRejected(f"payload too large ({len(payload)} bytes > {MAX_PAYLOAD_BYTES})")
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AnnounceRejected("not valid UTF-8 JSON") from None
        err = self._first_error(self.announce, doc)
        if err:
            raise AnnounceRejected(f"schema: {err}")
        if doc["node_id"] != topic_node_id:
            raise AnnounceRejected("node_id does not match the topic id")
        seen_local: set[str] = set()
        for ep in doc["endpoints"]:
            if ep["local_id"] in seen_local:
                raise AnnounceRejected(f"duplicate local_id {ep['local_id']}")
            seen_local.add(ep["local_id"])
            seen_caps: set[str] = set()
            for cap in ep["capabilities"]:
                if cap["id"] in seen_caps:
                    raise AnnounceRejected(f"duplicate capability {cap['id']} on {ep['local_id']}")
                seen_caps.add(cap["id"])
                self._check_cap(ep["local_id"], cap)
        return doc

    @staticmethod
    def _check_cap(local_id: str, cap: dict) -> None:
        where = f"{local_id}.{cap['id']}"
        lo, hi = cap.get("min"), cap.get("max")
        if cap["type"] == "int":
            for k in ("min", "max"):
                if k in cap and (isinstance(cap[k], bool) or float(cap[k]) != int(cap[k])):
                    raise AnnounceRejected(f"{where}: {k} must be an integer for int capability")
        if lo is not None and hi is not None and lo > hi:
            raise AnnounceRejected(f"{where}: min > max")
        norm = normalise_capability(cap)
        if cap["kind"] == "actuator":
            for k in ("boot_value", "failsafe_value"):
                if k in cap:
                    ok, _code, _v = coerce_value(norm, cap[k])
                    if not ok:
                        raise AnnounceRejected(f"{where}: {k} outside the declared range or enum")
        if cap["type"] == "enum" and cap["id"] == "mode" and not cap.get("values"):
            raise AnnounceRejected(f"{where}: mode requires values")


def normalise_capability(cap: dict) -> dict:
    """Capability dict as stored/served: vocabulary defaults filled, only known keys, values sanitised by schema."""
    spec = VOCAB[cap["id"]]
    out: dict[str, Any] = {"id": cap["id"], "kind": cap["kind"], "type": cap["type"]}
    lo = cap.get("min", spec.get("min"))
    hi = cap.get("max", spec.get("max"))
    if cap["type"] in ("int", "float"):
        if lo is not None:
            out["min"] = int(lo) if cap["type"] == "int" else lo
        if hi is not None:
            out["max"] = int(hi) if cap["type"] == "int" else hi
    if cap["type"] == "enum":
        out["values"] = list(cap.get("values") or spec.get("values") or [])
    if cap.get("unit"):
        out["unit"] = cap["unit"]
    for k in ("interval_s", "boot_value", "failsafe_value", "max_on_s", "settle_s"):
        if k in cap:
            out[k] = cap[k]
    return out


def coerce_value(cap: dict, value: Any) -> tuple[bool, str | None, Any]:
    """Check `value` against a normalised capability. Returns (ok, error_code, coerced_value).
    error_code is one of bad_type | out_of_range (mcp-tools.md set_state error codes)."""
    t = cap["type"]
    if t == "bool":
        return (True, None, value) if isinstance(value, bool) else (False, "bad_type", value)
    if t in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, "bad_type", value
        if isinstance(value, float) and not math.isfinite(value):
            return False, "bad_type", value
        if t == "int":
            if isinstance(value, float):
                if value != int(value):
                    return False, "bad_type", value
                value = int(value)
        else:
            value = float(value) if not isinstance(value, int) else value
        if "min" in cap and value < cap["min"]:
            return False, "out_of_range", value
        if "max" in cap and value > cap["max"]:
            return False, "out_of_range", value
        return True, None, value
    if t == "enum":
        if not isinstance(value, str):
            return False, "bad_type", value
        if value not in cap.get("values", []):
            return False, "out_of_range", value
        return True, None, value
    return False, "bad_type", value


def values_equal(cap: dict, a: Any, b: Any) -> bool:
    t = cap["type"]
    if t == "bool":
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if t in ("int", "float"):
        if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float)) \
                or not isinstance(b, (int, float)):
            return False
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-6)
    return isinstance(a, str) and a == b


def valid_node_id(s: Any) -> bool:
    return isinstance(s, str) and bool(NODE_ID_RE.match(s))


def valid_local_id(s: Any) -> bool:
    return isinstance(s, str) and bool(LOCAL_ID_RE.match(s))
