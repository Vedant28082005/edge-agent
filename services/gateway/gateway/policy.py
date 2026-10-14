"""Role and room rules. The hub applies none; they live here."""
from __future__ import annotations

from .auth import User


def endpoint_visible(user: User, ep: dict) -> bool:
    if user.is_admin:
        return True
    return bool(ep.get("enabled", True)) and ep.get("room") in user.rooms


def scene_rooms_ok(user: User, scene: dict) -> bool:
    """All rooms the scene touches are accessible. `*` (selector without a room) cannot be verified: admin only."""
    if user.is_admin:
        return True
    rooms = scene.get("rooms")
    if not isinstance(rooms, list):
        return False
    return all(r != "*" and r in user.rooms for r in rooms)


def scene_visible(user: User, scene: dict, assigned: set[str]) -> bool:
    if user.is_admin:
        return True
    if user.role == "guest":
        return scene.get("scene_id") in assigned
    return scene.get("owner") == user.user_id or scene_rooms_ok(user, scene)


def can_run_scene(user: User, scene: dict, assigned: set[str]) -> bool:
    if user.is_admin:
        return True
    if user.role == "guest":
        return scene.get("scene_id") in assigned
    return scene_rooms_ok(user, scene)


def action_rooms(actions: list[dict], room_of: dict[str, str]) -> set[str]:
    """Rooms a scene input touches; `*` for a selector without a room or a fixed endpoint we cannot place."""
    out: set[str] = set()
    for a in actions:
        t = a.get("target") or {}
        if "endpoint_id" in t:
            out.add(room_of.get(t["endpoint_id"], "*"))
        else:
            out.add(t.get("room") or "*")
    return out
