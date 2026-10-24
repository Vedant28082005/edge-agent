"""Per-turn endpoint aliases (e1, e2, ...) for the compact LLM output grammar (S8i): the model never generates or
reads back a full `endpoint_id` (11 bytes incl. quotes), only a 2-3 byte alias, cutting generated and (pre-cache)
evaluated tokens.

Numbered strictly by sorted `endpoint_id` -- NOT by the device list's own (room, name, endpoint_id) display order.
`PromptBuilder` rebuilds the device list (and this map) on every registry change, including a label-only rename, but
`sched.grammar.ConstraintCache` rebuilds the decoding schema (and its own copy of this map) only when content that
changes decoding does (ids/capabilities/ranges/enums); a rename alone is a cache hit. Were aliases numbered by name,
a rename would silently re-number them in the prompt while the cached schema (and `planner.parse_plan`'s decode) kept
the old numbering -- the model would then use an alias that decodes to the wrong endpoint. Keying only on the
endpoint_id set makes both call sites agree deterministically without having to stay in lockstep by hand: the mapping
changes if and only if a device is added or removed, which already invalidates both caches together."""
from __future__ import annotations

from dataclasses import dataclass, field

from .devices import DeviceRegistry


@dataclass(frozen=True)
class AliasMap:
    to_alias: dict[str, str] = field(default_factory=dict)     # endpoint_id -> alias
    to_ep: dict[str, str] = field(default_factory=dict)        # alias -> endpoint_id

    def alias(self, endpoint_id: object) -> str:
        """The alias for a known endpoint id, else the id itself (so a stale/unknown id is never silently hidden)."""
        return self.to_alias.get(endpoint_id, endpoint_id) if isinstance(endpoint_id, str) else "?"


def build_aliases(reg: DeviceRegistry) -> AliasMap:
    to_alias: dict[str, str] = {}
    to_ep: dict[str, str] = {}
    for i, ep in enumerate(sorted(reg.devices), start=1):
        alias = f"e{i}"
        to_alias[ep] = alias
        to_ep[alias] = ep
    return AliasMap(to_alias, to_ep)
