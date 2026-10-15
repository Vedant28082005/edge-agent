"""ASGI application implementing contracts/openapi/yolo-node-internal.yaml.

Privacy rule: image bytes live in request-scoped variables only. Nothing here writes them to disk, and the log lines
carry operation, status, latency and queue depth, never bytes, filenames, params, labels or secrets."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import CONTRACT_VERSION, SERVICE, multipart, sysinfo
from .auth import ApiAuth, AuthMiddleware
from .backends import Backend
from .config import Config
from .errors import BackendError, NodeError
from .imaging import MAX_IMAGE_BYTES, MAX_PIXELS, MAX_SIDE, check_header, decode_rgb
from .scheduler import Scheduler
from .sources import Source

log = logging.getLogger("yolo_node")

MAX_PARAMS_BYTES = 16 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES + MAX_PARAMS_BYTES + 4096      # multipart framing overhead
MAX_JSON_BYTES = 4096
DEFAULT_TIMEOUT_MS = 25000
SNAPSHOT_MIN_INTERVAL_S = 1.0
LABEL_RE = re.compile(r"^[A-Za-z0-9 _-]{1,32}$")
REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ---- request validation ------------------------------------------------------------------------------------------
def _num(v: Any, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise NodeError("bad_request")
    return float(v)


def _int(v: Any, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise NodeError("bad_request")
    return v


def _common(d: Dict[str, Any], allowed: set) -> Dict[str, Any]:
    if not isinstance(d, dict) or set(d) - allowed:
        raise NodeError("bad_request")
    out: Dict[str, Any] = {
        "min_conf": _num(d["min_conf"], 0.05, 1) if "min_conf" in d else 0.25,
        "max_detections": _int(d["max_detections"], 1, 50) if "max_detections" in d else 50,
        "timeout_ms": _int(d["timeout_ms"], 1000, 25000) if "timeout_ms" in d else DEFAULT_TIMEOUT_MS,
    }
    return out


def parse_detect_params(raw: bytes) -> Dict[str, Any]:
    if len(raw) > MAX_PARAMS_BYTES:
        raise NodeError("payload_too_large")
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NodeError("bad_request")
    out = _common(d, {"min_conf", "max_detections", "image_ref", "timeout_ms"})
    if "image_ref" in d:
        ref = d["image_ref"]
        if not isinstance(ref, str) or not REF_RE.match(ref):
            raise NodeError("bad_request")
        out["image_ref"] = ref
    return out


def parse_snapshot_params(raw: bytes) -> Dict[str, Any]:
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NodeError("bad_request")
    out = _common(d, {"source_id", "min_conf", "max_detections", "timeout_ms"})
    sid = d.get("source_id")
    if not isinstance(sid, str) or not SOURCE_RE.match(sid):
        raise NodeError("bad_request")
    out["source_id"] = sid
    return out


async def read_body(request: Request, limit: int) -> bytes:
    cl = request.headers.get("content-length")
    if cl is not None:
        if not cl.isdigit():
            raise NodeError("bad_request")
        if int(cl) > limit:
            raise NodeError("payload_too_large")
    chunks: List[bytes] = []
    total = 0
    async for c in request.stream():
        total += len(c)
        if total > limit:
            raise NodeError("payload_too_large")
        chunks.append(c)
    return b"".join(chunks)


# ---- response construction (closed schemas: only these keys, clamped and sanitized) ---------------------------------
def clean_detections(raw, max_det: int, allow: Optional[set]) -> List[Dict[str, Any]]:
    out = []
    for label, conf, bbox in raw:
        if not isinstance(label, str) or not LABEL_RE.match(label) or (allow is not None and label not in allow):
            continue
        if not (isinstance(conf, (int, float)) and math.isfinite(conf)) or len(bbox) != 4 \
                or not all(math.isfinite(v) for v in bbox):
            continue
        out.append({"label": label, "conf": round(min(1.0, max(0.0, float(conf))), 4),
                    "bbox": [round(max(0.0, float(v)), 1) for v in bbox]})
    out.sort(key=lambda d: -d["conf"])
    return out[:max_det]


def _ms(v: float) -> float:
    return round(min(120000.0, max(0.0, v)), 1)


class Node:
    """Everything the handlers need; built by create_app / __main__."""

    def __init__(self, cfg: Config, backend: Backend, sources: Dict[str, Source], auth: Optional[ApiAuth],
                 scheduler: Optional[Scheduler] = None) -> None:
        self.cfg, self.backend, self.sources, self.auth = cfg, backend, sources, auth
        self.sched = scheduler or Scheduler(8)
        self.allow = set(cfg.labels_allow) if cfg.labels_allow is not None else None
        self._snap_last: Dict[str, float] = {}

    def load_backend(self) -> None:
        try:
            self.backend.load()
        except Exception as e:  # noqa: BLE001 - /health reports engine_loaded=false, detect answers 503
            log.error("backend load failed: %s", type(e).__name__)

    # -- detection core (runs in the inference thread) ---------------------------------------------------------
    def _detect(self, rgb, p) -> List[Dict[str, Any]]:
        if not self.backend.engine_loaded:
            raise NodeError("vision_unavailable")
        try:
            raw = self.backend.infer(rgb, p["min_conf"], p["max_detections"])
        except BackendError:
            raise NodeError("model_error")
        return clean_detections(raw, p["max_detections"], self.allow)

    def _result_common(self, dets, w: int, h: int, t0: float, queue_ms: float) -> Dict[str, Any]:
        return {"detections": dets, "image_width": w, "image_height": h, "model": self.backend.model,
                "latency_ms": _ms((time.monotonic() - t0) * 1000.0), "queue_ms": _ms(queue_ms)}


def create_app(node: Node):
    @contextlib.asynccontextmanager
    async def lifespan(app):
        await asyncio.get_running_loop().run_in_executor(None, node.load_backend)
        yield
        node.sched.close()
        node.backend.close()
        for s in node.sources.values():
            s.close()

    def respond(op: str, t0: float, status: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None):
        log.info("op=%s status=%d ms=%.0f queue=%d", op, status, (time.monotonic() - t0) * 1000.0, node.sched.depth)
        return JSONResponse(body, status_code=status, headers=headers)

    def fail(op: str, t0: float, e: NodeError):
        hdr = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        return respond(op, t0, e.status, e.body(), hdr)

    async def health(request: Request):
        sources_online = sum(1 for s in node.sources.values() if s.online())
        body: Dict[str, Any] = {
            "ok": bool(node.backend.engine_loaded), "service": SERVICE, "node": node.cfg.node,
            "contract_version": CONTRACT_VERSION, "mem_available_mb": sysinfo.mem_available_mb(),
            "queue_depth": min(node.sched.depth, 8), "busy": node.sched.busy, "temp_c": sysinfo.temp_c(),
            "engine_loaded": bool(node.backend.engine_loaded), "sources_online": min(sources_online, 8),
        }
        if node.backend.mock:
            body["mock"] = True
        return JSONResponse(body)

    async def detect(request: Request):
        t0 = time.monotonic()
        try:
            parts = multipart.parse(await read_body(request, MAX_REQUEST_BYTES),
                                    request.headers.get("content-type", ""), ("params", "image"))
            t0 = time.monotonic()                                   # last request byte received
            if "params" not in parts or "image" not in parts:
                raise NodeError("bad_request")
            p = parse_detect_params(parts["params"][1])
            data = parts["image"][1]
            w, h = check_header(data)                               # 413 / 415 (magic bytes) / 422
            if not node.backend.engine_loaded:
                raise NodeError("vision_unavailable")
            deadline = t0 + p["timeout_ms"] / 1000.0

            def job(queue_ms: float):
                rgb = decode_rgb(data)
                return node._detect(rgb, p)

            dets, queue_ms = await node.sched.run(job, deadline)
            res = node._result_common(dets, w, h, t0, queue_ms)
            if "image_ref" in p:
                res["image_ref"] = p["image_ref"]
            return respond("detect", t0, 200, res)
        except NodeError as e:
            return fail("detect", t0, e)
        except Exception as e:  # noqa: BLE001 - type only: messages could contain request data
            log.error("op=detect unhandled %s", type(e).__name__)
            return fail("detect", t0, NodeError("model_error"))

    async def snapshot(request: Request):
        t0 = time.monotonic()
        try:
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                raise NodeError("bad_request")          # the contract declares no 415 for this operation
            try:
                raw = await read_body(request, MAX_JSON_BYTES)
            except NodeError:
                raise NodeError("bad_request")          # ... and no 413 either
            t0 = time.monotonic()
            p = parse_snapshot_params(raw)
            src = node.sources.get(p["source_id"])
            if src is None:
                raise NodeError("unknown_source")
            if not src.online():
                raise NodeError("source_offline")
            if not node.backend.engine_loaded:
                raise NodeError("vision_unavailable")
            last = node._snap_last.get(p["source_id"])
            if last is not None and t0 - last < SNAPSHOT_MIN_INTERVAL_S:
                raise NodeError("queue_full", retry_after=1)
            node._snap_last[p["source_id"]] = t0
            deadline = t0 + p["timeout_ms"] / 1000.0

            def job(queue_ms: float):
                rgb, captured = src.grab()
                age_ms = max(0.0, (time.monotonic() - captured) * 1000.0)
                h, w = rgb.shape[:2]
                if w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
                    raise NodeError("model_error")                    # source delivers an out-of-contract frame
                return node._detect(rgb, p), w, h, age_ms

            (dets, w, h, age_ms), queue_ms = await node.sched.run(job, deadline)
            res = node._result_common(dets, w, h, t0, queue_ms)
            res["source_id"] = p["source_id"]
            res["frame_age_ms"] = round(min(60000.0, age_ms), 1)
            return respond("snapshot", t0, 200, res)
        except NodeError as e:
            return fail("snapshot", t0, e)
        except Exception as e:  # noqa: BLE001 - type only: messages could contain request data
            log.error("op=snapshot unhandled %s", type(e).__name__)
            return fail("snapshot", t0, NodeError("model_error"))

    async def sources(request: Request):
        t0 = time.monotonic()
        items = [{"source_id": sid, "type": s.type, "online": bool(s.online())} for sid, s in node.sources.items()]
        return respond("sources", t0, 200, {"sources": items[:8]})

    async def http_error(request: Request, exc: HTTPException):
        code = "bad_request"
        return JSONResponse(NodeError(code).body(), status_code=exc.status_code if exc.status_code in (404, 405) else 400)

    async def server_error(request: Request, exc: Exception):
        log.error("unhandled %s", type(exc).__name__)            # type only: messages could contain request data
        return JSONResponse(NodeError("model_error").body(), status_code=500)

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]),
                Route("/v1/detect", detect, methods=["POST"]),
                Route("/v1/detect/snapshot", snapshot, methods=["POST"]),
                Route("/v1/sources", sources, methods=["GET"])],
        exception_handlers={HTTPException: http_error, Exception: server_error},
        lifespan=lifespan)
    return AuthMiddleware(app, node.auth, on_reject=lambda: log.warning("op=auth status=401"))
