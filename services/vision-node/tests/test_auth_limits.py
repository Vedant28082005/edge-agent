"""Authentication, size/format limits, request-shape errors."""
import hashlib
import io

import pytest
from PIL import Image

from conftest import SECRET, jpeg, png, wav
from vision_node.auth import CallerAuth
from vision_node.media import MAX_IMAGE_BYTES

Q = {"question": "Is the light on?"}
OPS = [("/v1/vlm/yesno", Q, "image"), ("/v1/vlm/short", Q, "image"),
       ("/v1/detect", {}, "image"), ("/v1/stt", {}, "audio")]


def file_for(part):
    return wav() if part == "audio" else png()


# ----------------------------------------------------------------------------------------------------------- auth
@pytest.mark.anyio
@pytest.mark.parametrize("path,params,part", OPS)
async def test_every_operation_requires_the_secret(env, path, params, part):
    for secret in (None, "", "wrong", SECRET + "x", SECRET.upper(), "x" * 5000):
        r = await env.post(path, params, file_for(part), part, secret=secret)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized", secret
    assert (await env.post(path, params, file_for(part), part)).status_code == 200
    assert env.backends.vlm.calls + env.backends.stt.calls + env.backends.detect.calls == 1


@pytest.mark.anyio
async def test_unknown_paths_need_auth_too_and_health_is_the_only_open_one(env):
    assert (await env.client.post("/v1/other")).status_code == 401
    assert (await env.client.get("/")).status_code == 401
    r = await env.client.get("/v1/other", headers={"X-Service-Secret": SECRET})
    assert r.status_code == 404 and r.json()["error"] == "bad_request"
    assert (await env.client.get("/v1/vlm/yesno", headers={"X-Service-Secret": SECRET})).status_code == 404
    assert (await env.client.get("/health")).status_code == 200


@pytest.mark.anyio
async def test_unauthorized_never_reads_or_runs(env):
    r = await env.post("/v1/vlm/yesno", Q, png(), secret="nope")
    assert r.status_code == 401 and env.backends.vlm.calls == 0


def test_verifier_required_and_validated(tmp_path):
    with pytest.raises(SystemExit):
        CallerAuth(str(tmp_path / "missing"))
    bad = tmp_path / "bad"
    bad.write_text("not-a-digest")
    with pytest.raises(SystemExit):
        CallerAuth(str(bad))
    good = tmp_path / "good"
    good.write_text(hashlib.sha256(b"abc").hexdigest().upper() + "\n")
    a = CallerAuth(str(good))
    assert a.check("abc") and not a.check("abd") and not a.check(None)


def test_insecure_no_auth_only_with_mock():
    from vision_node.__main__ import main
    with pytest.raises(SystemExit) as e:
        main(["--insecure-no-auth"])
    assert e.value.code == 2


def test_config_refuses_wildcard_bind_outside_mock():
    from vision_node.config import Config
    for host in ("0.0.0.0", "::", ""):
        with pytest.raises(SystemExit):
            Config(host=host).validate(mock=False)
    Config(host="100.64.0.7").validate(mock=False)
    Config(host="0.0.0.0").validate(mock=True)


# ---------------------------------------------------------------------------------------------------------- limits
@pytest.mark.anyio
async def test_image_part_over_5mib_is_413(env):
    big = b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_IMAGE_BYTES
    assert (await env.post("/v1/vlm/yesno", Q, big)).status_code == 413


@pytest.mark.anyio
async def test_body_over_limit_is_413_by_content_length_before_reading(env):
    body = b"x" * (6 * 1024 * 1024 + 1)
    r = await env.client.post("/v1/vlm/yesno", content=body, headers={
        "X-Service-Secret": SECRET, "Content-Type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413
    r = await env.client.post("/v1/stt", content=b"x" * (11 * 1024 * 1024 + 1), headers={
        "X-Service-Secret": SECRET, "Content-Type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413


@pytest.mark.anyio
async def test_streamed_body_without_content_length_is_capped(env):
    async def gen():
        for _ in range(8):
            yield b"x" * (1024 * 1024)
    r = await env.client.post("/v1/vlm/yesno", content=gen(), headers={
        "X-Service-Secret": SECRET, "Content-Type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413


@pytest.mark.anyio
async def test_params_over_16kib_is_413(env):
    assert (await env.post("/v1/vlm/yesno", None, png(), raw_params='{"question": "' + "a" * 17000 + '"}')).status_code == 413


@pytest.mark.anyio
async def test_audio_limits(env):
    assert (await env.post("/v1/stt", {}, b"RIFF\0\0\0\0WAVE" + b"\0" * (10 * 1024 * 1024), "audio")).status_code == 413
    r = await env.post("/v1/stt", {}, wav(61), "audio")
    assert r.status_code == 422 and r.json()["error"] == "bad_audio"
    assert (await env.post("/v1/stt", {}, wav(60), "audio")).status_code == 200
    assert (await env.post("/v1/stt", {}, b"ID3\x03" + b"\0" * 64, "audio")).status_code == 415
    assert (await env.post("/v1/stt", {}, png(), "audio")).status_code == 415


@pytest.mark.anyio
async def test_audio_formats_other_than_wav_go_through_conversion(env, monkeypatch):
    from vision_node import media

    async def fake(data, kind, ffmpeg):
        return b"\0\0" * 16000 * 2
    monkeypatch.setattr(media, "_ffmpeg_pcm", fake)
    for head in (b"\x1aE\xdf\xa3", b"\0\0\0\x18ftypM4A ", b"\xff\xf1\x50\x80"):
        r = await env.post("/v1/stt", {}, head + b"\0" * 32, "audio")
        assert r.status_code == 200 and r.json()["duration_ms"] == 2000, head
    async def huge(data, kind, ffmpeg):
        return b"\0\0" * 16000 * 61
    monkeypatch.setattr(media, "_ffmpeg_pcm", huge)
    assert (await env.post("/v1/stt", {}, b"\x1aE\xdf\xa3" + b"\0" * 32, "audio")).status_code == 422


@pytest.mark.anyio
async def test_missing_ffmpeg_is_model_error_not_a_crash(make_env):
    env = make_env(ffmpeg="no-such-ffmpeg-binary")
    r = await env.post("/v1/stt", {}, b"\x1aE\xdf\xa3" + b"\0" * 32, "audio")
    assert r.status_code == 500 and r.json()["error"] == "model_error"


@pytest.mark.anyio
async def test_image_types_by_magic_not_declared_type(env):
    for data in (png(), jpeg()):
        assert (await env.post("/v1/vlm/yesno", Q, data, ctype="text/plain")).status_code == 200
    for data in (b"GIF89a" + b"\0" * 20, b"BM" + b"\0" * 20, b"<svg></svg>", b"%PDF-1.4"):
        assert (await env.post("/v1/vlm/yesno", Q, data, ctype="image/png")).status_code == 415


@pytest.mark.anyio
async def test_image_dimension_and_corruption_limits(env):
    def solid(w, h):
        b = io.BytesIO()
        Image.new("1", (w, h)).save(b, "PNG")
        return b.getvalue()
    assert (await env.post("/v1/vlm/yesno", Q, solid(4096, 4096))).status_code == 200
    assert (await env.post("/v1/vlm/yesno", Q, solid(4097, 10))).status_code == 422
    assert (await env.post("/v1/vlm/yesno", Q, solid(10, 4097))).status_code == 422
    r = await env.post("/v1/vlm/yesno", Q, png()[:30])
    assert r.status_code == 422 and r.json()["error"] == "bad_image"
    assert (await env.post("/v1/vlm/yesno", Q, b"\xff\xd8\xff" + b"garbage")).status_code == 422
    # a PNG whose bytes declare a mismatching container (JPEG magic, PNG body) is not accepted
    assert (await env.post("/v1/vlm/yesno", Q, b"\xff\xd8\xff" + png())).status_code == 422
    assert env.backends.vlm.calls == 1


@pytest.mark.anyio
async def test_request_shape_errors(env):
    H = {"X-Service-Secret": SECRET}
    assert (await env.client.post("/v1/vlm/yesno", content=b"{}", headers={**H, "Content-Type": "application/json"})).status_code == 415
    assert (await env.client.post("/v1/vlm/yesno", headers=H)).status_code == 415
    assert (await env.client.post("/v1/vlm/yesno", content=b"x", headers={**H, "Content-Type": "multipart/form-data"})).status_code == 400
    assert (await env.post("/v1/vlm/yesno", Q, None)).status_code == 400                       # no image
    r = await env.client.post("/v1/vlm/yesno", files={"image": ("a", png())}, headers=H)       # no params
    assert r.status_code == 400
    assert (await env.post("/v1/vlm/yesno", Q, png(), extra_parts=[("sneaky", b"1")])).status_code == 400
    r = await env.client.post("/v1/vlm/yesno", headers=H, files=[
        ("params", (None, '{"question":"a"}')), ("image", ("a", png())), ("image", ("b", png()))])
    assert r.status_code == 400
    for raw in ("[]", "null", '"s"', '{"question": NaN}', '{"question": "a", "timeout_ms": Infinity}', ""):
        assert (await env.post("/v1/vlm/yesno", None, png(), raw_params=raw)).status_code == 400, raw
    assert (await env.post("/v1/vlm/yesno", {"question": "x" * 301}, png())).status_code == 400
    assert (await env.post("/v1/vlm/yesno", {"question": "\x00\x01 \n"}, png())).status_code == 400   # empty after cleaning
    assert env.backends.vlm.calls == 0


@pytest.mark.anyio
async def test_vision_unavailable(env):
    env.sys.mem = 399.9
    r = await env.post("/v1/vlm/yesno", Q, png())
    assert r.status_code == 503 and r.json()["error"] == "vision_unavailable" and env.backends.vlm.calls == 0
    env.sys.mem = 400.0
    env.backends.vlm.is_loaded = False
    assert (await env.post("/v1/vlm/yesno", Q, png())).status_code == 503
    assert (await env.post("/v1/stt", {}, wav(), "audio")).status_code == 200      # per-operation: STT model is loaded
