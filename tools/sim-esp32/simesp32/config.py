"""YAML -> NodeConfig. Builds the contract announce and applies the hub-enforced cross-field rules
(capabilities.md) so a bad YAML fails at load time, not on the broker."""
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .vocab import DEFAULT_UNIT, ENDPOINT_TYPES, VOCAB

NODE_ID_RE = re.compile(r"^esp32-[0-9a-f]{12}$")
LOCAL_ID_RE = re.compile(r"^[a-z0-9_]{1,16}$")
MALFORMED_MODES = ("invalid_json", "missing_field", "wrong_schema", "node_id_mismatch", "oversize",
                   "unknown_capability")


class ConfigError(Exception):
    pass


@dataclass
class Faults:
    drop_ack: float = 0.0            # probability each ack is silently dropped
    drop_first_acks: int = 0         # deterministic: drop the first N acks
    delay_ack_s: float = 0.0
    duplicate_cmd: int = 0           # extra deliveries of every incoming cmd
    duplicate_ack: bool = False      # publish every ack twice
    flapping: tuple | None = None    # (up_s, down_s)
    malformed_announce: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Faults":
        f = cls()
        for k, v in (d or {}).items():
            set_fault(f, k, v)
        return f


def set_fault(f: Faults, key: str, value) -> None:
    try:
        if key == "drop_ack":
            f.drop_ack = float(value)
            if not 0 <= f.drop_ack <= 1:
                raise ValueError("must be 0..1")
        elif key == "drop_first_acks":
            f.drop_first_acks = int(value)
        elif key == "delay_ack_s":
            f.delay_ack_s = float(value)
        elif key == "duplicate_cmd":
            f.duplicate_cmd = int(value)
        elif key == "duplicate_ack":
            f.duplicate_ack = str(value).lower() in ("1", "true", "yes")
        elif key == "flapping":
            if isinstance(value, str):
                up, down = value.split(":")
            elif isinstance(value, dict):
                up, down = value["up_s"], value["down_s"]
            else:
                up, down = value
            f.flapping = (float(up), float(down))
            if f.flapping[0] <= 0 or f.flapping[1] <= 0:
                raise ValueError("up/down must be > 0")
        elif key == "malformed_announce":
            if value not in MALFORMED_MODES:
                raise ValueError(f"mode must be one of {MALFORMED_MODES}")
            f.malformed_announce = value
        else:
            raise ConfigError(f"unknown fault '{key}'")
    except (ValueError, TypeError, KeyError) as e:
        raise ConfigError(f"bad value for fault '{key}': {value!r} ({e})") from e


@dataclass
class CapConfig:
    id: str
    kind: str
    type: str
    min: float | None = None
    max: float | None = None
    values: list | None = None
    unit: str | None = None
    interval_s: int | None = None
    boot_value: object = None
    failsafe_value: object = None
    max_on_s: int | None = None
    settle_s: float | None = None
    generator: dict | None = None
    has_failsafe: bool = False

    def effective_range(self):
        _, _, dmin, dmax, _ = VOCAB[self.id]
        return (self.min if self.min is not None else dmin, self.max if self.max is not None else dmax)

    def announce(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "type": self.type}
        for k in ("min", "max", "values", "unit", "interval_s", "max_on_s", "settle_s"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.kind == "actuator":
            d["boot_value"] = self.boot_value
            if self.has_failsafe:
                d["failsafe_value"] = self.failsafe_value
        return d


@dataclass
class EndpointConfig:
    local_id: str
    type: str
    caps: list = field(default_factory=list)
    travel_time_s: float = 10.0      # cover: full 0..100 travel
    interlock_s: float = 0.5         # cover: relay dead time between directions
    progress_s: float = 1.0          # cover: state publish interval while moving

    def announce(self) -> dict:
        return {"local_id": self.local_id, "type": self.type, "capabilities": [c.announce() for c in self.caps]}


@dataclass
class NodeConfig:
    node_id: str
    hw: str = "esp32-devkit"
    fw: str = "0.1.0-sim"
    failsafe_after_s: int = 60
    heartbeat_s: float = 60.0
    dedup_size: int = 16             # contracts/mqtt.md: last 16 cmd_ids
    seed: int = 1
    endpoints: list = field(default_factory=list)
    faults: Faults = field(default_factory=Faults)

    def announce(self) -> dict:
        return {"schema": 2, "node_id": self.node_id, "hw": self.hw, "fw": self.fw,
                "failsafe_after_s": self.failsafe_after_s,
                "endpoints": [e.announce() for e in self.endpoints]}


def _cap(raw: dict, where: str) -> CapConfig:
    if not isinstance(raw, dict) or "id" not in raw:
        raise ConfigError(f"{where}: capability needs an id")
    cid = raw["id"]
    if cid not in VOCAB:
        raise ConfigError(f"{where}: capability '{cid}' is not in the vocabulary (capabilities.md)")
    kind, typ, _, _, fixed = VOCAB[cid]
    allowed = {"id", "min", "max", "values", "unit", "interval_s", "boot_value", "failsafe_value",
               "max_on_s", "settle_s", "generator"}
    extra = set(raw) - allowed
    if extra:
        raise ConfigError(f"{where}/{cid}: unknown keys {sorted(extra)}")
    c = CapConfig(id=cid, kind=kind, type=typ, min=raw.get("min"), max=raw.get("max"),
                  values=raw.get("values", fixed), unit=raw.get("unit", DEFAULT_UNIT.get(cid)),
                  generator=raw.get("generator"))
    if typ in ("bool", "enum"):
        c.min = c.max = None
    if typ != "enum":
        c.values = None
    if typ == "enum" and not c.values:
        raise ConfigError(f"{where}/{cid}: enum needs values")
    if kind == "actuator":
        if "interval_s" in raw:
            raise ConfigError(f"{where}/{cid}: interval_s is for sensors")
        if "boot_value" not in raw:
            raise ConfigError(f"{where}/{cid}: actuator needs boot_value")
        c.boot_value = raw["boot_value"]
        c.has_failsafe = "failsafe_value" in raw
        c.failsafe_value = raw.get("failsafe_value")
        c.max_on_s = raw.get("max_on_s")
        c.settle_s = raw.get("settle_s")
        if c.settle_s is not None and not (isinstance(c.settle_s, (int, float)) and 1 <= c.settle_s <= 120):
            raise ConfigError(f"{where}/{cid}: settle_s must be 1..120")
        if c.max_on_s and typ != "bool":
            raise ConfigError(f"{where}/{cid}: max_on_s is only supported on bool capabilities in the sim")
        if cid == "speed" and (c.min is None or c.max is None):
            raise ConfigError(f"{where}/{cid}: speed needs min and max")
        lo, hi = c.effective_range()
        if lo is not None and hi is not None and lo > hi:
            raise ConfigError(f"{where}/{cid}: min > max")
        _check_value(c, c.boot_value, lo, hi, f"{where}/{cid}.boot_value")
        if c.has_failsafe:
            _check_value(c, c.failsafe_value, lo, hi, f"{where}/{cid}.failsafe_value")
    else:
        for k in ("boot_value", "failsafe_value", "max_on_s", "settle_s"):
            if k in raw:
                raise ConfigError(f"{where}/{cid}: {k} not allowed on a sensor")
        if "interval_s" not in raw:
            raise ConfigError(f"{where}/{cid}: sensor needs interval_s")
        c.interval_s = int(raw["interval_s"])
        if not 1 <= c.interval_s <= 86400:
            raise ConfigError(f"{where}/{cid}: interval_s must be 1..86400")
    return c


def _check_value(c: CapConfig, v, lo, hi, where):
    if c.type == "bool":
        ok = isinstance(v, bool)
    elif c.type == "int":
        ok = isinstance(v, int) and not isinstance(v, bool) and (lo is None or v >= lo) and (hi is None or v <= hi)
    elif c.type == "float":
        ok = (isinstance(v, (int, float)) and not isinstance(v, bool)
              and (lo is None or v >= lo) and (hi is None or v <= hi))
    else:
        ok = isinstance(v, str) and v in (c.values or [])
    if not ok:
        raise ConfigError(f"{where}: {v!r} is not a valid {c.type} value within the declared range/values")


def _endpoint(raw: dict, where: str) -> EndpointConfig:
    lid = raw.get("local_id")
    if not isinstance(lid, str) or not LOCAL_ID_RE.match(lid):
        raise ConfigError(f"{where}: bad local_id {lid!r}")
    if raw.get("type") not in ENDPOINT_TYPES:
        raise ConfigError(f"{where}/{lid}: bad endpoint type {raw.get('type')!r}")
    extra = set(raw) - {"local_id", "type", "capabilities", "sim"}
    if extra:
        raise ConfigError(f"{where}/{lid}: unknown keys {sorted(extra)} (names/rooms are admin labels, not firmware)")
    ep = EndpointConfig(local_id=lid, type=raw["type"])
    sim = raw.get("sim") or {}
    ep.travel_time_s = float(sim.get("travel_time_s", ep.travel_time_s))
    ep.interlock_s = float(sim.get("interlock_s", ep.interlock_s))
    ep.progress_s = float(sim.get("progress_s", ep.progress_s))
    ep.caps = [_cap(c, f"{where}/{lid}") for c in raw.get("capabilities") or []]
    if not ep.caps:
        raise ConfigError(f"{where}/{lid}: no capabilities")
    ids = [c.id for c in ep.caps]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"{where}/{lid}: duplicate capability id")
    for c in ep.caps:
        if c.id == "position" and c.settle_s is None:
            # worst case for a command: a full travel plus the relay dead time of a reversal, plus 1 s margin
            c.settle_s = math.ceil(ep.travel_time_s + ep.interlock_s) + 1
            if c.settle_s > 120:
                raise ConfigError(f"{where}/{lid}: travel_time_s {ep.travel_time_s} gives settle_s {c.settle_s} > 120 (contract max)")
    return ep


def _node(raw: dict, i: int) -> NodeConfig:
    where = f"nodes[{i}]"
    nid = raw.get("node_id")
    if not isinstance(nid, str) or not NODE_ID_RE.match(nid):
        raise ConfigError(f"{where}: node_id must match esp32-<12 hex>, got {nid!r}")
    extra = set(raw) - {"node_id", "hw", "fw", "failsafe_after_s", "heartbeat_s", "dedup_size", "seed", "endpoints", "faults"}
    if extra:
        raise ConfigError(f"{where}: unknown keys {sorted(extra)}")
    n = NodeConfig(node_id=nid, hw=raw.get("hw", "esp32-devkit"), fw=raw.get("fw", "0.1.0-sim"),
                   failsafe_after_s=int(raw.get("failsafe_after_s", 60)),
                   heartbeat_s=float(raw.get("heartbeat_s", 60)),
                   dedup_size=int(raw.get("dedup_size", 16)), seed=int(raw.get("seed", 1)),
                   faults=Faults.from_dict(raw.get("faults")))
    n.endpoints = [_endpoint(e, nid) for e in raw.get("endpoints") or []]
    if not n.endpoints:
        raise ConfigError(f"{where}: no endpoints")
    ids = [e.local_id for e in n.endpoints]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"{where}: duplicate local_id")
    if n.dedup_size < 1:
        raise ConfigError(f"{where}: dedup_size must be >= 1")
    return n


def load_config(path) -> list:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not data["nodes"]:
        raise ConfigError("YAML must have a non-empty 'nodes:' list")
    nodes = [_node(n, i) for i, n in enumerate(data["nodes"])]
    ids = [n.node_id for n in nodes]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate node_id")
    return nodes
