"""Per-scope views of the registry: what a restricted caller's prompt, grammar, fast path and router may see.

`allowed_rooms` / `allowed_scenes` (agent-internal.yaml 1.3.0) mean out-of-scope names must never reach the model. The
shared prompt prefix is byte-stable for the llama.cpp prefix cache, so the UNRESTRICTED scope keeps using the main
`PromptBuilder` untouched; each distinct restricted scope gets its own filtered `DeviceRegistry` view and its own builder,
rebuilt when the registry reloads (a small LRU)."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from .devices import DeviceRegistry
from .prompt import PromptBuilder
from .scope import Scope


@dataclass
class View:
    scope: Scope
    registry: DeviceRegistry
    generation: int
    _main: PromptBuilder | None = None
    _builder: PromptBuilder | None = field(default=None, repr=False)

    @property
    def builder(self) -> PromptBuilder:
        if self._builder is None:
            self._builder = self._main or PromptBuilder(self.registry)
        return self._builder


class ScopedViews:
    MAX = 32

    def __init__(self, registry: DeviceRegistry, main_builder: PromptBuilder | None = None) -> None:
        self.registry, self.main = registry, main_builder
        self.format = main_builder.format if main_builder else "json"
        self._cache: OrderedDict[str, View] = OrderedDict()

    def get(self, scope: Scope) -> View:
        if not scope.restricted:
            return View(scope, self.registry, self.registry.generation, self.main)      # live registry, shared builder
        key = scope.key
        v = self._cache.get(key)
        if v is not None and v.generation == self.registry.generation:
            self._cache.move_to_end(key)
            return v
        builder = PromptBuilder(self.registry.scoped(scope), format=self.format,
                                generated=bool(self.main and self.main.generated))
        v = View(scope, self.registry.scoped(scope), self.registry.generation, _builder=builder)
        self._cache[key] = v
        while len(self._cache) > self.MAX:
            self._cache.popitem(last=False)
        return v
