import hashlib
import io
import json
import logging
import uuid

import httpx
import pytest
from PIL import Image

from vision_node.app import create_app
from vision_node.auth import CallerAuth
from vision_node.backends import mock_backends
from vision_node.config import Config
from vision_node.contract import Contract
from vision_node.media import wav_header
from vision_node.sysinfo import MockSysInfo

SECRET = "s3cret-for-tests-" + "x" * 20


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def contract():
    return Contract()


@pytest.fixture
def verifier(tmp_path):
    p = tmp_path / "agent.sha256"
    p.write_text(hashlib.sha256(SECRET.encode()).hexdigest() + "\n")
    return str(p)


class Env:
    def __init__(self, contract, verifier, **cfg):
        self.cfg = Config(**cfg)
        self.backends = mock_backends()
        self.sys = MockSysInfo()
        self.app = create_app(self.cfg, self.backends, contract=contract, auth=CallerAuth(verifier), sysinfo=self.sys, mock=True)
        self.vision = self.app.state.vision
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")

    async def post(self, path, params, file=None, part="image", secret=SECRET, ctype=None, raw_params=None, extra_parts=(), filename="f.bin"):
        files = {"params": (None, raw_params if raw_params is not None else json.dumps(params), "application/json")}
        if file is not None:
            files[part] = (filename, file, ctype or "application/octet-stream")
        for name, data in extra_parts:
            files[name] = (None, data)
        headers = {"X-Service-Secret": secret} if secret is not None else {}
        return await self.client.post(path, files=files, headers=headers)


@pytest.fixture
def make_env(contract, verifier):
    made = []

    def f(**cfg):
        e = Env(contract, verifier, **cfg)
        made.append(e)
        return e
    yield f


@pytest.fixture
def env(make_env):
    return make_env()


def png(w=64, h=48, color=(10, 200, 30)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def jpeg(w=64, h=48) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 10, 10)).save(b, "JPEG")
    return b.getvalue()


def webp(w=32, h=32) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), (1, 2, 3)).save(b, "WEBP")
    return b.getvalue()


def wav(seconds=1.0, rate=16000) -> bytes:
    n = int(rate * seconds)
    pcm = b"\x00\x01" * n
    if rate == 16000:
        return wav_header(len(pcm)) + pcm
    raise ValueError


@pytest.fixture(autouse=True)
def _quiet():
    logging.getLogger("httpx").setLevel(logging.WARNING)
