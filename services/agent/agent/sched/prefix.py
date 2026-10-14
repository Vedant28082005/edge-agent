"""Prefix-cache bookkeeping. The byte-stable prefix itself is built by `PromptBuilder` (system prompt + tool text + device
list, no live state, rebuilt only when the registry content changes) and every per-turn / per-user part (history, summary,
memories, current text) comes after it. This module (a) holds the request fields that pin the llama-server slot, and (b)
records, per request, whether the prefix hash equals the previous request's, which is the precondition for a KV cache hit,
together with what the server reported (`timings.cache_n`).

`cache_prompt: true` + `id_slot: <n>` is sent on every request when the `prefix_cache` flag is on; with it off neither is
sent (ablation: llama-server then re-evaluates the whole prompt, the "cold prefix" case)."""
from __future__ import annotations

from dataclasses import dataclass


def cache_fields(enabled: bool, id_slot: int) -> dict:
    return {"cache_prompt": True, "id_slot": id_slot} if enabled else {"cache_prompt": False}


@dataclass
class PrefixObservation:
    prefix_hash: str
    same_as_previous: bool
    prompt_n: int | None
    cache_n: int | None

    @property
    def cache_hit(self) -> bool:
        """True when the prefix was unchanged AND the server (if it reports) actually reused cached tokens."""
        if not self.same_as_previous:
            return False
        return self.cache_n is None or self.cache_n > 0


class PrefixTracker:
    def __init__(self) -> None:
        self.last_hash = ""
        self.requests = 0
        self.stable = 0
        self.hits = 0

    def observe(self, prefix_hash: str, timings: dict | None) -> PrefixObservation:
        t = timings or {}
        same = bool(self.last_hash) and prefix_hash == self.last_hash
        obs = PrefixObservation(prefix_hash, same, t.get("prompt_n"), t.get("cache_n"))
        self.last_hash = prefix_hash
        self.requests += 1
        self.stable += int(same)
        self.hits += int(obs.cache_hit)
        return obs

    def stats(self) -> dict:
        return {"requests": self.requests, "prefix_stable": self.stable, "cache_hits": self.hits}
