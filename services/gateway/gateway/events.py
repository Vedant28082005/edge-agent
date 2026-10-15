"""WebSocket fan-out: hub /events (gateway scope) and agent events, filtered per user role and rooms."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from .auth import User
from .security import new_id
from .upstream import JsonClient, UpstreamError

log = logging.getLogger("gateway.events")

HUB_TYPES = "registry_changed,scenes_changed,node_status,pending_node,state,action_settled,action_settle_timeout"


@dataclass
class Event:
    """`type` is a WebSocket message type (state|perception|notification|async_result|registry_changed|...).
    Routing: `user_id` = only that user; `admin_only`; `no_guest`; `endpoint_id`/`endpoint_ids`/`room` = room filter."""
    type: str
    data: dict
    user_id: str | None = None
    admin_only: bool = False
    no_guest: bool = False
    endpoint_id: str | None = None
    endpoint_ids: list[str] | None = None
    room: str | None = None
    visible: object | None = None            # callable(User) -> bool, evaluated at delivery time (scenes_changed)


@dataclass(eq=False)
class Client:
    user: User
    queue: asyncio.Queue
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class Broadcaster:
    def __init__(self, queue_size: int = 256) -> None:
        self.node_of: dict[str, str] = {}          # endpoint_id -> node_id (action log)
        self.room_of: dict[str, str] = {}          # endpoint_id -> room; never forgets (forgotten endpoints stay filtered)
        self.scenes: dict[str, dict] = {}          # scene_id -> last known scene (to judge visibility of a deleted scene)
        self.clients: set[Client] = set()
        self.queue_size = queue_size

    # ---- clients
    def add(self, user: User) -> Client:
        c = Client(user, asyncio.Queue(self.queue_size))
        self.clients.add(c)
        return c

    def remove(self, c: Client) -> None:
        self.clients.discard(c)

    def update_user(self, user_id: str, user: User | None) -> None:
        """Role/room change, disable or delete takes effect on live sockets immediately."""
        for c in list(self.clients):
            if c.user.user_id == user_id:
                if user is None or user.disabled:
                    c.closed.set()
                else:
                    c.user = user

    # ---- filtering (pure)
    def render(self, user: User, ev: Event) -> dict | None:
        if ev.visible is not None:
            return {"type": ev.type, **ev.data} if ev.visible(user) else None
        if ev.user_id is not None:
            return {"type": ev.type, **ev.data} if ev.user_id == user.user_id else None
        if ev.admin_only:
            return {"type": ev.type, **ev.data} if user.is_admin else None
        if ev.no_guest and user.role == "guest":
            return None
        data = ev.data
        if ev.endpoint_ids is not None:
            if not user.is_admin:
                vis = [e for e in ev.endpoint_ids if self.room_of.get(e) in user.rooms]
                if ev.endpoint_ids and not vis:
                    return None
                data = {**data, "endpoint_ids": vis}
        else:
            room = ev.room if ev.room is not None else (self.room_of.get(ev.endpoint_id) if ev.endpoint_id else None)
            if (ev.endpoint_id or ev.room is not None) and not user.can_room(room):
                return None
        return {"type": ev.type, **data}

    def publish(self, ev: Event) -> None:
        for c in list(self.clients):
            msg = self.render(c.user, ev)
            if msg is None:
                continue
            try:
                c.queue.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("ws client too slow; dropping it")
                c.closed.set()


NODE_RE = re.compile(r"^esp32-[0-9a-f]{12}$")
EP_RE = re.compile(r"^ep_[0-9a-f]{8}$")


# ---- WsMessage shapes (gateway.yaml 2.11.0): every message is built by one of these, so what leaves the gateway is exactly
# the contract's shape. Anything that does not fit is dropped (None), never forwarded half-formed.
def _int(v, lo: int = 0) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo


def _str(v, n: int) -> bool:
    return isinstance(v, str) and len(v) <= n


def _scalar(v) -> bool:
    return isinstance(v, (bool, int, float, str)) and not (isinstance(v, float) and v != v)


def _num(v, lo: float, hi: float) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def clean_state(d: dict) -> dict | None:
    ep, vals, ts = d.get("endpoint_id"), d.get("values"), d.get("ts")
    if isinstance(ts, float):
        ts = int(ts)
    if _int(ts) and ts > 100_000_000_000:                   # milliseconds -> seconds
        ts //= 1000
    if not (isinstance(ep, str) and EP_RE.match(ep) and isinstance(vals, dict) and vals and all(_scalar(v) for v in vals.values())
            and _int(ts, 1_000_000_000)):
        return None
    return {"endpoint_id": ep, "values": vals, "ts": ts}


def clean_settle(name: str, d: dict) -> dict | None:
    """action_settled / action_settle_timeout. `call_id` is required by the contract: a hub event for a gateway-initiated
    command has none, so the gateway labels it `gw_<random>` (the message is not tied to any chat request)."""
    cid = d.get("call_id")
    if not (isinstance(cid, str) and 0 < len(cid) <= 64):
        cid = new_id("gw_")
    base = {"call_id": cid, "endpoint_id": d.get("endpoint_id"), "capability": d.get("capability"), "value": d.get("value")}
    if not (isinstance(base["endpoint_id"], str) and EP_RE.match(base["endpoint_id"]) and isinstance(base["capability"], str)
            and len(base["capability"]) <= 64 and _scalar(base["value"])):
        return None
    if name == "action_settled":
        if not (_scalar(d.get("observed")) and _int(d.get("elapsed_ms"))):
            return None
        return {**base, "observed": d["observed"], "elapsed_ms": d["elapsed_ms"]}
    obs = d.get("observed")
    if not ((obs is None or _scalar(obs)) and _num(d.get("settle_s"), 1, 120)):
        return None
    return {**base, "observed": obs, "settle_s": d["settle_s"]}


def from_hub(env: dict) -> Event | None:
    """Translate a hub EventEnvelope into a WebSocket event (None = not forwarded).
    Settle events that carry a `call_id` came from an agent call: they reach the user through the agent's GET /v1/events
    stream (routed to that user), so they are skipped here to avoid duplicates; the others (gateway-initiated
    commands) go to everyone who can see the endpoint's room."""
    t, d = env.get("type"), env.get("data") or {}
    if t == "registry_changed":
        ids = [x for x in (d.get("endpoint_ids") or []) if isinstance(x, str) and EP_RE.match(x)]
        if not _int(d.get("version")):
            return None
        return Event("registry_changed", {"version": d["version"], "endpoint_ids": ids}, endpoint_ids=ids)
    if t == "state":
        m = clean_state(d)
        return Event("state", m, endpoint_id=m["endpoint_id"]) if m else None
    if t in ("action_settled", "action_settle_timeout"):
        if d.get("call_id"):
            return None
        m = clean_settle(t, d)
        return Event(t, m, endpoint_id=m["endpoint_id"]) if m else None
    node = d.get("node_id") if isinstance(d.get("node_id"), str) and NODE_RE.match(d.get("node_id")) else None
    if t == "node_status" and node:                          # infrastructure detail: admins only
        return Event("notification", {"level": "info", "text": f"{node} is {'online' if d.get('online') else 'offline'}"}, admin_only=True)
    if t == "pending_node" and node:
        return Event("notification", {"level": "info", "text": f"{node} is waiting for approval"}, admin_only=True)
    return None                                              # scenes_changed is handled by scene_events (needs the hub)


def clean_agent(name: str, d: dict) -> dict | None:
    """Agent event fields -> WsMessage fields (None = not forwardable). Only the contract's fields are kept."""
    if name in ("action_settled", "action_settle_timeout"):
        return clean_settle(name, d) if isinstance(d.get("call_id"), str) and d.get("call_id") else None
    if name == "async_result":
        if _str(d.get("ref"), 64) and d.get("ref") and d.get("kind") == "vision" and isinstance(d.get("text"), str) \
                and _int(d.get("latency_ms")):
            return {"ref": d["ref"], "kind": "vision", "text": d["text"][:4000], "latency_ms": d["latency_ms"]}
        return None
    if name == "notification":
        if isinstance(d.get("level"), str) and 0 < len(d["level"]) <= 32 and isinstance(d.get("text"), str):
            return {"level": d["level"], "text": d["text"][:500]}
        return None
    return None


def from_agent(name: str, d: dict) -> Event | None:
    """An event of the agent's GET /v1/events (`data.user_id` = owner, or "household" = no single owner: the gateway decides).
    Only the contract's fields are forwarded (agent-events.md). Owner events go to that user only. Household events:
    settle events to everyone who can see the endpoint's room, the rest to admin and members, never guests.
    `confirmation_request` is handled by the subscriber."""
    if not isinstance(d, dict):
        return None
    m = clean_agent(name, d)
    if m is None:
        return None
    owner = d.get("user_id") if isinstance(d.get("user_id"), str) and d.get("user_id") != "household" else None
    ep = m["endpoint_id"] if name.startswith("action_") else None
    return Event(name, m, user_id=owner, endpoint_id=ep, no_guest=owner is None and ep is None)


def confirmation_request_event(user_id: str, request_id: str, summary: str, action: dict, action_hash: str, expires_in_s: int) -> Event:
    """The WsMessage `confirmation_request` from what the gateway stored (canonical action, bounded fields)."""
    return Event("confirmation_request", {"request_id": request_id, "summary": str(summary)[:200], "action": action,
                                          "action_hash": action_hash, "expires_in_s": max(1, min(int(expires_in_s), 300))},
                 user_id=user_id)


SCENE_CHANGE = {"create": "created", "delete": "deleted"}


async def scene_events(env: dict, hub, bc: Broadcaster, assigned) -> list[Event]:
    """hub `scenes_changed {reason, scene_ids}` -> one WebSocket `scenes_changed {scene_id, change}` per scene, delivered only to
    users who can see that scene (admin; members by rooms or ownership; guests by assignment), judged when it is delivered; a
    deleted scene is judged by its last known content (visibility just before the deletion). Unknown scenes: admins only."""
    from . import policy
    d = env.get("data") or {}
    change = SCENE_CHANGE.get(d.get("reason"), "updated")          # broken/unbroken flags and re-validation are updates
    out = []
    for sid in (d.get("scene_ids") or [])[:100]:
        if not isinstance(sid, str):
            continue
        scene = None
        if change == "deleted":
            scene = bc.scenes.pop(sid, None)
        else:
            try:
                status, body = await hub.hub("GET", f"/scenes/{sid}")
            except UpstreamError:
                status, body = 0, None
            if status == 200 and isinstance(body, dict):
                scene = bc.scenes[sid] = body
            else:
                scene = bc.scenes.get(sid)
        if scene is None:
            out.append(Event("scenes_changed", {"scene_id": sid, "change": change}, admin_only=True))
        else:
            snap = dict(scene)
            out.append(Event("scenes_changed", {"scene_id": sid, "change": change},
                             visible=lambda u, snap=snap: policy.scene_visible(u, snap, assigned(u))))
    return out


def note_settle(actions, name: str, d) -> None:
    """A settle event from the hub or the agent closes the matching `moving` row in the action log."""
    if isinstance(d, dict):
        actions.settle(name == "action_settled", d.get("endpoint_id"), d.get("capability"), d.get("value"), d.get("call_id"),
                       d.get("elapsed_ms"))


async def refresh_scenes(hub, bc: Broadcaster) -> None:
    try:
        status, body = await hub.hub("GET", "/scenes")
    except UpstreamError:
        return
    if status == 200 and isinstance(body, list):
        bc.scenes = {s["scene_id"]: s for s in body if isinstance(s, dict) and isinstance(s.get("scene_id"), str)}


async def refresh_rooms(hub, bc: Broadcaster) -> None:
    try:
        status, body = await hub.hub("GET", "/devices")
    except UpstreamError:
        return
    if status == 200 and isinstance(body, list):
        for e in body:
            if isinstance(e, dict) and e.get("endpoint_id"):
                bc.room_of[e["endpoint_id"]] = e.get("room", "")
                if isinstance(e.get("node_id"), str):
                    bc.node_of[e["endpoint_id"]] = e["node_id"]


async def run_hub_subscriber(hub: JsonClient, bc: Broadcaster, stop: asyncio.Event, retry_s: float = 1.0, assigned=None, actions=None) -> None:
    last_id: str | None = None
    delay = retry_s
    while not stop.is_set():
        try:
            await refresh_rooms(hub, bc)
            await refresh_scenes(hub, bc)
            hdr = {"Last-Event-ID": last_id} if last_id else None
            async for fid, ev, data in hub.sse("/events", params={"types": HUB_TYPES}, headers=hdr):
                delay = retry_s
                if fid:
                    last_id = fid
                try:
                    env = json.loads(data)
                except ValueError:
                    continue
                if ev == "hello" or not isinstance(env, dict):
                    continue
                if actions is not None and env.get("type") in ("action_settled", "action_settle_timeout"):
                    note_settle(actions, env.get("type"), env.get("data"))
                if env.get("type") == "registry_changed":
                    await refresh_rooms(hub, bc)
                if env.get("type") == "scenes_changed" and assigned is not None:
                    for sev in await scene_events(env, hub, bc, assigned):
                        bc.publish(sev)
                    continue
                out = from_hub(env)
                if out:
                    bc.publish(out)
            wait = retry_s                             # clean end of stream: reconnect promptly with Last-Event-ID
        except UpstreamError:
            wait, delay = delay, min(delay * 2, 30.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hub subscriber error")
            wait, delay = delay, min(delay * 2, 30.0)
        try:
            await asyncio.wait_for(stop.wait(), wait)
        except asyncio.TimeoutError:
            pass


async def run_agent_subscriber(agent: JsonClient, bc: Broadcaster, confirm, stop: asyncio.Event, retry_s: float = 2.0,
                               actions=None) -> None:
    """Consumes the agent's GET /v1/events (agent-internal.yaml): reconnects with Last-Event-ID; `reset` = older than the
    agent's buffer, missed events are lost."""
    last_id: str | None = None
    delay = retry_s
    while not stop.is_set():
        try:
            hdr = {"Last-Event-ID": last_id} if last_id else None
            async for fid, ev, data in agent.sse("/v1/events", headers=hdr):
                delay = retry_s
                if fid:
                    last_id = fid
                if ev == "reset":
                    log.warning("agent event buffer was reset; missed events are lost")
                    continue
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                uid = d.get("user_id")
                if ev == "confirmation_request":
                    if isinstance(uid, str) and uid != "household":
                        if confirm.register_request(uid, None, d):
                            bc.publish(confirm.request_event(d["request_id"]))
                    continue
                if actions is not None and ev in ("action_settled", "action_settle_timeout"):
                    note_settle(actions, ev, d)
                out = from_agent(ev, d)
                if out:
                    bc.publish(out)
            wait = retry_s
        except UpstreamError:
            wait, delay = delay, min(delay * 2, 60.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent subscriber error")
            wait, delay = delay, min(delay * 2, 60.0)
        try:
            await asyncio.wait_for(stop.wait(), wait)
        except asyncio.TimeoutError:
            pass
