"""llama-server client (OpenAI-compatible /v1/chat/completions).

* temperature 0, `cache_prompt: true`, `id_slot` pinned (single slot: the static prefix stays in that slot's KV cache);
  both are dropped when the `prefix_cache` ablation flag is off.
* Constrained mode (runtime switch): `response_format = {"type": "json_schema", "json_schema": {"schema": <from the live
  registry>}}`; llama.cpp compiles it to a GBNF grammar and masks tokens, so the reply can only be a plan whose calls
  name existing endpoints, their own capabilities and in-range values. Unconstrained mode sends no schema (S10 ablation);
  the executor's validation still protects the hardware.
* One request at a time (the slot is single); at most `max_queued` more wait, beyond that `LlmBusy` (SSE `llm_busy`).
* The reply's `timings` (prompt_n, prompt_ms, cache_n, predicted_n, ...) are returned for measurement."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .sched.grammar import ConstraintBackend, JsonSchemaConstraint
from .sched.prefix import cache_fields

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

    async def complete(self, messages: list[dict], schema: dict | None, max_tokens: int | None = None,
                       repeat_penalty: float | None = None) -> LlmResult: ...


class LlmClient:
    def __init__(self, base_url: str, model: str = "local", constrained: bool = True, max_tokens: int = 256,
                 timeout_s: float = 600.0, max_queued: int = 2, http: httpx.AsyncClient | None = None) -> None:
        self._base, self.model, self.constrained, self.max_tokens = base_url.rstrip("/"), model, constrained, max_tokens
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=3.0))
        self._own_http = http is None
        self.constraint: ConstraintBackend = JsonSchemaConstraint()   # how a constraint is sent (sched.constraint_backend)
        self.prefix_cache, self.id_slot = True, 0                      # cache_prompt + id_slot (sched.prefix_cache)
        self._slot = asyncio.Semaphore(1)
        self._waiting = 0
        self._model_name = ""
        self._max_queued = max_queued

    async def health(self) -> bool:
        try:
            r = await self._http.get(self._base + "/health", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def props_model(self) -> str:
        """The served model's name from llama-server's /props (alias, else the GGUF file name); "" when unavailable.
        Cached once found."""
        if self._model_name:
            return self._model_name
        try:
            r = await self._http.get(self._base + "/props", timeout=3.0)
            j = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return ""
        gen = j.get("default_generation_settings") if isinstance(j, dict) else None
        cand = (j.get("model_alias") if isinstance(j, dict) else None) or j.get("model_path") or (
            gen.get("model") if isinstance(gen, dict) else None)
        name = str(cand or "").replace("\\", "/").rsplit("/", 1)[-1]
        self._model_name = name.removesuffix(".gguf")
        return self._model_name

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    def payload(self, messages: list[dict], schema: dict | None, max_tokens: int | None = None,
                repeat_penalty: float | None = None) -> dict:
        body: dict = {"model": self.model, "messages": messages, "temperature": 0, "top_k": 1, "seed": 0,
                      "max_tokens": max_tokens if max_tokens is not None else self.max_tokens, "stream": False,
                      **cache_fields(self.prefix_cache, self.id_slot)}
        if repeat_penalty is not None:
            body["repeat_penalty"] = repeat_penalty
        if self.constrained and schema is not None:
            self.constraint.apply(body, schema)
        return body

    async def complete(self, messages: list[dict], schema: dict | None, max_tokens: int | None = None,
                       repeat_penalty: float | None = None) -> LlmResult:
        if self._slot.locked() and self._waiting >= self._max_queued:
            raise LlmBusy("language model busy")
        self._waiting += 1
        try:
            await self._slot.acquire()
        finally:
            self._waiting -= 1
        try:
            try:
                r = await self._http.post(self._base + "/v1/chat/completions",
                                          json=self.payload(messages, schema, max_tokens, repeat_penalty))
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

    async def stream_text(self, messages: list[dict], max_tokens: int):
        """(S14) Free text, streamed: yields the content deltas of an unconstrained chat completion (the short natural
        phrasing of a read answer). Same single slot and busy rule as `complete`; closing the generator (turn cancelled)
        closes the HTTP stream, which stops generation."""
        if self._slot.locked() and self._waiting >= self._max_queued:
            raise LlmBusy("language model busy")
        self._waiting += 1
        try:
            await self._slot.acquire()
        finally:
            self._waiting -= 1
        try:
            body = self.payload(messages, None, max_tokens)
            body["stream"] = True
            try:
                async with self._http.stream("POST", self._base + "/v1/chat/completions", json=body) as r:
                    if r.status_code == 503:
                        raise LlmBusy("llama-server busy or loading")
                    if r.status_code >= 400:
                        raise LlmError(f"llama-server HTTP {r.status_code}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            delta = ((json.loads(data).get("choices") or [{}])[0].get("delta") or {}).get("content")
                        except ValueError:
                            continue
                        if delta:
                            yield delta
            except httpx.HTTPError as e:
                raise LlmError(f"llama-server unreachable: {type(e).__name__}") from None
        finally:
            self._slot.release()
