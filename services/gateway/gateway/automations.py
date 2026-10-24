"""Automations (contracts/automations.md v1): rule store, validation, run history, notifications, kill switch and the engine.

No LLM runs here. The engine reads the hub event stream the gateway already consumes (state, node_status, registry_changed) plus a
timer tick, and acts only through the hub's `POST /devices/{id}/set` WITHOUT a confirmation token: a `risk: confirm` target is
refused here (`blocked_confirm`) and, should that check ever be bypassed, by the hub itself. Every set_state is written to the
action log as `automation:<rule_id>` (user `household`)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import json
import logging
import re
import secrets
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError, model_validator

from .actionlog import CODE_RE
from .auth import User
from .events import Event
from .upstream import UpstreamError

log = logging.getLogger("gateway.automations")

EP_RE = re.compile(r"^ep_[0-9a-f]{8}$")
RULE_RE = re.compile(r"^ru_[0-9a-f]{8}$")
TIME_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_RULES = 100
SET_STATE_PER_HOUR = 60                     # engine-wide cap against feedback loops (automations.md, invariant 7)
RETENTION_S = 30 * 86400
RUNS_PER_RULE = 1000
OFFLINE_DEFAULT_S = 60
FIXED_ENUMS = {"lock": ["locked", "unlocked"], "contact": ["open", "closed"]}

Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr]
EpId = Annotated[str, Field(pattern=EP_RE.pattern)]
Cap = Annotated[str, Field(min_length=1, max_length=64)]
TimeOfDay = Annotated[str, Field(pattern=TIME_RE)]
Days = Annotated[list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]], Field(min_length=1, max_length=7),
                 AfterValidator(lambda v: v if len(set(v)) == len(v) else (_ for _ in ()).throw(ValueError("duplicate days")))]


def _plain(s: str, n: int) -> str:
    """Author-written labels are plain text: control characters out, whitespace collapsed, bounded."""
    return " ".join("".join(c if c.isprintable() else " " for c in str(s or "")).split())[:n]


class AutoError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------------ input models (strict, closed)
class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TChange(_M):
    type: Literal["device_state_change"]
    endpoint_id: EpId
    capability: Cap
    to: Scalar | None = None
    from_: Scalar | None = Field(default=None, alias="from")


class TOffline(_M):
    type: Literal["device_offline"]
    endpoint_id: EpId
    for_s: Annotated[int, Field(strict=True, ge=0, le=86400)] | None = None


class TDuration(_M):
    type: Literal["duration_in_state"]
    endpoint_id: EpId
    capability: Cap
    value: Scalar
    for_s: Annotated[int, Field(strict=True, ge=10, le=86400)]


class TTime(_M):
    type: Literal["time_of_day"]
    at: TimeOfDay
    days: Days | None = None


class Between(_M):
    from_: TimeOfDay = Field(alias="from")
    to: TimeOfDay


class TSchedule(_M):
    type: Literal["schedule"]
    every_s: Annotated[int, Field(strict=True, ge=60, le=86400)]
    between: Between | None = None
    days: Days | None = None


class CState(_M):
    type: Literal["device_state"]
    endpoint_id: EpId
    capability: Cap
    op: Literal["eq", "ne", "gt", "ge", "lt", "le"]
    value: Scalar


class CWindow(_M):
    type: Literal["time_window"]
    after: TimeOfDay
    before: TimeOfDay
    days: Days | None = None


class Condition(_M):
    all: list[Annotated[Union[CState, CWindow], Field(discriminator="type")]] = Field(min_length=1, max_length=4)


class ANotify(_M):
    type: Literal["notify"]
    level: Literal["info", "warning"] = "info"
    text: str = Field(min_length=1, max_length=120)


class ASet(_M):
    type: Literal["set_state"]
    endpoint_id: EpId
    capability: Cap
    value: Scalar


class RuleIn(_M):
    name: str = Field(min_length=1, max_length=48)
    source: Literal["user", "llm_proposal"]
    trigger: Annotated[Union[TChange, TOffline, TDuration, TTime, TSchedule], Field(discriminator="type")]
    condition: Condition | None = None
    action: Annotated[Union[ANotify, ASet], Field(discriminator="type")]
    cooldown_s: Annotated[int, Field(strict=True, ge=10, le=86400)]
    max_runs_per_day: Annotated[int, Field(strict=True, ge=1, le=1000)]

    @model_validator(mode="after")
    def _text(self):
        if not _plain(self.name, 48):
            raise ValueError("empty name")
        if isinstance(self.action, ANotify) and not _plain(self.action.text, 120):
            raise ValueError("empty text")
        return self

    def body(self) -> dict:
        d = {"trigger": self.trigger.model_dump(by_alias=True, exclude_none=True),
             "action": self.action.model_dump(by_alias=True, exclude_none=True)}
        if self.condition is not None:
            d["condition"] = self.condition.model_dump(by_alias=True, exclude_none=True)
        if isinstance(self.action, ANotify):
            d["action"]["text"] = _plain(self.action.text, 120)
        return d


# ------------------------------------------------------------------ registry helpers
def _cap(ep: dict, cap: str) -> dict | None:
    return next((c for c in ep.get("capabilities") or [] if isinstance(c, dict) and c.get("id") == cap), None)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def same(a, b) -> bool:
    """Equality that never confuses True with 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def value_problem(cap: dict, v) -> str | None:
    t = cap.get("type")
    if t == "bool":
        return None if isinstance(v, bool) else "bad_type"
    if t in ("int", "float"):
        if not _num(v) or (t == "int" and (not isinstance(v, int) or isinstance(v, bool))):
            return "bad_type"
        if (_num(cap.get("min")) and v < cap["min"]) or (_num(cap.get("max")) and v > cap["max"]):
            return "out_of_range"
        return None
    if t == "enum":
        vals = cap.get("values") or FIXED_ENUMS.get(cap.get("id"), [])
        return None if isinstance(v, str) and v in vals else "bad_type"
    return None if isinstance(v, (bool, int, float, str)) else "bad_type"


def refs(body: dict) -> list[tuple[str, str | None, str, object]]:
    """(endpoint_id, capability|None, role read|act, value|None) for everything a rule touches."""
    out = []
    t = body["trigger"]
    if "endpoint_id" in t:
        out.append((t["endpoint_id"], t.get("capability"), "read", None))
    for c in (body.get("condition") or {}).get("all", []):
        if c["type"] == "device_state":
            out.append((c["endpoint_id"], c["capability"], "read", c["value"]))
    a = body["action"]
    if a["type"] == "set_state":
        out.append((a["endpoint_id"], a["capability"], "act", a["value"]))
    return out


def problems(body: dict, reg: dict[str, dict]) -> tuple[str | None, set[str]]:
    """First registry problem (None = fine) and the rooms the rule touches. Code: bad_rule | confirm_endpoint_not_allowed."""
    rooms: set[str] = set()
    first = None

    def note(code: str) -> None:
        nonlocal first
        first = first or code
    t, cond = body["trigger"], (body.get("condition") or {}).get("all", [])
    values = []                                                   # (endpoint, capability, value) literals to type-check
    if t["type"] == "device_state_change":
        values += [(t["endpoint_id"], t["capability"], t[k]) for k in ("to", "from") if k in t]
    if t["type"] == "duration_in_state":
        values.append((t["endpoint_id"], t["capability"], t["value"]))
    values += [(c["endpoint_id"], c["capability"], c["value"]) for c in cond if c["type"] == "device_state"]
    for eid, cap, role, _v in refs(body):
        ep = reg.get(eid)
        if ep is None:
            note("bad_rule")
            continue
        if ep.get("room"):
            rooms.add(ep["room"])
        if cap is not None and _cap(ep, cap) is None:
            note("bad_rule")
        elif role == "act":
            c = _cap(ep, cap)
            if c.get("kind") != "actuator" or not ep.get("enabled", True):
                note("bad_rule")
            elif ep.get("risk") == "confirm":
                note("confirm_endpoint_not_allowed")
    for eid, cap, v in values + ([(body["action"]["endpoint_id"], body["action"]["capability"], body["action"]["value"])]
                                 if body["action"]["type"] == "set_state" else []):
        c = _cap(reg.get(eid) or {}, cap)
        if c is not None and value_problem(c, v):
            note("bad_rule")
    for c in cond:
        if c["type"] == "device_state" and c["op"] not in ("eq", "ne"):
            cp = _cap(reg.get(c["endpoint_id"]) or {}, c["capability"])
            if cp is not None and cp.get("type") not in ("int", "float"):
                note("bad_rule")
    return first, rooms


def in_scope(user: User | None, rooms) -> bool:
    """Every room the rule touches is the user's. Room-less rules are admin-only (all targets are endpoint ids)."""
    if user is None or user.disabled:
        return False
    if user.is_admin:
        return True
    return user.role == "member" and bool(rooms) and set(rooms) <= set(user.rooms)


def can_see(user: User, owner: str, rooms) -> bool:
    if user.is_admin:
        return True
    return user.role == "member" and (owner == user.user_id or in_scope(user, rooms))


def nt_visible(user: User, owner: str | None, rooms, system_wide: bool) -> bool:
    if user.is_admin:
        return True
    if user.role != "member":
        return False
    return system_wide or can_see(user, owner or "", rooms)


# ------------------------------------------------------------------ time helpers
def in_window(after: str, before: str, lt: dt.datetime) -> bool:
    m = lt.hour * 60 + lt.minute
    a, b = int(after[:2]) * 60 + int(after[3:]), int(before[:2]) * 60 + int(before[3:])
    return a <= m < b if a <= b else (m >= a or m < b)


def day_ok(days, lt: dt.datetime) -> bool:
    return not days or DAYS[lt.weekday()] in days


def window_ok(c: dict, lt: dt.datetime) -> bool:
    """`time_window`: days refer to the day the window opened (so 22:00-06:00 on `fri` includes Saturday 02:00)."""
    if not in_window(c["after"], c["before"], lt):
        return False
    if not c.get("days"):
        return True
    wrapped = c["after"] > c["before"] and (lt.hour * 60 + lt.minute) < int(c["before"][:2]) * 60 + int(c["before"][3:])
    return day_ok(c["days"], lt - dt.timedelta(days=1) if wrapped else lt)


def compare(op: str, have, want) -> bool:
    if op in ("eq", "ne"):
        return same(have, want) == (op == "eq")
    if not (_num(have) and _num(want)):
        return False
    return {"gt": have > want, "ge": have >= want, "lt": have < want, "le": have <= want}[op]


# ------------------------------------------------------------------ the service
class Automations:
    def __init__(self, db, hub, auth, bc, actions, cfg, clock) -> None:
        self.db, self.hub, self.auth, self.bc, self.actions, self.cfg, self.clock = db, hub, auth, bc, actions, cfg, clock
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(cfg.household_tz)
        except Exception:                                          # noqa: BLE001 - no tzdata: fall back to UTC
            if cfg.household_tz not in ("UTC", "utc", ""):
                log.warning("household_tz %r unavailable, using UTC", cfg.household_tz)
            self.tz = dt.timezone.utc
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.state: dict[str, dict] = {}                           # endpoint -> {capability: value}
        self.since: dict[tuple[str, str], tuple[object, float]] = {}
        self.node_of: dict[str, str] = {}
        self.offline: dict[str, float] = {}                        # endpoint -> offline since
        self._fired_ep: dict[str, object] = {}                     # rule -> episode key already fired (duration/offline/time)
        self._sched: dict[str, float] = {}
        self._cache: list[dict] | None = None
        self._last_housekeeping = 0.0
        row = self.db.one("SELECT value FROM automation_state WHERE key='enabled'")
        self.enabled = row is None or row["value"] == "1"

    # ---- small helpers
    def now(self) -> int:
        return int(self.clock())

    def local(self, ts: float | None = None) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.clock() if ts is None else ts, self.tz)

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def idle(self) -> None:
        """Wait for runs started by events/ticks (tests, shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def registry(self) -> dict[str, dict] | None:
        try:
            status, body = await self.hub.hub("GET", "/devices", actor="household")
        except UpstreamError:
            return None
        if status != 200 or not isinstance(body, list):
            return None
        return {e["endpoint_id"]: e for e in body if isinstance(e, dict) and isinstance(e.get("endpoint_id"), str)}

    # ---- rule rows
    def _out(self, r, uid: str | None = None) -> dict:
        body = json.loads(r["body"])
        d = {"rule_id": r["rule_id"], "name": r["name"], "owner": r["owner"], "enabled": r["status"] == "approved",
             "trigger": body["trigger"], "action": body["action"], "cooldown_s": r["cooldown_s"],
             "max_runs_per_day": r["max_runs_per_day"], "status": r["status"], "created_by": r["created_by"],
             "created_at": r["created_at"], "updated_at": r["updated_at"], "rooms": json.loads(r["rooms"]), "broken": bool(r["broken"])}
        if "condition" in body:
            d["condition"] = body["condition"]
        if r["status"] == "approved":
            d["approved_by"], d["approved_at"] = r["approved_by"], r["approved_at"]
        return d

    def _row(self, rule_id: str):
        return self.db.one("SELECT * FROM automation_rules WHERE rule_id=?", (rule_id,)) if RULE_RE.match(rule_id) else None

    def _visible_row(self, user: User, rule_id: str):
        r = self._row(rule_id)
        if r is None or not can_see(user, r["owner"], json.loads(r["rooms"])):
            raise AutoError(404, "not_found")
        return r

    def _changed(self, r, change: str) -> None:
        owner, rooms = r["owner"], json.loads(r["rooms"])
        self._cache = None
        self.bc.publish(Event("automations_changed", {"rule_id": r["rule_id"], "change": change},
                              visible=lambda u, o=owner, rm=rooms: can_see(u, o, rm)))

    def _reset(self, rule_id: str) -> None:
        for d in (self._fired_ep, self._sched):
            d.pop(rule_id, None)

    # ---- validation shared by create / update / approve
    async def _check(self, user: User, inp: RuleIn | dict, *, body: dict | None = None) -> tuple[dict, list[str]]:
        body = body or inp.body()
        reg = await self.registry()
        if reg is None:
            raise AutoError(502, "upstream_error", "device registry unavailable")
        missing = [e for e, *_ in refs(body) if e not in reg]
        if missing:
            raise AutoError(400, "bad_rule", "unknown endpoint")
        rooms = sorted({reg[e]["room"] for e, *_ in refs(body) if reg[e].get("room")})
        if not in_scope(user, rooms):
            raise AutoError(403, "out_of_scope", "every endpoint of a rule must be in your rooms")
        code, _ = problems(body, reg)
        if code == "confirm_endpoint_not_allowed":
            raise AutoError(409, code, "set_state on a risk:confirm endpoint is not allowed; use notify")
        if code:
            raise AutoError(400, "bad_rule", "capability, type or range does not match the registry")
        return body, rooms

    @staticmethod
    def parse(raw) -> RuleIn:
        try:
            return RuleIn.model_validate(raw)
        except ValidationError as e:
            loc = ".".join(str(x) for x in (e.errors()[0].get("loc", ()) if e.errors() else ()))
            raise AutoError(400, "bad_rule", f"invalid field: {loc}"[:120] if loc else "invalid rule") from None

    # ---- CRUD
    def list_rules(self, user: User, status: str | None = None, created_by: str | None = None) -> list[dict]:
        rows = self.db.all("SELECT * FROM automation_rules ORDER BY created_at DESC, rule_id")
        return [self._out(r) for r in rows if can_see(user, r["owner"], json.loads(r["rooms"]))
                and (status is None or r["status"] == status) and (created_by is None or r["created_by"] == created_by)][:200]

    def get(self, user: User, rule_id: str) -> dict:
        return self._out(self._visible_row(user, rule_id))

    async def create(self, user: User, raw) -> dict:
        inp = self.parse(raw)
        if self.db.one("SELECT COUNT(*) n FROM automation_rules")["n"] >= MAX_RULES:
            self.notify("rule_limit", "system", None, None, "The automation limit (100 rules) is reached; delete a rule first.",
                        owner=None, rooms=[], throttle_s=3600)
            raise AutoError(409, "rule_limit", "at most 100 rules")
        body, rooms = await self._check(user, inp)
        now, rid, llm = self.now(), "ru_" + secrets.token_hex(4), inp.source == "llm_proposal"
        status = "proposed" if llm else "approved"
        self.db.run("INSERT INTO automation_rules(rule_id,name,owner,status,created_by,body,cooldown_s,max_runs_per_day,approved_by,"
                    "approved_at,created_at,updated_at,broken,rooms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                    (rid, _plain(inp.name, 48), user.user_id, status, "llm" if llm else "user", json.dumps(body, sort_keys=True),
                     inp.cooldown_s, inp.max_runs_per_day, None if llm else user.user_id, None if llm else now, now, now,
                     json.dumps(rooms)))
        self.db.audit(user.user_id, "automation.create", rid, {"created_by": "llm" if llm else "user", "status": status})
        r = self._row(rid)
        if llm:
            self.notify("rule_proposed", f"automation:{rid}", rid, None, f"A new rule is waiting for your approval: {_plain(inp.name, 48)}",
                        owner=user.user_id, rooms=rooms)
        self._changed(r, "created")
        return self._out(r)

    async def update(self, user: User, rule_id: str, raw) -> dict:
        r = self._visible_row(user, rule_id)
        old_rooms = json.loads(r["rooms"])
        if not user.is_admin and not (r["owner"] == user.user_id and in_scope(user, old_rooms)):
            raise AutoError(403, "forbidden", "members may edit their own rules inside their rooms only")
        inp = self.parse(raw)
        body, rooms = await self._check(user, inp)
        now = self.now()
        status = "proposed" if r["status"] == "approved" else r["status"]      # an edit never runs on an earlier approval
        self.db.run("UPDATE automation_rules SET name=?,status=?,body=?,cooldown_s=?,max_runs_per_day=?,approved_by=NULL,approved_at=NULL,"
                    "updated_at=?,broken=0,rooms=? WHERE rule_id=?",
                    (_plain(inp.name, 48), status, json.dumps(body, sort_keys=True), inp.cooldown_s, inp.max_runs_per_day, now,
                     json.dumps(rooms), rule_id))
        self._reset(rule_id)
        self.db.audit(user.user_id, "automation.update", rule_id, {"status": status})
        r = self._row(rule_id)
        self._changed(r, "updated")
        return self._out(r)

    def delete(self, user: User, rule_id: str) -> None:
        r = self._visible_row(user, rule_id)
        if not user.is_admin and r["owner"] != user.user_id:
            raise AutoError(403, "forbidden", "members may delete their own rules only")
        with self.db.tx():
            self.db.run("UPDATE automation_runs SET rule_deleted=1 WHERE rule_id=?", (rule_id,))
            self.db.run("DELETE FROM automation_rules WHERE rule_id=?", (rule_id,))
        self._reset(rule_id)
        self.db.audit(user.user_id, "automation.delete", rule_id)
        self._changed(r, "deleted")

    async def approve(self, user: User, rule_id: str) -> dict:
        r = self._visible_row(user, rule_id)
        rooms = json.loads(r["rooms"])
        if not in_scope(user, rooms):
            raise AutoError(403, "forbidden", "approval needs access to every room of the rule")
        if r["status"] == "approved" and not r["broken"]:
            return self._out(r)
        if r["broken"]:
            raise AutoError(409, "rule_broken", "the rule references something that no longer exists; edit it first")
        body, rooms = await self._check(user, None, body=json.loads(r["body"]))     # live registry first; nothing changes on failure
        now = self.now()
        self.db.run("UPDATE automation_rules SET status='approved',approved_by=?,approved_at=?,updated_at=?,rooms=? WHERE rule_id=?",
                    (user.user_id, now, now, json.dumps(rooms), rule_id))
        self._reset(rule_id)
        self.db.audit(user.user_id, "automation.approve", rule_id)
        r = self._row(rule_id)
        self._changed(r, "approved")
        return self._out(r)

    def disable(self, user: User, rule_id: str) -> dict:
        r = self._visible_row(user, rule_id)
        if not (user.is_admin or r["owner"] == user.user_id or in_scope(user, json.loads(r["rooms"]))):
            raise AutoError(403, "forbidden")
        if r["status"] != "disabled":
            self.db.run("UPDATE automation_rules SET status='disabled',approved_by=NULL,approved_at=NULL,updated_at=? WHERE rule_id=?",
                        (self.now(), rule_id))
            self._reset(rule_id)
            self.db.audit(user.user_id, "automation.disable", rule_id)
            r = self._row(rule_id)
            self._changed(r, "disabled")
        return self._out(r)

    # ---- run history
    @staticmethod
    def _cursor(prefix: str, last: int) -> str:
        return base64.urlsafe_b64encode(f"{prefix}1:{last}".encode()).decode().rstrip("=")

    @staticmethod
    def _decode(prefix: str, c: str) -> int | None:
        try:
            raw = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        m = re.fullmatch(prefix + r"1:([0-9]{1,15})", raw)
        return int(m.group(1)) if m else None

    def runs(self, user: User, rule_id: str, limit: int = 50, cursor: str | None = None) -> dict:
        if not RULE_RE.match(rule_id):
            raise AutoError(404, "not_found")
        r = self._row(rule_id)
        if r is None:
            if not (user.is_admin and self.db.one("SELECT 1 FROM automation_runs WHERE rule_id=? LIMIT 1", (rule_id,))):
                raise AutoError(404, "not_found")                    # history of a deleted rule is kept for admins (audit)
        else:
            self._visible_row(user, rule_id)
        after = None
        if cursor is not None:
            after = self._decode("r", cursor)
            if after is None:
                raise AutoError(400, "bad_request", "bad cursor")
        rows = self.db.all(
            "SELECT r.*, a.status AS live_status FROM automation_runs r LEFT JOIN action_log a ON a.id=r.action_log_id "
            "WHERE r.rule_id=? AND (? IS NULL OR r.run_id<?) ORDER BY r.run_id DESC LIMIT ?", (rule_id, after, after, limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for x in rows:
            d = {"run_id": x["run_id"], "rule_id": x["rule_id"], "ts": x["ts"], "outcome": x["outcome"],
                 "action_status": x["live_status"] or x["action_status"], "action_log_id": x["action_log_id"],
                 "call_id": x["call_id"], "error": x["error"]}
            if x["rule_deleted"]:
                d["rule_deleted"] = True
            items.append(d)
        return {"items": items, "next_cursor": self._cursor("r", rows[-1]["run_id"]) if more else None}

    def _run(self, rule_id: str, outcome: str, **kw) -> int:
        cur = self.db.run("INSERT INTO automation_runs(rule_id,ts,outcome,action_status,action_log_id,call_id,error) VALUES(?,?,?,?,?,?,?)",
                          (rule_id, self.now(), outcome, kw.get("action_status"), kw.get("action_log_id"), kw.get("call_id"),
                           kw.get("error")))
        self.db.run("DELETE FROM automation_runs WHERE rule_id=? AND run_id <= (SELECT run_id FROM automation_runs WHERE rule_id=? "
                    "ORDER BY run_id DESC LIMIT 1 OFFSET ?)", (rule_id, rule_id, RUNS_PER_RULE))
        self.db.audit(f"automation:{rule_id}", "automation.run", rule_id, {"outcome": outcome, "error": kw.get("error")})
        return cur.lastrowid

    # ---- notifications
    def notify(self, level: str, source: str, rule_id: str | None, endpoint_id: str | None, text: str, *, owner: str | None,
               rooms: list[str], throttle_s: int = 0) -> dict | None:
        """Persist and push. `throttle_s`: skip when the same level+source(+rule) was sent that recently."""
        now = self.now()
        if throttle_s and self.db.one("SELECT 1 FROM notifications WHERE level=? AND source=? AND COALESCE(rule_id,'')=? AND ts>? LIMIT 1",
                                      (level, source, rule_id or "", now - throttle_s)):
            return None
        nid = "nt_" + secrets.token_hex(6)
        text = _plain(text, 500) or "-"
        system_wide = rule_id is None and endpoint_id is None
        self.db.run("INSERT INTO notifications(notification_id,ts,level,source,rule_id,endpoint_id,text,owner,rooms) VALUES(?,?,?,?,?,?,?,?,?)",
                    (nid, now, level, source, rule_id, endpoint_id, text, owner, json.dumps(sorted(rooms))))
        d = {"notification_id": nid, "ts": now, "level": level, "source": source, "text": text}
        if rule_id:
            d["rule_id"] = rule_id
        if endpoint_id:
            d["endpoint_id"] = endpoint_id
        self.bc.publish(Event("automation_notification", d, visible=lambda u, o=owner, rm=rooms, sw=system_wide: nt_visible(u, o, rm, sw)))
        return d

    def notifications(self, user: User, unread: bool = False, rule_id: str | None = None, limit: int = 50,
                      cursor: str | None = None) -> dict:
        after = None
        if cursor is not None:
            after = self._decode("n", cursor)
            if after is None:
                raise AutoError(400, "bad_request", "bad cursor")
        read = {x["notification_id"] for x in self.db.all("SELECT notification_id FROM notification_reads WHERE user_id=?", (user.user_id,))}
        out, last = [], None
        rows = self.db.all("SELECT * FROM notifications WHERE (? IS NULL OR seq<?) AND (? IS NULL OR rule_id=?) ORDER BY seq DESC LIMIT 5000",
                           (after, after, rule_id, rule_id))
        more = False
        for r in rows:
            if not nt_visible(user, r["owner"], json.loads(r["rooms"]), r["rule_id"] is None and r["endpoint_id"] is None):
                continue
            if unread and r["notification_id"] in read:
                continue
            if len(out) == limit:
                more = True
                break
            d = {"notification_id": r["notification_id"], "ts": r["ts"], "level": r["level"], "source": r["source"], "text": r["text"],
                 "read": r["notification_id"] in read}
            if r["rule_id"]:
                d["rule_id"] = r["rule_id"]
            if r["endpoint_id"]:
                d["endpoint_id"] = r["endpoint_id"]
            out.append(d)
            last = r["seq"]
        return {"items": out, "next_cursor": self._cursor("n", last) if more else None}

    def mark_read(self, user: User, ids: list[str]) -> None:
        for nid in ids:
            r = self.db.one("SELECT * FROM notifications WHERE notification_id=?", (nid,))
            if r and nt_visible(user, r["owner"], json.loads(r["rooms"]), r["rule_id"] is None and r["endpoint_id"] is None):
                self.db.run("INSERT OR IGNORE INTO notification_reads(user_id,notification_id) VALUES(?,?)", (user.user_id, nid))

    # ---- kill switch
    def kill_switch(self) -> dict:
        by, at = (self.db.one("SELECT value FROM automation_state WHERE key=?", (k,)) for k in ("changed_by", "changed_at"))
        return {"automations_enabled": self.enabled, "changed_by": by["value"] if by else None,
                "changed_at": int(at["value"]) if at else None}

    def set_kill_switch(self, user: User, enabled: bool) -> dict:
        now = self.now()
        with self.db.tx():
            for k, v in (("enabled", "1" if enabled else "0"), ("changed_by", user.user_id), ("changed_at", str(now))):
                self.db.run("INSERT INTO automation_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        changed, self.enabled = enabled != self.enabled, enabled      # takes effect before the next action starts
        self.db.audit(user.user_id, "admin.automations_kill_switch", "", {"automations_enabled": enabled})
        if changed:
            self.notify("kill_switch", "system", None, None,
                        "Automations are switched on again." if enabled else "Automations are switched off. Nothing runs until an admin switches them on.",
                        owner=None, rooms=[])
            self.bc.publish(Event("automations_changed", {"rule_id": None, "change": "kill_switch", "automations_enabled": enabled},
                                  visible=lambda u: u.is_admin or u.role == "member"))
        return self.kill_switch()

    # ---- registry changes: broken rules, state cache
    async def resync(self) -> None:
        """Seed the state/online caches and re-validate every rule against the live registry (invariant 9)."""
        reg = await self.registry()
        if reg is None:
            return
        now = self.clock()
        self.node_of = {e: ep["node_id"] for e, ep in reg.items() if isinstance(ep.get("node_id"), str)}
        for eid, ep in reg.items():
            st = ep.get("state") if isinstance(ep.get("state"), dict) else {}
            self.state[eid] = dict(st)
            for cap, v in st.items():
                cur = self.since.get((eid, cap))
                if cur is None or not same(cur[0], v):
                    self.since[(eid, cap)] = (v, now)
            if ep.get("online") is False:
                self.offline.setdefault(eid, now)
            else:
                self.offline.pop(eid, None)
        for eid in [e for e in self.state if e not in reg]:
            self.state.pop(eid, None)
            self.offline.pop(eid, None)
        for r in self.db.all("SELECT * FROM automation_rules"):
            body = json.loads(r["body"])
            if all(e in reg for e, *_ in refs(body)):
                code, rooms = problems(body, reg)
            else:                                                      # forgotten endpoint: keep the last known rooms for visibility
                code, rooms = "bad_rule", set(json.loads(r["rooms"]))
            broken, was = code is not None, bool(r["broken"])
            if broken != was or sorted(rooms) != json.loads(r["rooms"]):
                self.db.run("UPDATE automation_rules SET broken=?,rooms=? WHERE rule_id=?", (int(broken), json.dumps(sorted(rooms)), r["rule_id"]))
                r = self._row(r["rule_id"])
                if broken and not was:
                    self.notify("rule_broken", f"automation:{r['rule_id']}", r["rule_id"], None,
                                f"Rule '{r['name']}' is broken: something it uses was removed or changed. It will not run until it is edited.",
                                owner=r["owner"], rooms=sorted(rooms))
                    self._changed(r, "broken")
                elif broken != was:
                    self._changed(r, "updated")
                else:
                    self._cache = None
        self._cache = None

    def usage_for_node(self, node_id: str) -> int:
        """Rules that reference an endpoint of this node (ForgetUsage.automations)."""
        eps = {e for e, n in self.node_of.items() if n == node_id}
        return sum(1 for r in self.db.all("SELECT body FROM automation_rules")
                   if any(e in eps for e, *_ in refs(json.loads(r["body"]))))

    # ---- engine
    def _active(self) -> list[dict]:
        if self._cache is None:
            self._cache = []
            for r in self.db.all("SELECT * FROM automation_rules WHERE status='approved' AND broken=0"):
                self._cache.append({"rule_id": r["rule_id"], **json.loads(r["body"])})
        return self._cache

    async def on_hub_event(self, env: dict) -> None:
        t, d = env.get("type"), env.get("data") or {}
        if not isinstance(d, dict):
            return
        if t == "registry_changed":
            await self.resync()
        elif t == "state":
            self._on_state(d)
        elif t == "node_status":
            self._on_node(d)

    def _on_state(self, d: dict) -> None:
        eid, vals = d.get("endpoint_id"), d.get("values")
        if not (isinstance(eid, str) and EP_RE.match(eid) and isinstance(vals, dict)):
            return
        cur, now = self.state.setdefault(eid, {}), self.clock()
        self.offline.pop(eid, None)                                  # a state report means the endpoint is back
        due = []
        for cap, v in vals.items():
            old = cur.get(cap, _MISSING)
            cur[cap] = v
            if old is not _MISSING and same(old, v):
                continue
            self.since[(eid, cap)] = (v, now)
            for r in self._active():
                t = r["trigger"]
                if t["type"] == "device_state_change" and t["endpoint_id"] == eid and t["capability"] == cap \
                        and ("to" not in t or same(t["to"], v)) and ("from" not in t or (old is not _MISSING and same(t["from"], old))):
                    due.append(r["rule_id"])
        for rid in due:
            self._spawn(self.fire(rid))

    def _on_node(self, d: dict) -> None:
        node, online = d.get("node_id"), d.get("online")
        if not isinstance(node, str) or not isinstance(online, bool):
            return
        now = self.clock()
        for eid, n in self.node_of.items():
            if n == node:
                if online:
                    self.offline.pop(eid, None)
                else:
                    self.offline.setdefault(eid, now)
        if not online:
            for rid in self._offline_due():
                self._spawn(self.fire(rid))

    def _offline_due(self) -> list[str]:
        now, due = self.clock(), []
        for r in self._active():
            t = r["trigger"]
            if t["type"] != "device_offline":
                continue
            since = self.offline.get(t["endpoint_id"])
            if since is not None and now - since >= t.get("for_s", OFFLINE_DEFAULT_S) and self._fired_ep.get(r["rule_id"]) != since:
                self._fired_ep[r["rule_id"]] = since
                due.append(r["rule_id"])
        return due

    async def tick(self) -> None:
        """Timer-driven triggers: time_of_day, schedule, duration_in_state, device_offline (after `for_s`)."""
        now, lt, due = self.clock(), self.local(), self._offline_due()
        for r in self._active():
            t, rid = r["trigger"], r["rule_id"]
            if t["type"] == "time_of_day":
                key = (lt.date().isoformat(), t["at"])
                if lt.strftime("%H:%M") == t["at"] and day_ok(t.get("days"), lt) and self._fired_ep.get(rid) != key:
                    self._fired_ep[rid] = key
                    due.append(rid)
            elif t["type"] == "schedule":
                last = self._sched.setdefault(rid, now)
                b = t.get("between")
                if now - last >= t["every_s"] and day_ok(t.get("days"), lt) and (not b or in_window(b["from"], b["to"], lt)):
                    self._sched[rid] = now
                    due.append(rid)
            elif t["type"] == "duration_in_state":
                ent = self.since.get((t["endpoint_id"], t["capability"]))
                if ent and same(ent[0], t["value"]) and now - ent[1] >= t["for_s"] and self._fired_ep.get(rid) != ent[1]:
                    self._fired_ep[rid] = ent[1]
                    due.append(rid)
        for rid in due:
            self._spawn(self.fire(rid))
        if now - self._last_housekeeping > 3600:
            self._last_housekeeping = now
            self.housekeeping()

    def housekeeping(self) -> None:
        cut = self.now() - RETENTION_S
        self.db.run("DELETE FROM automation_runs WHERE ts<?", (cut,))
        self.db.run("DELETE FROM notifications WHERE ts<?", (cut,))
        self.db.run("DELETE FROM notification_reads WHERE notification_id NOT IN (SELECT notification_id FROM notifications)")

    # ---- one run: kill switch -> status -> scope -> condition -> risk -> cooldown -> daily limit -> execute
    async def fire(self, rule_id: str) -> str | None:
        async with self.lock:
            try:
                return await self._fire(rule_id)
            except asyncio.CancelledError:
                raise
            except Exception:                                        # noqa: BLE001 - a rule bug never stops the engine
                log.exception("automation run failed")
                self._run(rule_id, "failed", error="engine_error")
                return "failed"

    async def _fire(self, rule_id: str) -> str | None:
        r = self._row(rule_id)
        if r is None:
            return None
        if not self.enabled:                                          # nothing is evaluated; sampled once per rule per hour
            if not self.db.one("SELECT 1 FROM automation_runs WHERE rule_id=? AND outcome='suppressed_kill_switch' AND ts>? LIMIT 1",
                               (rule_id, self.now() - 3600)):
                self._run(rule_id, "suppressed_kill_switch")
            return "suppressed_kill_switch"
        if r["status"] != "approved" or r["broken"]:
            return None
        body, rooms_known = json.loads(r["body"]), json.loads(r["rooms"])
        reg = await self.registry()
        if reg is None:
            self._run(rule_id, "failed", error="upstream_error")
            return "failed"
        act = body["action"]
        live_rooms = sorted({reg[e]["room"] for e, *_ in refs(body) if e in reg and reg[e].get("room")})
        owner = self.auth.get(r["owner"])
        if not in_scope(owner, live_rooms) or any(e not in reg for e, *_ in refs(body)):
            self._run(rule_id, "blocked_scope")
            if act["type"] == "set_state":
                self.actions.record("household", f"automation:{rule_id}", "set_state", "denied", endpoint_id=act["endpoint_id"],
                                    node_id=(reg.get(act["endpoint_id"]) or {}).get("node_id"), capability=act["capability"],
                                    value=act["value"], error="out_of_scope")
            return "blocked_scope"
        lt = self.local()
        for c in (body.get("condition") or {}).get("all", []):
            ok = window_ok(c, lt) if c["type"] == "time_window" else \
                ((reg[c["endpoint_id"]].get("state") or {}).get(c["capability"], _MISSING) is not _MISSING and
                 compare(c["op"], reg[c["endpoint_id"]]["state"][c["capability"]], c["value"]))
            if not ok:
                self._run(rule_id, "skipped_condition")
                return "skipped_condition"
        ep = reg.get(act["endpoint_id"]) if act["type"] == "set_state" else None
        if ep is not None and ep.get("risk") == "confirm":
            # No confirmation_request is created and nothing executes; the user does it themselves (invariant 1).
            lid = self.actions.record("household", f"automation:{rule_id}", "set_state", "blocked", endpoint_id=act["endpoint_id"],
                                      node_id=ep.get("node_id"), capability=act["capability"], value=act["value"],
                                      error="confirmation_required")
            self._run(rule_id, "blocked_confirm", action_status="blocked", action_log_id=lid, error="confirmation_required")
            self.notify("action_blocked", f"automation:{rule_id}", rule_id, act["endpoint_id"],
                        f"Rule '{r['name']}' wanted to change {_plain(ep.get('name'), 48)} ({act['capability']} = {_plain(act['value'], 40)}). "
                        "That needs your confirmation; open the app to do it yourself.",
                        owner=r["owner"], rooms=live_rooms, throttle_s=r["cooldown_s"])
            return "blocked_confirm"
        now = self.now()
        done = "SELECT ts FROM automation_runs WHERE rule_id=? AND outcome IN ('fired','failed') "
        last = self.db.one(done + "ORDER BY run_id DESC LIMIT 1", (rule_id,))
        if last and now - last["ts"] < r["cooldown_s"]:
            self._run(rule_id, "skipped_cooldown")
            return "skipped_cooldown"
        midnight = int(lt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        if self.db.one("SELECT COUNT(*) n FROM automation_runs WHERE rule_id=? AND outcome IN ('fired','failed') AND ts>=?",
                       (rule_id, midnight))["n"] >= r["max_runs_per_day"]:
            self._run(rule_id, "skipped_daily_limit")
            return "skipped_daily_limit"
        if act["type"] == "notify":
            self.notify(act["level"] if "level" in act else "info", f"automation:{rule_id}", rule_id, None, act["text"],
                        owner=r["owner"], rooms=live_rooms)
            self._run(rule_id, "fired")
            return "fired"
        return await self._set_state(r, act, ep, live_rooms)

    async def _set_state(self, r, act: dict, ep: dict, rooms: list[str]) -> str:
        rid, src = r["rule_id"], f"automation:{r['rule_id']}"
        kw = dict(endpoint_id=act["endpoint_id"], node_id=ep.get("node_id"), capability=act["capability"], value=act["value"])
        if ep.get("online") is False:
            self._run(rid, "skipped_offline", error="offline")
            return "skipped_offline"
        sent = self.db.one("SELECT COUNT(*) n FROM action_log WHERE source LIKE 'automation:%' AND kind='set_state' AND ts>? "
                           "AND status NOT IN ('blocked','denied') AND COALESCE(error,'')!='rate_limited'", (self.now() - 3600,))["n"]
        if sent >= SET_STATE_PER_HOUR:
            lid = self.actions.record("household", src, "set_state", "failed", error="rate_limited", **kw)
            self._run(rid, "failed", action_status="failed", action_log_id=lid, error="rate_limited")
            return "failed"
        try:
            status, res = await self.hub.hub("POST", f"/devices/{act['endpoint_id']}/set", actor="household", json={
                "capability": act["capability"], "value": act["value"], "user_id": "household"})   # never a confirmation_token
        except UpstreamError:
            status, res = 0, None
        if status == 200 and isinstance(res, dict) and res.get("ok"):
            st = "moving" if res.get("status") == "moving" else "done"
            lid = self.actions.record("household", src, "set_state", st, **kw)
            self._run(rid, "fired", action_status=st, action_log_id=lid)
            return "fired"
        err = res.get("error") if isinstance(res, dict) and isinstance(res.get("error"), str) and CODE_RE.match(res["error"]) else None
        if status == 404:
            err = "unknown_endpoint"
        err = err or "device_error"
        if err == "confirmation_required":
            lid = self.actions.record("household", src, "set_state", "blocked", error=err, **kw)
            self._run(rid, "blocked_confirm", action_status="blocked", action_log_id=lid, error=err)
            return "blocked_confirm"
        lid = self.actions.record("household", src, "set_state", "failed", error=err, **kw)
        self._run(rid, "skipped_offline" if err == "offline" else "failed", action_status="failed", action_log_id=lid, error=err)
        return "skipped_offline" if err == "offline" else "failed"


_MISSING = object()


async def run_engine(auto: Automations, stop: asyncio.Event, tick_s: float) -> None:
    """Timer loop for the engine (the hub subscriber feeds it events)."""
    while not stop.is_set():
        try:
            await auto.tick()
        except asyncio.CancelledError:
            raise
        except Exception:                                            # noqa: BLE001
            log.exception("automation tick failed")
        try:
            await asyncio.wait_for(stop.wait(), tick_s)
        except asyncio.TimeoutError:
            pass
