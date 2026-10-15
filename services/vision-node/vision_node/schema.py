"""BoundedSchema handling: semantic checks the contract schema cannot express, conversion to the JSON schema handed to
llama-server (grammar-constrained decoding), and strict validation + sanitising of what the model returned."""
from __future__ import annotations

import math
from typing import Any

from .errors import ApiError
from .sanitize import safe_line


def check_bounded(schema: dict) -> None:
    """Called after the contract validator accepted the shape. min <= max, `required` only names declared fields."""
    props = schema["properties"]
    for name in schema.get("required", []):
        if name not in props:
            raise ApiError("bad_request")
    for spec in props.values():
        if spec["type"] in ("integer", "number") and spec["minimum"] > spec["maximum"]:
            raise ApiError("bad_request")


def to_json_schema(schema: dict) -> dict:
    """Closed JSON schema for llama-server's `response_format`. No additionalProperties; every field required in the
    grammar (a model that may omit fields tends to emit `{}`), the caller's `required` is enforced on top of that
    by validate_output only for fields the model could not know; here all declared fields are produced."""
    props: dict[str, Any] = {}
    for name, spec in schema["properties"].items():
        t = spec["type"]
        if t == "boolean":
            props[name] = {"type": "boolean"}
        elif t in ("integer", "number"):
            props[name] = {"type": t, "minimum": spec["minimum"], "maximum": spec["maximum"]}
        elif t == "string" and "enum" in spec:
            props[name] = {"type": "string", "enum": list(spec["enum"])}
        elif t == "string":
            props[name] = {"type": "string", "maxLength": spec["maxLength"]}
        else:   # array of enum strings
            props[name] = {"type": "array", "maxItems": spec["maxItems"],
                           "items": {"type": "string", "enum": list(spec["items"]["enum"])}}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def validate_output(schema: dict, raw: Any) -> dict:
    """Strict check of the model's object against the REQUEST schema: exact field set (no unknown field), types, bounds,
    enums, lengths. Free strings are reduced to the contract's SafeValue alphabet. Raises ValueError on any violation
    (the caller retries once, then 502 schema_violation). Never echoes the value."""
    if not isinstance(raw, dict):
        raise ValueError
    props = schema["properties"]
    if any(k not in props for k in raw):
        raise ValueError
    if any(k not in raw for k in schema.get("required", [])):
        raise ValueError
    out: dict[str, Any] = {}
    for name, val in raw.items():
        spec = props[name]
        t = spec["type"]
        if t == "boolean":
            if not isinstance(val, bool):
                raise ValueError
            out[name] = val
        elif t == "integer":
            if isinstance(val, bool) or not isinstance(val, int) or not spec["minimum"] <= val <= spec["maximum"]:
                raise ValueError
            out[name] = val
        elif t == "number":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) \
                    or not spec["minimum"] <= val <= spec["maximum"]:
                raise ValueError
            out[name] = val
        elif t == "string" and "enum" in spec:
            if not isinstance(val, str) or val not in spec["enum"]:
                raise ValueError
            out[name] = val
        elif t == "string":
            if not isinstance(val, str):
                raise ValueError
            text, _ = safe_line(val, min(spec["maxLength"], 64))
            out[name] = text
        else:
            if not isinstance(val, list) or len(val) > spec["maxItems"] \
                    or any(not isinstance(v, str) or v not in spec["items"]["enum"] for v in val):
                raise ValueError
            out[name] = list(val)
    return out


def mock_value(schema: dict) -> dict:
    """Deterministic schema-valid object for the mock backend."""
    out: dict[str, Any] = {}
    for name, spec in schema["properties"].items():
        t = spec["type"]
        if t == "boolean":
            out[name] = False
        elif t in ("integer", "number"):
            out[name] = spec["minimum"]
        elif t == "string" and "enum" in spec:
            out[name] = spec["enum"][0]
        elif t == "string":
            out[name] = "ok"
        else:
            out[name] = []
    return out
