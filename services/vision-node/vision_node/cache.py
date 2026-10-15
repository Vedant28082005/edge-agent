"""30 s answer cache. Keyed by a hash (operation, image hash, question, bounds); stores the ANSWER only: never the image,
the question or the audio. In RAM, never persisted."""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any


class AnswerCache:
    def __init__(self, ttl_s: float = 30.0, max_entries: int = 64, clock=time.monotonic):
        self.ttl = ttl_s
        self.max = max_entries
        self._clock = clock
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    @staticmethod
    def key(op: str, image: bytes, question: str, extra: Any = None) -> str:
        h = hashlib.sha256()
        h.update(op.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(image).digest())
        h.update(b"\0")
        h.update(hashlib.sha256(question.encode("utf-8")).digest())
        h.update(b"\0")
        h.update(json.dumps(extra, sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()

    def get(self, key: str) -> Any | None:
        item = self._d.get(key)
        if item is None:
            return None
        if self._clock() - item[0] > self.ttl:
            del self._d[key]
            return None
        return json.loads(item[1])       # a copy: callers cannot mutate the stored answer

    def put(self, key: str, answer: Any) -> None:
        self._d[key] = (self._clock(), json.dumps(answer))
        self._d.move_to_end(key)
        while len(self._d) > self.max:
            self._d.popitem(last=False)
