"""No persistence and no content in logs: images, audio, questions, answers, transcripts, filenames, exception text."""
import base64
import io
import logging
import os
import tempfile

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from conftest import wav
from vision_node.backends import RawDetection, VlmText

QM, AM, IM, FM, TM, EM = ("QUESTIONMARK-7f3a9c", "ANSWERMARK-91bcd2", "IMAGEMARK-55aa01",
                          "FILENAMEMARK-d00d", "TRANSCRIPTMARK-ab12", "EXCMARK-cafe99")


def marked_png() -> bytes:
    info = PngInfo()
    info.add_text("c", IM)
    b = io.BytesIO()
    Image.new("RGB", (40, 30), (9, 8, 7)).save(b, "PNG", pnginfo=info)
    return b.getvalue()


def marked_wav() -> bytes:
    return wav(1.0) + IM.encode()


class Forbidden:
    def __init__(self, *a, **k):
        raise AssertionError("temp file created")


@pytest.mark.anyio
async def test_all_operations_leave_no_content_in_logs_and_no_temp_files(env, caplog, capfd, monkeypatch):
    for name in ("mkstemp", "mkdtemp", "NamedTemporaryFile", "SpooledTemporaryFile", "TemporaryFile", "TemporaryDirectory"):
        monkeypatch.setattr(tempfile, name, Forbidden)
    tmp_before = set(os.listdir(tempfile.gettempdir()))
    cwd_before = set(os.listdir("."))
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)

    img = marked_png()
    env.backends.vlm.yesno_out = lambda a: "yes"
    env.backends.vlm.short_out = VlmText(AM)
    env.backends.vlm.json_out = {"a": AM}
    env.backends.stt.out = TM
    env.backends.detect.out = [RawDetection("person", 0.9, (1, 1, 5, 5))]
    schema = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 40}}}
    calls = [
        ("/v1/vlm/yesno", {"question": QM, "image_ref": "r1"}, img, "image"),
        ("/v1/vlm/short", {"question": QM}, img, "image"),
        ("/v1/vlm/json", {"question": QM, "schema": schema, "max_tokens": 32, "timeout_ms": 5000}, img, "image"),
        ("/v1/stt", {}, marked_wav(), "audio"),
        ("/v1/detect", {}, img, "image"),
        ("/v1/vlm/yesno", {"question": QM}, img, "image"),                       # cache hit
        ("/v1/vlm/yesno", {"question": QM, "bad": 1}, img, "image"),            # 400
        ("/v1/vlm/yesno", {"question": QM}, b"GIF89a" + IM.encode(), "image"),  # 415
        ("/v1/vlm/yesno", {"question": QM}, img[:60], "image"),                  # 422
    ]
    for path, params, data, part in calls:
        r = await env.post(path, params, data, part, filename=FM)
        assert r.status_code in (200, 400, 415, 422)

    env.backends.vlm.raise_exc = RuntimeError(f"{QM} {AM} {IM} {EM}")           # exception text must not reach the log
    r = await env.post("/v1/vlm/short", {"question": QM + "x"}, img, filename=FM)
    assert r.status_code == 500
    env.backends.vlm.raise_exc = None
    env.backends.vlm.json_out = {"bad": AM}                                      # 502 path
    assert (await env.post("/v1/vlm/json", {"question": QM + "y", "schema": schema, "max_tokens": 32, "timeout_ms": 5000},
                           img)).status_code == 502
    env.backends.stt.out = lambda w: (_ for _ in ()).throw(ValueError(TM))      # backend crash on STT
    assert (await env.post("/v1/stt", {}, marked_wav(), "audio")).status_code == 500
    assert (await env.post("/v1/stt", {}, marked_wav(), "audio", secret="wrong")).status_code == 401

    log_text = caplog.text + "".join(capfd.readouterr())
    assert "op=vlm_yesno" in log_text and "op=stt" in log_text            # something is logged: operation/status/latency
    img_b64 = base64.b64encode(img).decode()
    for needle in (QM, AM, IM, FM, TM, EM, img_b64[:60], base64.b64encode(marked_wav()).decode()[:60], "GIF89a"):
        assert needle not in log_text, needle
    assert "exc=RuntimeError" in log_text                                 # class name only
    assert set(os.listdir(tempfile.gettempdir())) == tmp_before and set(os.listdir(".")) == cwd_before


@pytest.mark.anyio
async def test_cache_holds_hash_keys_and_answers_only(env):
    img = marked_png()
    env.backends.vlm.short_out = VlmText(AM)
    r1 = await env.post("/v1/vlm/short", {"question": QM}, img)
    assert r1.json()["cached"] is False
    stored = repr(env.vision.cache._d)
    assert QM not in stored and IM not in stored and base64.b64encode(img).decode()[:40] not in stored
    assert all(len(k) == 64 and set(k) <= set("0123456789abcdef") for k in env.vision.cache._d)
    assert AM in stored                                                    # the answer text is what is cached
    r2 = await env.post("/v1/vlm/short", {"question": QM}, img)
    assert r2.json()["cached"] is True and r2.json()["queue_ms"] == 0 and env.backends.vlm.calls == 1
    assert (await env.post("/v1/vlm/short", {"question": QM + " "}, img)).json()["cached"] is True   # normalised question
    assert (await env.post("/v1/vlm/short", {"question": "other"}, img)).json()["cached"] is False
    assert (await env.post("/v1/vlm/short", {"question": QM, "max_tokens": 9}, img)).json()["cached"] is False
    assert (await env.post("/v1/vlm/short", {"question": QM}, marked_png() + b"\0")).json()["cached"] is False


@pytest.mark.anyio
async def test_cache_ttl_30s(env):
    now = [100.0]
    env.vision.cache._clock = lambda: now[0]
    img = marked_png()
    assert (await env.post("/v1/vlm/yesno", {"question": "x"}, img)).json()["cached"] is False
    now[0] += 29
    assert (await env.post("/v1/vlm/yesno", {"question": "x"}, img)).json()["cached"] is True
    now[0] += 2
    assert (await env.post("/v1/vlm/yesno", {"question": "x"}, img)).json()["cached"] is False


@pytest.mark.anyio
async def test_cached_answers_are_not_shared_across_operations_and_errors_are_not_cached(env):
    img = marked_png()
    env.backends.vlm.yesno_out = lambda a: "bad"
    assert (await env.post("/v1/vlm/yesno", {"question": "x"}, img)).status_code == 502
    env.backends.vlm.yesno_out = lambda a: "no"
    r = await env.post("/v1/vlm/yesno", {"question": "x"}, img)
    assert r.status_code == 200 and r.json()["cached"] is False
    assert (await env.post("/v1/vlm/short", {"question": "x"}, img)).json()["cached"] is False
