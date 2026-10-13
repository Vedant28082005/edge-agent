"""Loads the contract JSON Schemas and validates every outgoing message against them."""
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator

KINDS = {"announce": "announce.schema.json", "state": "state.schema.json", "ack": "ack.schema.json"}
MAX_PAYLOAD = 16 * 1024


class SchemaViolation(Exception):
    pass


def default_contracts_dir() -> Path:
    env = os.environ.get("SIM_CONTRACTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "contracts"


class Validator:
    def __init__(self, contracts_dir: Path | None = None):
        d = Path(contracts_dir) if contracts_dir else default_contracts_dir()
        self._v = {}
        for kind, fname in KINDS.items():
            schema = json.loads((d / fname).read_text(encoding="utf-8"))
            self._v[kind] = Draft202012Validator(schema)

    def validate(self, kind: str, payload) -> None:
        errs = sorted(self._v[kind].iter_errors(payload), key=lambda e: list(e.path))
        if errs:
            e = errs[0]
            where = "/".join(str(p) for p in e.path) or "<root>"
            raise SchemaViolation(f"{kind} violates contract at {where}: {e.message[:120]}")
        size = len(json.dumps(payload, separators=(",", ":")).encode())
        if size > MAX_PAYLOAD:
            raise SchemaViolation(f"{kind} payload is {size} bytes, over the {MAX_PAYLOAD} byte limit")
