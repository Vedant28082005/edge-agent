"""VLM-only node (vlmpi): whisper/yolo disabled by config, and the VLM image is downscaled before llama-server."""
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from conftest import SECRET, png
from vision_node.app import create_app
from vision_node.auth import CallerAuth
from vision_node.backends import Backends, DisabledBackend, mock_backends
from vision_node.config import Config, load_config
from vision_node.real import LlamaVlm
from vision_node.sysinfo import MockSysInfo


def vlm_only_client(contract, verifier, **cfg):
    mock = mock_backends()
    be = Backends(mock.vlm, DisabledBackend("whisper-base.en"), DisabledBackend("yolov8n"))
    app = create_app(Config(whisper_enabled=False, yolo_enabled=False, **cfg), be, contract=contract,
                     auth=CallerAuth(verifier), sysinfo=MockSysInfo(), mock=True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), mock


@pytest.mark.anyio
async def test_health_ok_with_only_the_vlm(contract, verifier):
    client, _ = vlm_only_client(contract, verifier)
    body = (await client.get("/health")).json()
    assert not list(contract.validator("Health").iter_errors(body))
    assert body["ok"] is True and [m["name"] for m in body["models"]] == ["qwen3-vl-2b-q4"]


@pytest.mark.anyio
async def test_health_not_ok_when_the_vlm_is_down_or_memory_low(contract, verifier):
    client, mock = vlm_only_client(contract, verifier)
    mock.vlm.is_loaded = False
    assert (await client.get("/health")).json()["ok"] is False


@pytest.mark.anyio
async def test_disabled_operations_are_503_and_vlm_still_works(contract, verifier):
    client, _ = vlm_only_client(contract, verifier)
    h = {"X-Service-Secret": SECRET}
    files = {"params": (None, json.dumps({"min_conf": 0.3}), "application/json"), "image": ("f", png(), "image/png")}
    r = await client.post("/v1/detect", files=files, headers=h)
    assert r.status_code == 503 and r.json()["error"] == "vision_unavailable"
    files = {"params": (None, json.dumps({"question": "Is it on?"}), "application/json"), "image": ("f", png(), "image/png")}
    assert (await client.post("/v1/vlm/yesno", files=files, headers=h)).status_code == 200


def test_enabled_flags_come_from_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"whisper": {"enabled": False}, "yolo": {"enabled": False}, "limits": {"vlm_max_side": 224}}))
    c = load_config(str(p))
    assert (c.whisper_enabled, c.yolo_enabled, c.vlm_max_side) == (False, False, 224)
    assert Config().whisper_enabled and Config().yolo_enabled and Config().vlm_max_side == 0


@pytest.mark.parametrize("bad", [{"vlm_max_side": 10}, {"vlm_max_side": 5000}, {"vlm_max_side": True},
                                 {"whisper_enabled": "no"}, {"yolo_enabled": 0}])
def test_bad_values_refuse_to_start(bad):
    with pytest.raises(SystemExit):
        Config(**bad).validate(mock=False)


def llama(max_side, seen):
    def h(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "yes"}, "finish_reason": "stop"}]})
    v = LlamaVlm("http://127.0.0.1:8081", max_side=max_side)
    v._http._client = httpx.AsyncClient(transport=httpx.MockTransport(h), base_url="http://127.0.0.1:8081")
    return v


def sent_image(seen):
    uri = seen["messages"][1]["content"][0]["image_url"]["url"]
    head, b64 = uri.split(",", 1)
    return head, Image.open(io.BytesIO(base64.b64decode(b64)))


@pytest.mark.anyio
async def test_vlm_image_is_downscaled_to_224():
    seen = {}
    await llama(224, seen).yesno(png(1920, 1080), "image/png", "Is it on?", attempt=0)
    head, im = sent_image(seen)
    assert head == "data:image/jpeg;base64" and im.size == (224, 126)


@pytest.mark.anyio
async def test_small_image_and_default_are_sent_unchanged():
    small, big = png(100, 50), png(1000, 500)
    for max_side, img in ((224, small), (0, big)):
        seen = {}
        await llama(max_side, seen).yesno(img, "image/png", "q", attempt=0)
        uri = seen["messages"][1]["content"][0]["image_url"]["url"]
        assert uri == "data:image/png;base64," + base64.b64encode(img).decode()
