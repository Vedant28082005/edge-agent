"""Fake llama-server (no model needed) wired into the service through httpx's ASGI transport."""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision_hub.app import Service, create_app  # noqa: E402
from vision_hub.frames import FolderSource  # noqa: E402
from vision_hub.llama import LlamaClient  # noqa: E402


class FakeLlama:
    """Behaviour knobs: content (str returned as the model message), status, delay (async sleep), healthy."""

    def __init__(self):
        self.content = "yes"
        self.status = 200
        self.delay = 0.0
        self.healthy = True
        self.requests: list[dict] = []

    def app(self) -> Starlette:
        async def chat(request):
            import asyncio
            body = await request.json()
            self.requests.append(body)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.status != 200:
                return JSONResponse({"error": "x"}, status_code=self.status)
            return JSONResponse({"choices": [{"message": {"content": self.content}, "finish_reason": "stop"}]})

        async def health(request):
            return JSONResponse({"status": "ok"}, status_code=200 if self.healthy else 503)

        return Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]),
                                 Route("/health", health)])

    def image_sent(self, i: int = -1) -> Image.Image:
        uri = self.requests[i]["messages"][1]["content"][0]["image_url"]["url"]
        return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))


def png(size=(640, 480), text: str | None = None, color=(200, 200, 200)) -> bytes:
    im = Image.new("RGB", size, color)
    if text:
        ImageDraw.Draw(im).text((10, 10), text, fill=(0, 0, 0))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def fake():
    return FakeLlama()


@pytest.fixture
def frames_dir(tmp_path):
    d = tmp_path / "frames"
    d.mkdir()
    (d / "a.png").write_bytes(png(text="A"))
    (d / "b.png").write_bytes(png(text="B"))
    return d


@pytest.fixture
def service(fake, frames_dir):
    llama = LlamaClient(httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app()), base_url="http://llama"),
                        timeout_s=2.0)
    return Service(llama, allowed_dir=frames_dir, source=FolderSource(frames_dir))


@pytest.fixture
def client(service):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)), base_url="http://svc")


def run(coro):
    import asyncio
    return asyncio.run(coro)


def scene_json(person=True, objs=("chair",)) -> str:
    return json.dumps({"person": person, "objs": list(objs)})
