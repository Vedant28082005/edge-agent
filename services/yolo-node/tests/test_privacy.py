"""Image bytes are never persisted and never logged."""
import base64
import builtins
import logging
import os
import tempfile

from PIL import Image
from PIL.PngImagePlugin import PngInfo
import io

from conftest import H, SECRET, multipart
from yolo_node.backends.mock import MockBackend

MARK = "SECRETMARKER-7f3a9c"


def marked_png() -> bytes:
    info = PngInfo()
    info.add_text("comment", MARK)
    b = io.BytesIO()
    Image.new("RGB", (32, 32), (9, 9, 9)).save(b, "PNG", pnginfo=info)
    assert MARK.encode() in b.getvalue()
    return b.getvalue()


async def test_no_bytes_filenames_params_or_secret_in_logs(hx, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    png = marked_png()
    fn = "private_family_photo_xyz.png"
    r = await hx.detect({"image_ref": "refsecret42"}, png, filename=fn, ctype="image/png")
    assert r.status_code == 200
    r = await hx.detect({}, MARK.encode() * 10, filename=fn)                       # 415 path
    assert r.status_code == 415
    r = await hx.detect({}, b"\xff\xd8\xff" + MARK.encode() * 5, filename=fn)         # 422 path
    assert r.status_code == 422
    r = await hx.detect(b"{" + MARK.encode(), png)                                  # 400 path
    assert r.status_code == 400
    await hx.snap({"source_id": MARK[:10]})
    r = await hx.detect({}, png, headers={"X-Service-Secret": "bad-" + SECRET})     # 401 path
    assert r.status_code == 401
    hx.backend.fail = True
    assert (await hx.detect({}, png, filename=fn)).status_code == 500                # model_error path

    out = capsys.readouterr()
    blob = caplog.text + out.out + out.err
    assert "op=detect" in blob                                    # something was logged...
    for needle in (MARK, base64.b64encode(MARK.encode()).decode(), fn, "refsecret42", SECRET, "bad-" + SECRET,
                   base64.b64encode(png[:30]).decode(), repr(png[:12])):
        assert needle not in blob, needle


async def test_unhandled_exception_logs_type_only(make_harness, caplog):
    caplog.set_level(logging.DEBUG)
    b = MockBackend()

    def boom(*a, **k):
        raise ValueError("payload " + MARK)
    b.infer = boom
    h = make_harness(backend=b)
    h.node._detect = boom
    r = await h.detect({}, marked_png())
    assert r.status_code == 500 and MARK not in r.text and MARK not in caplog.text


async def test_nothing_written_to_disk(hx, monkeypatch):
    tmp = tempfile.gettempdir()
    before = set(os.listdir(tmp))
    real_open = builtins.open
    writes = []

    def guard(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            writes.append(file)
        return real_open(file, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", guard)
    png = marked_png()
    assert (await hx.detect({}, png)).status_code == 200
    assert (await hx.snap({"source_id": "cam0"})).status_code == 200
    assert (await hx.detect({}, b"not an image")).status_code == 415
    assert writes == []
    assert set(os.listdir(tmp)) == before
