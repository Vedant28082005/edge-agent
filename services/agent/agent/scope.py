"""Per-request scope (contracts/openapi/agent-internal.yaml 1.3.0): which rooms and scenes the caller may touch.

The gateway sends `allowed_rooms` / `allowed_scenes` with every chat request. Fail closed:
* a MISSING field is `[]` (nothing allowed) for member and guest;
* an explicit `null` means unrestricted, and is honoured only for `admin` and `member`, never for `guest`
  (a guest's `null` becomes `[]`);
* anything that is not a list of strings (or null) is treated as `[]`.
The scope is snapshotted for the turn, never widened from text, and enforced in the executor independent of the model."""
from __future__ import annotations

from dataclasses import dataclass

from .util import clean_label

_MISSING = object()


@dataclass(frozen=True)
class Scope:
    rooms: frozenset[str] | None = None        # None = unrestricted
    scenes: frozenset[str] | None = None       # permanent scene ids; None = unrestricted

    @property
    def restricted(self) -> bool:
        return self.rooms is not None or self.scenes is not None

    @property
    def key(self) -> str:
        """Stable cache key (views, prompt prefixes and grammars are built per distinct scope)."""
        r = "*" if self.rooms is None else "|".join(sorted(self.rooms))
        s = "*" if self.scenes is None else "|".join(sorted(self.scenes))
        return f"r={r};s={s}"

    def room_ok(self, room: object) -> bool:
        return self.rooms is None or (isinstance(room, str) and room in self.rooms)

    def scene_id_ok(self, scene_id: object) -> bool:
        return self.scenes is None or (isinstance(scene_id, str) and scene_id in self.scenes)

    def for_guest(self) -> "Scope":
        """A guest is never unrestricted, whatever was sent."""
        return Scope(self.rooms if self.rooms is not None else frozenset(),
                     self.scenes if self.scenes is not None else frozenset())


UNRESTRICTED = Scope(None, None)
NOTHING = Scope(frozenset(), frozenset())


def _norm(v: object, label: bool) -> frozenset[str] | None:
    """None for an explicit null; a frozenset otherwise (malformed = empty = nothing allowed)."""
    if v is None:
        return None
    if not isinstance(v, (list, tuple, set, frozenset)):
        return frozenset()
    out = set()
    for x in v:
        if isinstance(x, str) and x:
            out.add(clean_label(x, 48) if label else x)       # registry rooms were sanitised at ingest; compare like with like
    return frozenset(out)


def make_scope(role: str, allowed_rooms: object = _MISSING, allowed_scenes: object = _MISSING) -> Scope:
    """Fail-closed scope for a request. Pass `_MISSING` (the default) for a field the body did not carry."""
    if role in ("member", "guest"):
        rooms = frozenset() if allowed_rooms is _MISSING else _norm(allowed_rooms, True)
        scenes = frozenset() if allowed_scenes is _MISSING else _norm(allowed_scenes, False)
    elif role == "admin":
        rooms = None if allowed_rooms is _MISSING else _norm(allowed_rooms, True)
        scenes = None if allowed_scenes is _MISSING else _norm(allowed_scenes, False)
    else:
        return NOTHING
    sc = Scope(rooms, scenes)
    return sc.for_guest() if role == "guest" else sc


MISSING = _MISSING
