"""Registry-derived constrained decoding: JSON schema -> GBNF, a cache keyed by the registry's content hash, and the
backend interface that puts either form into a llama-server request.

* The JSON schema comes from `schema.build_plan_schema` (unchanged: one `set_state` variant per capability/value-schema
  with an enum of the endpoints that have it, so unknown endpoints, capabilities a device lacks and out-of-range values
  cannot be expressed).
* `schema_to_gbnf` compiles that schema (the subset it uses) to a GBNF grammar, for servers/builds where `response_format`
  is not available and for the golden-grammar tests. Floats are not range-bounded in GBNF (the executor re-validates).
  UNVERIFIED against a real llama-server in this session; `tests/test_llama_real.py` (EDGE_TEST_GGUF) is the gate.
* `ConstraintCache` rebuilds only when its key changes (the hub's registry version, else the registry CONTENT relevant
  to decoding: ids, capabilities, ranges, enums, rooms, scene names), so without a version a label-only edit keeps the compiled
  artefacts. When `list_devices` carries the hub's `registry_version` (device-hub 1.4.0) that number is the key instead;
  the content hash is the fallback for a hub that does not send it."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Protocol

from ..aliases import AliasMap, build_aliases
from ..devices import DeviceRegistry
from ..schema import SCENE_RE, build_plan_schema
from ..util import jcs


# --------------------------------------------------------------------------------------------- cache key
def registry_decoding_key(reg: DeviceRegistry, scene_names: tuple[str, ...] | None = None, scope_key: str = "") -> str:
    """Cache key of the decoding schema. When `list_devices` carried the hub's `registry_version` the key is that number
    (plus the scope and the scene listing, which a version bump need not cover): O(1) in the device count and exactly as
    fresh as the hub's own change counter. Without one it falls back to a hash of everything the schema depends on (ids,
    capabilities, ranges, enums, rooms, scenes): names, tags and `online` are NOT part of it, so a rename does not rebuild."""
    scenes = (list(scene_names) if scene_names is not None
              else [[s.scene_id, s.name] for s in sorted(reg.scenes.values(), key=lambda s: s.scene_id) if not s.broken])
    if reg.list_version is not None:
        shape: dict = {"v": reg.list_version, "scope": scope_key, "scenes": scenes}
    else:
        shape = {
            "devices": sorted(
                [d.endpoint_id, d.type,
                 sorted([c.id, c.kind, c.type, c.min, c.max, list(c.values)] for c in d.caps)]
                for d in reg.devices.values()),
            "rooms": reg.rooms(), "scenes": scenes, "scope": scope_key}
    try:
        text = jcs(shape)
    except (TypeError, ValueError):
        text = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return ("v" if reg.list_version is not None else "c") + hashlib.sha256(text.encode()).hexdigest()[:15]


# --------------------------------------------------------------------------------------------- JSON schema -> GBNF
class GbnfError(ValueError):
    pass


def lit(s: str) -> str:
    """A GBNF string literal that matches `s` exactly."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def json_lit(v: Any) -> str:
    """GBNF literal matching the compact JSON encoding of `v` (a string value includes its quotes)."""
    return lit(json.dumps(v, ensure_ascii=False, separators=(",", ":")))


class _Compiler:
    def __init__(self) -> None:
        self.rules: dict[str, str] = {}
        self._n = 0

    def add(self, hint: str, body: str) -> str:
        for name, b in self.rules.items():
            if b == body and name.rstrip("0123456789") == hint:
                return name
        self._n += 1
        name = f"{hint}{self._n}"
        self.rules[name] = body
        return name

    def value(self, sch: dict, hint: str = "v") -> str:
        """A rule name (or inline expression) matching the JSON values valid for `sch`."""
        if "const" in sch:
            return json_lit(sch["const"])
        if "enum" in sch:
            if not sch["enum"]:
                raise GbnfError("empty enum")
            return "(" + " | ".join(json_lit(v) for v in sch["enum"]) + ")"
        if "anyOf" in sch:
            return self.add("alt", " | ".join(self.value(s, hint) for s in sch["anyOf"]))
        t = sch.get("type")
        if t == "null":
            return '"null"'
        if t == "boolean":
            return '("true" | "false")'
        if t == "integer":
            return self._integer(sch)
        if t == "number":
            return "number"
        if t == "string":
            return self._string(sch)
        if t == "array":
            return self._array(sch, hint)
        if t == "object":
            return self._object(sch, hint)
        raise GbnfError(f"unsupported schema node: {sorted(sch)}")

    def _integer(self, sch: dict) -> str:
        lo, hi = sch.get("minimum"), sch.get("maximum")
        if lo is not None and hi is not None and 0 <= hi - lo <= 255:
            return self.add("int", " | ".join(lit(str(i)) for i in range(int(lo), int(hi) + 1)))
        return "integer"                    # unbounded in the grammar; the executor's range check is the backstop

    def _string(self, sch: dict) -> str:
        if sch.get("pattern") == SCENE_RE.pattern:
            return self.add("scenestr", '"\\"" [A-Za-z0-9] [A-Za-z0-9 _.\'&-]{0,47} "\\""')
        n = int(sch.get("maxLength", 240))
        return self.add("str", '"\\"" ([^"\\\\\\x00-\\x1f] | "\\\\" ["\\\\/bfnrt])' + "{0,%d}" % n + ' "\\""')

    def _array(self, sch: dict, hint: str) -> str:
        items = sch["items"]
        if isinstance(items, list):          # fixed-arity tuple (S8i call tuples): one rule per position, no optionals
            parts = [self.value(s, f"{hint}{i}") for i, s in enumerate(items)]
            return self.add("tup", '"[" ws ' + ' "," ws '.join(parts) + ' ws "]"')
        item = self.value(items, hint + "i")
        mx = int(sch.get("maxItems", 4))
        if mx < 1 or mx > 16:
            raise GbnfError("array bounds unsupported")
        chain = item
        for _ in range(mx - 1):             # item ("," ws item ("," ws item)?)?  nested optionals, no repetition syntax
            chain = f'{item} ("," ws {chain})?'
        return self.add("arr", f'"[" ws ({chain})? ws "]"')

    def _object(self, sch: dict, hint: str) -> str:
        props: dict = sch.get("properties", {})
        req = list(sch.get("required", []))
        parts: list[tuple[str, str, bool]] = []
        for k in props:
            v = self.value(props[k], f"{hint}_{k[:6]}")
            parts.append((k, f'{lit(json.dumps(k))} ws ":" ws {v}', k in req))
        body = self._members(parts)
        return self.add("obj", f'"{{" ws {body} ws "}}"' if body else '"{" ws "}"')

    @staticmethod
    def _members(parts: list[tuple[str, str, bool]]) -> str:
        """Members in schema order. Required ones first (always), optional ones after as `("," ws member)?`. When every
        member is optional the first one emitted carries no comma, hence the alternatives."""
        if not parts:
            return ""
        flags = [p[2] for p in parts]
        if any(flags):
            if not flags[0] or flags != sorted(flags, reverse=True):
                raise GbnfError("required properties must come first")
            out = parts[0][1]
            for _, m, r in parts[1:]:
                out += f' ("," ws {m})' + ("" if r else "?")
            return out
        alts = []
        for i, (_, m, _r) in enumerate(parts):
            tail = " ".join(f'("," ws {q[1]})?' for q in parts[i + 1:])
            alts.append(m + (" " + tail if tail else ""))
        return "(" + " | ".join(alts) + ")?"

    def compile(self, sch: dict) -> str:
        root = self.value(sch)
        rules = {"root": root, "ws": "[ \\n]?", "integer": '"-"? ("0" | [1-9] [0-9]*)',
                 "number": '"-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)?'}
        rules.update(self.rules)
        return "\n".join(f"{k} ::= {v}" for k, v in rules.items()) + "\n"


def schema_to_gbnf(schema: dict) -> str:
    return _Compiler().compile(schema)


# --------------------------------------------------------------------------------------------- cache
class ConstraintSchema(dict):
    """The plan schema (a plain dict to every consumer: json, jsonschema, the mock LLM) that also carries its GBNF form,
    the registry key it was built for, and the alias map (S8i) that decodes the compact wire format back to endpoint
    ids -- built together so planner decode and grammar encode always agree."""
    key: str = ""
    gbnf: str = ""
    hub_version: int = -1
    aliases: AliasMap = AliasMap()


class ConstraintCache:
    """Builds the schema + grammar when the cache key changes (see `registry_decoding_key`), otherwise returns the cached
    object (the same object, so identity doubles as a cheap "unchanged" check for callers). One entry per scope key
    (a restricted caller decodes against a schema that names only its own endpoints and scenes); a small LRU."""
    MAX_SCOPES = 16

    def __init__(self) -> None:
        self._by_scope: OrderedDict[str, ConstraintSchema] = OrderedDict()
        self.builds = 0
        self.hits = 0

    def get(self, reg: DeviceRegistry, scene_names: tuple[str, ...] | None = None, scope_key: str = "") -> ConstraintSchema:
        key = registry_decoding_key(reg, scene_names, scope_key)
        cur = self._by_scope.get(scope_key)
        if cur is not None and cur.key == key:
            self.hits += 1
            cur.hub_version = reg.version
            self._by_scope.move_to_end(scope_key)
            return cur
        aliases = build_aliases(reg)
        sch = ConstraintSchema(build_plan_schema(reg, aliases, scene_names))
        sch.key, sch.hub_version, sch.aliases = key, reg.version, aliases
        sch.gbnf = schema_to_gbnf(sch)
        self._by_scope[scope_key] = sch
        self._by_scope.move_to_end(scope_key)
        while len(self._by_scope) > self.MAX_SCOPES:
            self._by_scope.popitem(last=False)
        self.builds += 1
        return sch

    @property
    def current(self) -> ConstraintSchema | None:
        """The unrestricted schema, if built (stats)."""
        return self._by_scope.get("")


# --------------------------------------------------------------------------------------------- backends
class ConstraintBackend(Protocol):
    """How a constraint reaches llama-server. `apply` mutates the request body in place."""
    name: str

    def apply(self, body: dict, schema: dict) -> None: ...


class JsonSchemaConstraint:
    """llama-server compiles `response_format.json_schema` to a grammar itself (the v1 behaviour)."""
    name = "json_schema"

    def apply(self, body: dict, schema: dict) -> None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "plan", "strict": True, "schema": dict(schema)}}


class GbnfConstraint:
    """Sends our own GBNF in the `grammar` field (llama-server's native parameter, also accepted on the chat route)."""
    name = "gbnf"

    def apply(self, body: dict, schema: dict) -> None:
        body["grammar"] = getattr(schema, "gbnf", "") or schema_to_gbnf(schema)


def make_backend(name: str) -> ConstraintBackend:
    return GbnfConstraint() if name == "gbnf" else JsonSchemaConstraint()
