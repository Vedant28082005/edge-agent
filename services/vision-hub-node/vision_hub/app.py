"""HTTP service, loopback only. One request at a time (429 `busy` otherwise), bounded body, no frame ever stored.

POST /v1/yesno  question + image  -> {"answer": bool, "ms": int}
POST /v1/scene  image             -> {"person": bool, "objs": [<=3 str], "ms": int}
GET  /health
Image input (pick one):
  * raw body with Content-Type image/jpeg|png|webp (yesno: question in the `question` query parameter)
  * JSON body {"path": "<file under the allowed directory>", "question": "..."}
  * JSON body {"frame": true, "question": "..."}  -> next frame of the configured FrameSource
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import imaging, sanitize
from .frames import FrameError, FrameSource, resolve_allowed
from .llama import LlamaClient, VlmError

BODY_TIMEOUT_S = 10.0
MAX_JSON_BYTES = 4096
_STATUS = {"bad_request": 400, "payload_too_large": 413, "bad_image": 422, "busy": 429, "model_error": 500,
           "schema_violation": 502, "vlm_unavailable": 503, "timeout": 504, "no_frame": 503}


class ApiError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _err(code: str) -> JSONResponse:
    headers = {"Retry-After": "5"} if code == "busy" else None
    return JSONResponse({"error": code}, status_code=_STATUS[code], headers=headers)


async def _read_body(request: Request, limit: int) -> bytes:
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > limit:
                raise ApiError("payload_too_large")
        except ValueError:
            raise ApiError("bad_request") from None
    buf = bytearray()

    async def pump() -> None:
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > limit:
                raise ApiError("payload_too_large")

    try:
        await asyncio.wait_for(pump(), BODY_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise ApiError("timeout") from None
    return bytes(buf)


class Service:
    def __init__(self, llama: LlamaClient, allowed_dir: Path | None = None, source: FrameSource | None = None):
        self.llama = llama
        self.allowed_dir = allowed_dir
        self.source = source
        self._lock = asyncio.Lock()
        self.last_ms: int | None = None

    async def _input(self, request: Request) -> tuple[bytes, str]:
        """Returns (encoded image bytes, raw question)."""
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype in ("image/jpeg", "image/png", "image/webp"):
            data = await _read_body(request, imaging.MAX_BYTES)
            return data, request.query_params.get("question", "")
        if ctype != "application/json":
            raise ApiError("bad_request")
        raw = await _read_body(request, MAX_JSON_BYTES)
        try:
            obj = json.loads(raw)
        except ValueError:
            raise ApiError("bad_request") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("question", ""), str):
            raise ApiError("bad_request")
        q = obj.get("question", "")
        try:
            if isinstance(obj.get("path"), str) and self.allowed_dir is not None:
                p = resolve_allowed(self.allowed_dir, obj["path"])
                return await asyncio.to_thread(p.read_bytes), q
            if obj.get("frame") is True and self.source is not None:
                return await self.source.grab(), q
        except (FrameError, OSError):
            raise ApiError("bad_request") from None
        raise ApiError("bad_request")

    async def _run(self, request: Request, op: str) -> JSONResponse:
        # Admission before reading anything: a busy node never buffers a second upload.
        if self._lock.locked():
            return _err("busy")
        async with self._lock:
            t0 = time.monotonic()
            try:
                data, question = await self._input(request)
                try:
                    jpeg = await asyncio.to_thread(imaging.prepare, data)
                except imaging.BadImage:
                    raise ApiError("bad_image") from None
                del data
                if op == "yesno":
                    q = sanitize.clean_question(question)
                    if not q:
                        raise ApiError("bad_request")
                    result: dict = {"answer": await asyncio.wait_for(self.llama.yesno(jpeg, q), self.llama.timeout_s)}
                else:
                    result = await asyncio.wait_for(self.llama.scene(jpeg), self.llama.timeout_s)
            except (ApiError, VlmError) as exc:
                return _err(exc.code)
            except asyncio.TimeoutError:
                return _err("timeout")
            ms = int((time.monotonic() - t0) * 1000)
            self.last_ms = ms
            return JSONResponse({**result, "ms": ms})

    async def yesno(self, request: Request) -> JSONResponse:
        return await self._run(request, "yesno")

    async def scene(self, request: Request) -> JSONResponse:
        return await self._run(request, "scene")

    async def health(self, request: Request) -> JSONResponse:
        up = await self.llama.alive()
        return JSONResponse({"status": "ok" if up else "degraded", "vlm": up, "busy": self._lock.locked(),
                             "source": self.source.describe() if self.source else None,
                             "last_ms": self.last_ms})


def create_app(service: Service) -> Starlette:
    return Starlette(routes=[
        Route("/v1/yesno", service.yesno, methods=["POST"]),
        Route("/v1/scene", service.scene, methods=["POST"]),
        Route("/health", service.health, methods=["GET"]),
    ])
