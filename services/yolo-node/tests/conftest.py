from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from PIL import Image
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yolo_node.app import Node, create_app          # noqa: E402
from yolo_node.auth import ApiAuth                  # noqa: E402
from yolo_node.backends.mock import MockBackend     # noqa: E402
from yolo_node.config import Config                 # noqa: E402
from yolo_node.sources import MockCamera            # noqa: E402

CONTRACT = ROOT.parents[1] / "contracts" / "openapi" / "yolo-node-internal.yaml"
SAMPLES = ROOT.parents[1] / "contracts" / "samples"
SECRET = "test-secret-not-a-real-one-0123456789"
H = {"X-Service-Secret": SECRET}
BOUNDARY = "xBOUNDARYx123"

_DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def image_bytes(fmt="JPEG", size=(64, 48), color=(200, 30, 30), **save_kw) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, fmt, **save_kw)
    return b.getvalue()


def multipart(params, image, filename="photo.jpg", ctype="image/jpeg", extra=b"") -> tuple:
    if isinstance(params, (dict, list)):
        params = json.dumps(params).encode()
    body = (b"--" + BOUNDARY.encode() + b'\r\nContent-Disposition: form-data; name="params"\r\n'
            b"Content-Type: application/json\r\n\r\n" + params + b"\r\n"
            b"--" + BOUNDARY.encode() + b'\r\nContent-Disposition: form-data; name="image"; filename="'
            + filename.encode() + b'"\r\nContent-Type: ' + ctype.encode() + b"\r\n\r\n" + image + b"\r\n"
            + extra + b"--" + BOUNDARY.encode() + b"--\r\n")
    return body, {"Content-Type": "multipart/form-data; boundary=" + BOUNDARY}


class Harness:
    def __init__(self, tmp_path, backend=None, sources=None, auth=True):
        self.backend = backend or MockBackend()
        self.cam = MockCamera("cam0")
        srcs = {"cam0": self.cam} if sources is None else sources
        self.auth = ApiAuth.from_secret(SECRET, str(tmp_path / "agent.sha256")) if auth else None
        self.node = Node(Config(), self.backend, srcs, self.auth)
        self.app = create_app(self.node)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://yolo")

    async def detect(self, params=None, image=None, headers=H, **kw):
        body, h = multipart({} if params is None else params, image if image is not None else image_bytes(), **kw)
        return await self.client.post("/v1/detect", content=body, headers={**h, **(headers or {})})

    async def snap(self, body, headers=H):
        return await self.client.post("/v1/detect/snapshot", json=body, headers=headers)


@pytest.fixture
async def hx(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.client.aclose()
    h.node.sched.close()


@pytest.fixture
def make_harness(tmp_path):
    made = []

    def _make(**kw):
        h = Harness(tmp_path, **kw)
        made.append(h)
        return h
    yield _make
    for h in made:
        h.node.sched.close()


# ---- contract conformance ------------------------------------------------------------------------------------------
def _validator(ref: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": "#/components/schemas/" + ref, "components": _DOC["components"]})


def conforms(path: str, method: str, resp: httpx.Response) -> None:
    """Status is declared for the operation, JSON body validates against the declared schema, headers per contract."""
    op = _DOC["paths"][path][method.lower()]
    declared = op["responses"]
    assert str(resp.status_code) in declared, "status %s not declared for %s %s" % (resp.status_code, method, path)
    spec = declared[str(resp.status_code)]
    if "$ref" in spec:
        spec = _DOC["components"]["responses"][spec["$ref"].rsplit("/", 1)[1]]
    schema = spec["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1]
    errors = list(_validator(schema).iter_errors(resp.json()))
    assert not errors, errors[0].message
    if resp.status_code == 429:
        assert 1 <= int(resp.headers["Retry-After"]) <= 60
    if resp.status_code >= 400:
        assert resp.json()["error"] in _DOC["components"]["schemas"]["Error"]["properties"]["error"]["enum"]


def schema_ok(name: str, data) -> bool:
    return not list(_validator(name).iter_errors(data))
