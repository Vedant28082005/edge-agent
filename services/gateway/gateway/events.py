"""WebSocket fan-out: hub /events (gateway scope) and agent events, filtered per user role and rooms."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from .auth import User
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


@dataclass(eq=False)
class Client:
    user: User
    queue: asyncio.Queue
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class Broadcaster:
    def __init__(self, queue_size: int = 256) -> None:
        self.room_of: dict[str, str] = {}          # endpoint_id -> room; never forgets (forgotten endpoints stay filtered)
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


NODE_RE = __import__("re").compile(r"^esp32-[0-9a-f]{12}$")


def from_hub(env: dict) -> Event | None:
    """Translate a hub EventEnvelope into a WebSocket event (None = not forwarded).
    Settle events that carry a `call_id` came from an agent call: they reach the user through the agent's GET /v1/events
    stream (routed to that user), so they are skipped here to avoid duplicates; the others (gateway-initiated
    commands) go to everyone who can see the endpoint's room."""
    t, d = env.get("type"), env.get("data") or {}
    if t == "registry_changed":
        return Event("registry_changed", {"version": d.get("version"), "reason": d.get("reason"),
                                          "endpoint_ids": d.get("endpoint_ids") or []},
                     endpoint_ids=list(d.get("endpoint_ids") or []))
    if t == "state":
        return Event("state", {"endpoint_id": d.get("endpoint_id"), "values": d.get("values"), "ts": d.get("ts")},
                     endpoint_id=d.get("endpoint_id"))
    if t in ("action_settled", "action_settle_timeout"):
        if d.get("call_id"):
            return None
        keys = ("endpoint_id", "capability", "value", "observed", "elapsed_ms" if t == "action_settled" else "settle_s")
        return Event(t, {k: d.get(k) for k in keys}, endpoint_id=d.get("endpoint_id"))
    node = d.get("node_id") if isinstance(d.get("node_id"), str) and NODE_RE.match(d.get("node_id")) else None
    if t == "node_status" and node:                          # infrastructure detail: admins only
        return Event("notification", {"level": "info", "text": f"{node} is {'online' if d.get('online') else 'offline'}"}, admin_only=True)
    if t == "pending_node" and node:
        return Event("notification", {"level": "info", "text": f"{node} is waiting for approval"}, admin_only=True)
    return None                                              # scenes_changed etc.: not part of the WebSocket contract


AGENT_FIELDS = {
    "async_result": ("ref", "kind", "text", "latency_ms"),
    "action_settled": ("call_id", "endpoint_id", "capability", "value", "observed", "elapsed_ms"),
    "action_settle_timeout": ("call_id", "endpoint_id", "capability", "value", "observed", "settle_s"),
    "notification": ("level", "text"),
}


def from_agent(name: str, d: dict) -> Event | None:
    """An event of the agent's GET /v1/events (`data.user_id` = owner, or "household" = no single owner: the gateway decides).
    Only the contract's fields are forwarded (agent-events.md). Owner events go to that user only. Household events:
    settle events to everyone who can see the endpoint's room, the rest to admin and members, never guests.
    `confirmation_request` is handled by the subscriber."""
    fields = AGENT_FIELDS.get(name)
    if fields is None or not isinstance(d, dict):
        return None
    owner = d.get("user_id") if isinstance(d.get("user_id"), str) and d.get("user_id") != "household" else None
    ep = d.get("endpoint_id") if name.startswith("action_") and isinstance(d.get("endpoint_id"), str) else None
    return Event(name, {k: d[k] for k in fields if k in d}, user_id=owner, endpoint_id=ep,
                 no_guest=owner is None and ep is None)


async def refresh_rooms(hub, bc: Broadcaster) -> None:
    try:
        status, body = await hub.hub("GET", "/devices")
    except UpstreamError:
        return
    if status == 200 and isinstance(body, list):
        for e in body:
            if isinstance(e, dict) and e.get("endpoint_id"):
                bc.room_of[e["endpoint_id"]] = e.get("room", "")


async def run_hub_subscriber(hub: JsonClient, bc: Broadcaster, stop: asyncio.Event, retry_s: float = 1.0) -> None:
    last_id: str | None = None
    delay = retry_s
    while not stop.is_set():
        try:
            await refresh_rooms(hub, bc)
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
                if env.get("type") == "registry_changed":
                    await refresh_rooms(hub, bc)
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


async def run_agent_subscriber(agent: JsonClient, bc: Broadcaster, confirm, stop: asyncio.Event, retry_s: float = 2.0) -> None:
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
                    if isinstance(uid, str) and uid != "household" and confirm.register_request(uid, None, d):
                        bc.publish(Event("confirmation_request", {k: d.get(k) for k in
                                         ("request_id", "summary", "action", "action_hash", "expires_in_s")}, user_id=uid))
                    continue
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
