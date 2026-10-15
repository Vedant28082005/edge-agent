"""Backend interfaces and the mock implementations. The app never talks to llama-server, whisper.cpp or ONNX directly:
it sees only these protocols, so tests (and `--mock`) run on a PC with no model.

Contract for implementations: raw model output only (the app sanitises, validates and closes it); raise ApiError for
known failures (`model_error`, `bad_audio`...), anything else is turned into `model_error` by the app with the exception
CLASS NAME logged and nothing else; be cancel-safe (the app cancels on deadline); never write bytes to disk or logs."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from .schema import mock_value


@dataclass
class VlmText:
    text: str
    truncated: bool = False          # generation stopped at max_tokens


@dataclass
class RawDetection:
    label: str
    conf: float
    bbox: tuple[float, float, float, float]   # x, y, w, h in pixels of the submitted image


class VlmBackend(Protocol):
    model_name: str

    async def loaded(self) -> bool: ...
    async def yesno(self, image: bytes, mime: str, question: str, *, attempt: int) -> str: ...
    async def short(self, image: bytes, mime: str, question: str, max_tokens: int) -> VlmText: ...
    async def json(self, image: bytes, mime: str, question: str, json_schema: dict, max_tokens: int, *, attempt: int) -> Any: ...


class SttBackend(Protocol):
    model_name: str

    async def loaded(self) -> bool: ...
    async def transcribe(self, wav16k: bytes) -> str: ...


class DetectBackend(Protocol):
    model_name: str

    async def loaded(self) -> bool: ...
    async def detect(self, image: bytes, width: int, height: int, min_conf: float, max_detections: int) -> list[RawDetection]: ...


@dataclass
class Backends:
    vlm: VlmBackend
    stt: SttBackend
    detect: DetectBackend


# ------------------------------------------------------------------------------------------------------ mocks
class MockVlm:
    """Deterministic. Tests set `.yesno_out`/`.short_out`/`.json_out` (a value, or a callable(attempt)) and `.delay`/`.gate`."""
    model_name = "qwen3-vl-2b-q4"

    def __init__(self):
        self.is_loaded = True
        self.delay = 0.0
        self.gate: asyncio.Event | None = None
        self.yesno_out: Any = None
        self.short_out: Any = None
        self.json_out: Any = None
        self.calls = 0
        self.raise_exc: BaseException | None = None

    async def loaded(self) -> bool:
        return self.is_loaded

    async def _wait(self) -> None:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc

    @staticmethod
    def _val(v: Any, attempt: int = 0) -> Any:
        return v(attempt) if callable(v) else v

    async def yesno(self, image, mime, question, *, attempt):
        await self._wait()
        if self.yesno_out is not None:
            return self._val(self.yesno_out, attempt)
        return "yes" if hashlib.sha256(image).digest()[0] % 2 == 0 else "no"

    async def short(self, image, mime, question, max_tokens):
        await self._wait()
        if self.short_out is not None:
            return self._val(self.short_out)
        return VlmText("A mock scene with a table and a lamp.")

    async def json(self, image, mime, question, json_schema, max_tokens, *, attempt):
        await self._wait()
        if self.json_out is not None:
            return self._val(self.json_out, attempt)
        return mock_value({"properties": _from_json_schema(json_schema)})


def _from_json_schema(js: dict) -> dict:
    """Inverse of schema.to_json_schema just enough for mock_value."""
    out = {}
    for name, p in js["properties"].items():
        spec = dict(p)
        if p["type"] == "array":
            spec = {"type": "array", "maxItems": p["maxItems"], "items": p["items"]}
        out[name] = spec
    return out


class MockStt:
    model_name = "whisper-base.en"

    def __init__(self):
        self.is_loaded = True
        self.delay = 0.0
        self.gate: asyncio.Event | None = None
        self.out: Any = "turn on the kitchen light"
        self.calls = 0

    async def loaded(self) -> bool:
        return self.is_loaded

    async def transcribe(self, wav16k: bytes) -> str:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.out(wav16k) if callable(self.out) else self.out


class MockDetect:
    model_name = "yolov8n"

    def __init__(self):
        self.is_loaded = True
        self.delay = 0.0
        self.out: Any = None
        self.calls = 0

    async def loaded(self) -> bool:
        return self.is_loaded

    async def detect(self, image, width, height, min_conf, max_detections):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.out is not None:
            return self.out
        return [RawDetection("person", 0.62, (width * 0.1, height * 0.1, width * 0.3, height * 0.6)),
                RawDetection("cup", 0.91, (width * 0.5, height * 0.5, width * 0.1, height * 0.1))]


def mock_backends() -> Backends:
    return Backends(MockVlm(), MockStt(), MockDetect())
