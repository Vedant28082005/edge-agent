"""llama-server client (OpenAI-compatible /v1/chat/completions).

* temperature 0, `cache_prompt: true`, `id_slot: 0` (single slot: the static prefix stays in that slot's KV cache).
* Constrained mode (runtime switch): `response_format = {"type": "json_schema", "json_schema": {"schema": <from the live
  registry>}}`; llama.cpp compiles it to a GBNF grammar and masks tokens, so the reply can only be a plan whose calls
  name existing endpoints, their own capabilities and in-range values. Unconstrained mode sends no schema (S10 ablation);
  the executor's validation still protects the hardware.
* One request at a time (the slot is single); at most `max_queued` more wait, beyond that `LlmBusy` (SSE `llm_busy`).
* The reply's `timings` (prompt_n, prompt_ms, cache_n, predicted_n, ...) are returned for measurement."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

log = logging.getLogger("agent.llm")


class LlmError(Exception):
    pass


class LlmBusy(LlmError):
    pass


@dataclass
class LlmResult:
    content: str
    timings: dict = field(default_factory=dict)
    finish_reason: str = ""


class Llm(Protocol):
    constrained: bool

    async def complete(self, messages: list[dict], schema: dict | None) -> LlmResult: ...


class LlmClient:
    def __init__(self, base_url: str, model: str = "local", constrained: bool = True, max_tokens: int = 256,
                 timeout_s: float = 600.0, max_queued: int = 2, http: httpx.AsyncClient | None = None) -> None:
        self._base, self.model, self.constrained, self.max_tokens = base_url.rstrip("/"), model, constrained, max_tokens
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=3.0))
        self._own_http = http is None
        self._slot = asyncio.Semaphore(1)
        self._waiting = 0
        self._max_queued = max_queued

    async def health(self) -> bool:
        try:
            r = await self._http.get(self._base + "/health", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    def payload(self, messages: list[dict], schema: dict | None) -> dict:
        body: dict = {"model": self.model, "messages": messages, "temperature": 0, "top_k": 1, "seed": 0,
                      "max_tokens": self.max_tokens, "stream": False, "cache_prompt": True, "id_slot": 0}
        if self.constrained and schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "plan", "strict": True, "schema": schema}}
        return body

    async def complete(self, messages: list[dict], schema: dict | None) -> LlmResult:
        if self._slot.locked() and self._waiting >= self._max_queued:
            raise LlmBusy("language model busy")
        self._waiting += 1
        try:
            await self._slot.acquire()
        finally:
            self._waiting -= 1
        try:
            try:
                r = await self._http.post(self._base + "/v1/chat/completions", json=self.payload(messages, schema))
            except httpx.HTTPError as e:
                raise LlmError(f"llama-server unreachable: {type(e).__name__}") from None
            if r.status_code == 503:
                raise LlmBusy("llama-server busy or loading")
            if r.status_code >= 400:
                raise LlmError(f"llama-server HTTP {r.status_code}")
            j = r.json()
            ch = (j.get("choices") or [{}])[0]
            return LlmResult((ch.get("message") or {}).get("content") or "", j.get("timings") or {},
                             ch.get("finish_reason") or "")
        finally:
            self._slot.release()
