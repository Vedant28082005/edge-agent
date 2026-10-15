r"""The contract file is the single source of truth: request params are validated against its schemas and every
response is re-validated against them before it leaves (a conformance backstop; a failure is a 500 `model_error`).

Python's `re` lets `$` match before a trailing newline and jsonschema uses re.search, so a value like "abc\n" would pass
`^[a-z]+$`. Patterns are therefore rewritten to `\A...\Z` when the contract is loaded."""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import jsonschema
import yaml

CONTRACT_FILE = "openapi/vision-node-internal.yaml"
_SEARCH = (Path(__file__).resolve().parents[3] / "contracts", Path("/app/contracts"))


def find_contracts_dir(explicit: str | None = None) -> Path:
    for p in ((Path(explicit),) if explicit else ()) + _SEARCH:
        if (p / CONTRACT_FILE).is_file():
            return p
    raise SystemExit("contracts/ directory not found (set contracts_dir in the config)")


def _strict(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "pattern" and isinstance(v, str):
                v = re.sub(r"^\^", lambda _m: "\\A", v)
                v = re.sub(r"\$$", lambda _m: "\\Z", v)
                out[k] = v
            else:
                out[k] = _strict(v)
        return out
    if isinstance(node, list):
        return [_strict(v) for v in node]
    return node


class Contract:
    def __init__(self, contracts_dir: str | None = None):
        path = find_contracts_dir(contracts_dir) / CONTRACT_FILE
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.version: str = spec["info"]["version"]
        self.spec = spec
        self._components = _strict(copy.deepcopy(spec["components"]))
        self._validators: dict[str, jsonschema.Draft202012Validator] = {}

    def validator(self, name: str) -> jsonschema.Draft202012Validator:
        v = self._validators.get(name)
        if v is None:
            v = jsonschema.Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": self._components})
            self._validators[name] = v
        return v

    def valid(self, name: str, instance: Any) -> bool:
        return self.validator(name).is_valid(instance)
