"""Responses and samples against contracts/openapi/vision-node-internal.yaml (contract file = source of truth)."""
import json
from pathlib import Path

import pytest

from conftest import jpeg, png, wav, webp

SAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "samples"

# sample dir -> (path, request-schema, file part, file builder)
OPS = {
    "vision-yesno-params": ("/v1/vlm/yesno", "image", png),
    "vision-short-params": ("/v1/vlm/short", "image", jpeg),
    "vision-json-params": ("/v1/vlm/json", "image", webp),
    "detect-params": ("/v1/detect", "image", png),
}


def sample_files(dirname, kind):
    return sorted((SAMPLES / dirname / kind).glob("*.json"))


def params_cases(kind):
    return [pytest.param(d, f, id=f"{d}/{f.name}") for d in OPS for f in sample_files(d, kind)]


@pytest.mark.anyio
@pytest.mark.parametrize("dirname,f", params_cases("valid"))
async def test_valid_param_samples_are_accepted_and_result_conforms(env, contract, dirname, f):
    path, part, build = OPS[dirname]
    r = await env.post(path, json.loads(f.read_text()), build(), part)
    assert r.status_code == 200, r.text
    result = {"/v1/vlm/yesno": "VlmYesNoResult", "/v1/vlm/short": "VlmShortResult",
              "/v1/vlm/json": "VlmJsonResult", "/v1/detect": "DetectResult"}[path]
    errs = list(contract.validator(result).iter_errors(r.json()))
    assert not errs, errs


@pytest.mark.anyio
@pytest.mark.parametrize("dirname,f", params_cases("invalid"))
async def test_invalid_param_samples_are_400(env, contract, dirname, f):
    path, part, build = OPS[dirname]
    r = await env.post(path, json.loads(f.read_text()), build(), part)
    assert r.status_code == 400
    assert r.json() == {"error": "bad_request", "message": "Invalid request."}


@pytest.mark.anyio
async def test_stt_conforms_and_params_may_be_empty(env, contract):
    r = await env.post("/v1/stt", {}, wav(1.5), "audio")
    assert r.status_code == 200
    body = r.json()
    assert not list(contract.validator("SttResult").iter_errors(body))
    assert body["duration_ms"] == 1500 and body["filtered"] is False and body["text"] == "turn on the kitchen light"
    assert (await env.post("/v1/stt", {"timeout_ms": 5000}, wav(), "audio")).status_code == 200
    assert (await env.post("/v1/stt", {"timeout_ms": 5}, wav(), "audio")).status_code == 400


@pytest.mark.anyio
async def test_health_open_and_conforms(env, contract):
    r = await env.client.get("/health")          # no secret
    assert r.status_code == 200
    body = r.json()
    assert not list(contract.validator("Health").iter_errors(body))
    assert body["contract_version"] == contract.version and body["service"] == "vision-node" and body["mock"] is True
    assert {m["name"] for m in body["models"]} == {"qwen3-vl-2b-q4", "whisper-base.en", "yolov8n"}
    assert body["ok"] is True and body["busy"] is False and body["queue_depth"] == 0


@pytest.mark.anyio
async def test_health_not_ok_when_model_missing_or_memory_low(env):
    env.backends.stt.is_loaded = False
    h = (await env.client.get("/health")).json()
    assert h["ok"] is False and [m["loaded"] for m in h["models"]] == [True, False, True]
    env.backends.stt.is_loaded = True
    env.sys.mem = 399.0
    assert (await env.client.get("/health")).json()["ok"] is False


@pytest.mark.anyio
async def test_image_ref_is_echoed_and_filenames_are_irrelevant(env):
    r = await env.post("/v1/vlm/yesno", {"question": "Is it on?", "image_ref": "ref_1-A"}, png(), filename="../../etc/passwd")
    assert r.status_code == 200 and r.json()["image_ref"] == "ref_1-A"
    r = await env.post("/v1/vlm/yesno", {"question": "Is it on?", "image_ref": "ref\n"}, png())
    assert r.status_code == 400          # trailing newline must not slip through `$`


@pytest.mark.anyio
async def test_field_name_with_trailing_newline_rejected(env):
    schema = {"type": "object", "properties": {"lights\n": {"type": "boolean"}}}
    r = await env.post("/v1/vlm/json", {"question": "q", "schema": schema, "max_tokens": 32, "timeout_ms": 5000}, png())
    assert r.status_code == 400


@pytest.mark.anyio
async def test_schema_semantics(env):
    base = {"question": "q", "max_tokens": 32, "timeout_ms": 5000}
    bad_range = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 5, "maximum": 1}}}
    bad_req = {"type": "object", "properties": {"a": {"type": "boolean"}}, "required": ["nope"]}
    for schema in (bad_range, bad_req):
        assert (await env.post("/v1/vlm/json", {**base, "schema": schema}, png())).status_code == 400


@pytest.mark.anyio
async def test_json_op_returns_schema_valid_data(env):
    schema = json.loads((SAMPLES / "vision-json-params/valid/basic.json").read_text())["schema"]
    r = await env.post("/v1/vlm/json", {"question": "q", "schema": schema, "max_tokens": 64, "timeout_ms": 5000}, png())
    assert r.status_code == 200
    assert set(r.json()["data"]) <= set(schema["properties"])


@pytest.mark.anyio
async def test_detect_shape(env):
    r = await env.post("/v1/detect", {"min_conf": 0.5}, png(100, 80))
    body = r.json()
    assert body["image_width"] == 100 and body["image_height"] == 80 and body["model"] == "yolov8n"
    confs = [d["conf"] for d in body["detections"]]
    assert confs == sorted(confs, reverse=True) and len(confs) == 2
    r = await env.post("/v1/detect", {"min_conf": 0.8, "max_detections": 1}, png(100, 80))
    assert [d["label"] for d in r.json()["detections"]] == ["cup"]


@pytest.mark.anyio
async def test_error_bodies_are_fixed_text_and_never_echo(env, contract):
    marker = "ECHO-MARKER-12345"
    cases = [
        await env.post("/v1/vlm/yesno", {"question": marker, "bogus": marker}, png()),
        await env.post("/v1/vlm/yesno", {"question": marker}, b"GIF89a" + marker.encode(), filename=marker),
        await env.post("/v1/vlm/yesno", {"question": marker}, b"\x89PNG\r\n\x1a\n" + marker.encode()),
        await env.post("/v1/vlm/yesno", None, png(), raw_params="{" + marker),
    ]
    for r in cases:
        assert marker not in r.text
        assert not list(contract.validator("Error").iter_errors(r.json()))
    assert [r.status_code for r in cases] == [400, 415, 422, 400]
