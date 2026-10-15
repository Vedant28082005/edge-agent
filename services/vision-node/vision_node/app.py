"""vision-node HTTP app (contract: contracts/openapi/vision-node-internal.yaml).

Request path: auth -> admission (bounded, BEFORE the body is read) -> body into RAM with a hard cap -> multipart ->
params validated against the contract schema -> cheap media checks -> (answer cache) -> scheduler slot -> backend
-> sanitise/validate -> closed response, re-validated against the contract.

Logging: operation, status, latency, queue depth. Never a byte of image/audio, question, answer, transcript, filename or
exception text (only the exception class name)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import media, multipart
from .auth import CallerAuth
from .backends import Backends
from .cache import AnswerCache
from .config import Config
from .contract import Contract
from .errors import ApiError
from .sanitize import clean_question, filter_transcript, safe_line
from .scheduler import PRIO_OTHER, PRIO_STT, Scheduler
from .schema import check_bounded, to_json_schema, validate_output
from .sysinfo import ProcSysInfo

log = logging.getLogger("vision_node")

_LABEL_OK = re.compile(r"[A-Za-z0-9 _-]{1,32}")
BODY_READ_S = 20.0
_YESNO = {"yes", "no", "unsure"}


def _no_const(_c: str) -> Any:
    raise ValueError("non-finite number")


class VisionApp:
    def __init__(self, cfg: Config, backends: Backends, contract: Contract, auth: CallerAuth | None,
                 sysinfo=None, mock: bool = False, clock: Callable[[], float] = time.monotonic):
        self.cfg, self.b, self.contract, self.auth, self.mock = cfg, backends, contract, auth, mock
        self.sys = sysinfo or ProcSysInfo()
        self.sched = Scheduler(max_waiting=cfg.max_waiting)
        self.cache = AnswerCache(ttl_s=cfg.cache_ttl_s)
        self.clock = clock

    # ------------------------------------------------------------------------------------------------ plumbing
    def starlette(self) -> Starlette:
        post = ["POST"]
        routes = [
            Route("/health", self.health, methods=["GET"]),
            Route("/v1/vlm/yesno", self._op("vlm_yesno", self.yesno), methods=post),
            Route("/v1/vlm/short", self._op("vlm_short", self.short), methods=post),
            Route("/v1/vlm/json", self._op("vlm_json", self.vlm_json), methods=post),
            Route("/v1/stt", self._op("stt", self.stt), methods=post),
            Route("/v1/detect", self._op("detect", self.detect), methods=post),
            Route("/{path:path}", self._unknown, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
        ]
        return Starlette(routes=routes)

    def _authed(self, request: Request) -> None:
        if self.auth is not None and not self.auth.check(request.headers.get("x-service-secret")):
            raise ApiError("unauthorized")

    @staticmethod
    def _reply(status: int, body: dict, headers: dict | None = None) -> JSONResponse:
        h = {"Cache-Control": "no-store"}
        if headers:
            h.update(headers)
        return JSONResponse(body, status_code=status, headers=h)

    def _error(self, op: str, e: ApiError, t0: float) -> JSONResponse:
        log.info("op=%s status=%d error=%s ms=%d depth=%d", op, e.status, e.code, (self.clock() - t0) * 1000, self.sched.depth)
        hdr = {"Retry-After": str(e.retry_after or 2)} if e.code == "queue_full" else None
        return self._reply(e.status, e.body(), hdr)

    async def _unknown(self, request: Request) -> Response:
        t0 = self.clock()
        try:
            self._authed(request)
        except ApiError as e:
            return self._error("unknown", e, t0)
        return self._reply(404, {"error": "bad_request", "message": "Unknown operation."})

    def _op(self, op: str, fn: Callable[..., Awaitable[tuple[dict, float]]]):
        async def endpoint(request: Request) -> Response:
            t0 = self.clock()
            try:
                self._authed(request)
                self.sched.admit()
            except ApiError as e:
                return self._error(op, e, t0)
            try:
                try:        # a client that trickles its upload must not hold an admitted slot for long
                    body = await asyncio.wait_for(
                        self._read_body(request, media.MAX_AUDIO_BODY if op == "stt" else media.MAX_IMAGE_BODY), BODY_READ_S)
                except asyncio.TimeoutError:
                    raise ApiError("timeout")
                t_last = self.clock()              # latency_ms starts at the last request byte
                result = await fn(request, body, t_last)
                result["latency_ms"] = round(min((self.clock() - t_last) * 1000, 120000.0), 1)
                if not self.contract.valid(_RESULT_SCHEMA[op], result):
                    raise ApiError("model_error")    # conformance backstop: never emit what the contract does not allow
                log.info("op=%s status=200 latency_ms=%d queue_ms=%d depth=%d", op, result["latency_ms"],
                         result["queue_ms"], self.sched.depth)
                return self._reply(200, result)
            except ApiError as e:
                return self._error(op, e, t0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  class name only: exception text may contain request content
                log.error("op=%s status=500 exc=%s", op, type(e).__name__)
                return self._error(op, ApiError("model_error"), t0)
            finally:
                self.sched.leave()
        return endpoint

    async def _read_body(self, request: Request, limit: int) -> bytes:
        if not request.headers.get("content-type", "").lower().lstrip().startswith("multipart/form-data"):
            raise ApiError("unsupported_media_type")
        cl = request.headers.get("content-length")
        if cl is not None:
            if not cl.isdigit():
                raise ApiError("bad_request")
            if int(cl) > limit:
                raise ApiError("payload_too_large")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                raise ApiError("payload_too_large")
        return bytes(buf)

    def _parts(self, request: Request, body: bytes, file_part: str, file_cap: int) -> tuple[dict, bytes]:
        parts = multipart.parse(body, multipart.boundary_of(request.headers.get("content-type")),
                                {"params": media.MAX_PARAMS_BYTES, file_part: file_cap})
        if len(parts) != 2:
            raise ApiError("bad_request")
        try:
            params = json.loads(parts["params"].decode("utf-8"), parse_constant=_no_const)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ApiError("bad_request")
        return params, parts[file_part]

    def _params(self, schema: str, params: Any) -> dict:
        if not isinstance(params, dict) or not self.contract.valid(schema, params):
            raise ApiError("bad_request")
        return params

    def _deadline(self, t_last: float, params: dict) -> float:
        return t_last + min(params.get("timeout_ms", self.cfg.node_cap_ms), self.cfg.node_cap_ms) / 1000.0

    async def _ready(self, backend) -> None:
        if self.sys.mem_available_mb() < self.cfg.min_mem_mb or not await backend.loaded():
            raise ApiError("vision_unavailable")

    async def _run(self, backend, prio: int, deadline: float, work: Callable[[], Awaitable[Any]]) -> tuple[Any, float]:
        """Slot + guards + deadline. Returns (result, queue_ms)."""
        await self._ready(backend)
        queue_s = await self.sched.acquire(prio, deadline)
        try:
            await self._ready(backend)               # memory may have dropped while waiting
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ApiError("timeout")
            try:
                return await asyncio.wait_for(work(), remaining), round(queue_s * 1000, 1)
            except asyncio.TimeoutError:
                raise ApiError("timeout")
        finally:
            self.sched.release()

    # ------------------------------------------------------------------------------------------------ operations
    def _vlm_in(self, request: Request, body: bytes, schema: str) -> tuple[dict, bytes, media.ImageInfo, str]:
        params, image = self._parts(request, body, "image", media.MAX_IMAGE_BYTES)
        params = self._params(schema, params)
        info = media.inspect_image(image)
        question = clean_question(params["question"])
        if not question:
            raise ApiError("bad_request")
        return params, image, info, question

    @staticmethod
    def _echo(result: dict, params: dict) -> dict:
        if "image_ref" in params:
            result["image_ref"] = params["image_ref"]
        return result

    async def yesno(self, request: Request, body: bytes, t_last: float) -> dict:
        params, image, info, question = self._vlm_in(request, body, "VlmYesNoParams")
        key = AnswerCache.key("yesno", image, question)
        hit = self.cache.get(key)
        if hit is not None:
            return self._echo({"answer": hit["answer"], "queue_ms": 0.0, "cached": True}, params)

        async def work() -> str:
            for attempt in (0, 1):
                raw = await self.b.vlm.yesno(image, info.mime, question, attempt=attempt)
                ans = raw.strip().lower().rstrip(".!") if isinstance(raw, str) else ""
                if ans in _YESNO:
                    return ans
            raise ApiError("schema_violation")

        ans, queue_ms = await self._run(self.b.vlm, PRIO_OTHER, self._deadline(t_last, params), work)
        self.cache.put(key, {"answer": ans})
        return self._echo({"answer": ans, "queue_ms": queue_ms, "cached": False}, params)

    async def short(self, request: Request, body: bytes, t_last: float) -> dict:
        params, image, info, question = self._vlm_in(request, body, "VlmShortParams")
        max_tokens = params.get("max_tokens", 48)
        key = AnswerCache.key("short", image, question, max_tokens)
        hit = self.cache.get(key)
        if hit is not None:
            return self._echo({**hit, "queue_ms": 0.0, "cached": True}, params)

        async def work() -> dict:
            raw = await self.b.vlm.short(image, info.mime, question, max_tokens)
            text, cut = safe_line(raw.text if isinstance(raw.text, str) else "", 240)
            return {"text": text, "truncated": bool(raw.truncated) or cut}

        out, queue_ms = await self._run(self.b.vlm, PRIO_OTHER, self._deadline(t_last, params), work)
        self.cache.put(key, out)
        return self._echo({**out, "queue_ms": queue_ms, "cached": False}, params)

    async def vlm_json(self, request: Request, body: bytes, t_last: float) -> dict:
        params, image, info, question = self._vlm_in(request, body, "VlmJsonParams")
        schema = params["schema"]
        check_bounded(schema)
        js = to_json_schema(schema)
        key = AnswerCache.key("json", image, question, [schema, params["max_tokens"]])
        hit = self.cache.get(key)
        if hit is not None:
            return self._echo({"data": hit, "queue_ms": 0.0, "cached": True}, params)

        async def work() -> dict:
            for attempt in (0, 1):             # one retry, then 502
                try:
                    raw = await self.b.vlm.json(image, info.mime, question, js, params["max_tokens"], attempt=attempt)
                    return validate_output(schema, raw)
                except ValueError:
                    continue
            raise ApiError("schema_violation")

        data, queue_ms = await self._run(self.b.vlm, PRIO_OTHER, self._deadline(t_last, params), work)
        self.cache.put(key, data)
        return self._echo({"data": data, "queue_ms": queue_ms, "cached": False}, params)

    async def stt(self, request: Request, body: bytes, t_last: float) -> dict:
        params, audio = self._parts(request, body, "audio", media.MAX_AUDIO_BYTES)
        params = self._params("SttParams", params)
        kind = media.sniff_audio(audio)

        async def work() -> dict:
            wav, ms = await media.to_wav16k(audio, kind, self.cfg.ffmpeg)
            raw = await self.b.stt.transcribe(wav)
            text, filtered = filter_transcript(raw if isinstance(raw, str) else "", 1000)
            return {"text": text, "duration_ms": ms, "filtered": filtered}

        out, queue_ms = await self._run(self.b.stt, PRIO_STT, self._deadline(t_last, params), work)
        return {**out, "queue_ms": queue_ms}

    async def detect(self, request: Request, body: bytes, t_last: float) -> dict:
        params, image = self._parts(request, body, "image", media.MAX_IMAGE_BYTES)
        params = self._params("DetectParams", params)
        info = media.inspect_image(image)
        min_conf, max_det = params.get("min_conf", 0.25), params.get("max_detections", 50)

        async def work() -> list:
            return await self.b.detect.detect(image, info.width, info.height, min_conf, max_det)

        raw, queue_ms = await self._run(self.b.detect, PRIO_OTHER, self._deadline(t_last, params), work)
        dets = []
        for d in raw:
            try:
                x, y, w, h = (float(v) for v in d.bbox)
                conf = float(d.conf)
            except (TypeError, ValueError):
                continue
            vals = (x, y, w, h, conf)
            if not all(v == v and abs(v) != float("inf") for v in vals) or conf < min_conf or conf > 1:
                continue
            if not isinstance(d.label, str) or not _LABEL_OK.fullmatch(d.label):
                continue
            x, y = min(max(x, 0.0), info.width), min(max(y, 0.0), info.height)
            w, h = min(max(w, 0.0), info.width - x), min(max(h, 0.0), info.height - y)
            dets.append({"label": d.label, "conf": round(conf, 4), "bbox": [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]})
        dets.sort(key=lambda d: -d["conf"])
        return self._echo({"detections": dets[:max_det], "image_width": info.width, "image_height": info.height,
                           "model": self.b.detect.model_name, "queue_ms": queue_ms}, params)

    # ------------------------------------------------------------------------------------------------ health
    async def health(self, request: Request) -> Response:
        backends = (self.b.vlm, self.b.stt, self.b.detect)
        loaded = await asyncio.gather(*(b.loaded() for b in backends), return_exceptions=True)
        loaded = [x is True for x in loaded]
        mem = self.sys.mem_available_mb()
        body: dict = {
            "ok": all(loaded) and mem >= self.cfg.min_mem_mb,
            "service": "vision-node", "node": self.cfg.node,
            "contract_version": self.contract.version,
            "mem_available_mb": mem, "queue_depth": min(self.sched.depth, 4), "busy": self.sched.busy,
            "temp_c": self.sys.temp_c(),
            "models": [{"name": b.model_name, "loaded": ok} for b, ok in zip(backends, loaded)],
        }
        if self.mock:
            body["mock"] = True
        if not self.contract.valid("Health", body):
            return self._reply(500, ApiError("model_error").body())
        return self._reply(200, body)


_RESULT_SCHEMA = {"vlm_yesno": "VlmYesNoResult", "vlm_short": "VlmShortResult", "vlm_json": "VlmJsonResult",
                  "stt": "SttResult", "detect": "DetectResult"}


def create_app(cfg: Config, backends: Backends, *, contract: Contract | None = None, auth: CallerAuth | None,
               sysinfo=None, mock: bool = False) -> Starlette:
    va = VisionApp(cfg, backends, contract or Contract(cfg.contracts_dir), auth, sysinfo, mock)
    app = va.starlette()
    app.state.vision = va
    return app
