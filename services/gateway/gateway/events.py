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


def from_hub(env: dict) -> Event | None:
    """Translate a hub EventEnvelope into a WebSocket event (None = not forwarded)."""
    t, d = env.get("type"), env.get("data") or {}
    if t == "registry_changed":
        return Event("registry_changed", {"version": d.get("version"), "reason": d.get("reason"),
                                          "endpoint_ids": d.get("endpoint_ids") or []},
                     endpoint_ids=list(d.get("endpoint_ids") or []))
    if t == "state":
        return Event("state", {"endpoint_id": d.get("endpoint_id"), "values": d.get("values"), "ts": d.get("ts")},
                     endpoint_id=d.get("endpoint_id"))
    if t in ("action_settled", "action_settle_timeout"):
        return Event("notification", {"kind": t, **{k: v for k, v in d.items() if k != "call_id"}},
                     endpoint_id=d.get("endpoint_id"))
    if t == "node_status":                                   # infrastructure detail: admins only
        return Event("notification", {"kind": "node_status", **d}, admin_only=True)
    if t == "pending_node":
        return Event("notification", {"kind": "pending_node", **d}, admin_only=True)
    if t == "scenes_changed":
        return Event("notification", {"kind": "scenes_changed", **d}, no_guest=True)
    return None


def from_agent(name: str, d: dict) -> Event | None:
    """Agent events (PROVISIONAL push path, see README): async_result, notification, perception. Optional routing hints
    `user_id`, `room`, `endpoint_id`, `admin_only` are honoured; a perception/notification without a hint goes to
    admin and members only."""
    if not isinstance(d, dict):
        return None
    allowed = {"async_result", "notification", "perception"}
    if name not in allowed:
        return None
    data = {k: v for k, v in d.items() if k not in ("user_id", "admin_only")}
    uid = d.get("user_id") if isinstance(d.get("user_id"), str) else None
    room = d.get("room") if isinstance(d.get("room"), str) else None
    ep = d.get("endpoint_id") if isinstance(d.get("endpoint_id"), str) else None
    return Event(name, data, user_id=uid, admin_only=bool(d.get("admin_only")), no_guest=uid is None and room is None and ep is None,
                 room=room, endpoint_id=ep)


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
        except UpstreamError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hub subscriber error")
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, 30.0)


async def run_agent_subscriber(agent: JsonClient, bc: Broadcaster, stop: asyncio.Event, retry_s: float = 2.0) -> None:
    """Optional: consumes the agent's push stream GET /v1/events (PROVISIONAL, not in the contract)."""
    delay = retry_s
    while not stop.is_set():
        try:
            async for _fid, ev, data in agent.sse("/v1/events"):
                delay = retry_s
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                out = from_agent(ev, d)
                if out:
                    bc.publish(out)
        except UpstreamError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent subscriber error")
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, 60.0)
