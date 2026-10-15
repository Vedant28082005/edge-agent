"""Size, format and framing limits."""
import io

from PIL import Image

from conftest import BOUNDARY, H, conforms, image_bytes, multipart

MiB = 1024 * 1024


async def test_image_over_5mib_413(hx):
    big = image_bytes("JPEG")[:2] + b"\xff" + b"\x00" * (5 * MiB + 1)
    r = await hx.detect({}, big)
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
    conforms("/v1/detect", "post", r)
    assert hx.backend.calls == 0


async def test_request_over_cap_413_by_content_length(hx):
    r = await hx.client.post("/v1/detect", content=b"x" * (6 * MiB), headers={
        **H, "Content-Type": "multipart/form-data; boundary=" + BOUNDARY})
    assert r.status_code == 413


async def test_request_over_cap_413_when_chunked(hx):
    async def gen():
        for _ in range(12):
            yield b"x" * (512 * 1024)
    r = await hx.client.post("/v1/detect", content=gen(), headers={
        **H, "Content-Type": "multipart/form-data; boundary=" + BOUNDARY})
    assert r.status_code == 413


async def test_exactly_5mib_image_not_413(hx):
    jpg = image_bytes("JPEG", quality=95)
    pad = jpg + b"\x00" * (5 * MiB - len(jpg))           # trailing junk after EOI: still a valid jpeg
    r = await hx.detect({}, pad)
    assert r.status_code == 200, r.text


async def test_magic_bytes_decide_not_content_type(hx):
    buf = io.BytesIO()
    Image.new("P", (8, 8)).save(buf, "GIF")
    for data in (buf.getvalue(), b"hello world, not an image", b"BM" + b"\x00" * 100, b"", b"%PDF-1.4"):
        r = await hx.detect({}, data, ctype="image/jpeg")        # declared jpeg, actually something else
        assert r.status_code == 415, data[:8]
        assert r.json()["error"] == "unsupported_media_type"
        conforms("/v1/detect", "post", r)
    assert hx.backend.calls == 0


async def test_corrupt_images_422(hx):
    for data in (b"\xff\xd8\xff" + b"garbage" * 20, b"\x89PNG\r\n\x1a\n" + b"x" * 40, b"RIFF\x00\x00\x00\x00WEBPjunk"):
        r = await hx.detect({}, data)
        assert r.status_code == 422 and r.json()["error"] == "bad_image"
        conforms("/v1/detect", "post", r)


async def test_dimension_limits_422(hx):
    for size in ((4097, 1), (1, 4097), (5000, 5000)):
        r = await hx.detect({}, image_bytes("PNG", size=size))
        assert r.status_code == 422, size


async def test_max_dimension_accepted(hx):
    r = await hx.detect({}, image_bytes("PNG", size=(4096, 4096)))     # 16 MP exactly; small file, big decode
    assert r.status_code == 200 and r.json()["image_width"] == 4096


async def test_params_part_limits(hx):
    r = await hx.detect(b'{"image_ref": "' + b"a" * 17000 + b'"}')
    assert r.status_code == 413
    for bad in (b"not json", b"[]", b'"x"', b"null", b'{"image_ref": "has space"}', b'{"image_ref": 5}',
                b'{"timeout_ms": 999}', b'{"timeout_ms": 25001}', b'{"max_detections": 0}', b'{"min_conf": "0.5"}',
                b'{"min_conf": true}', b'{"image_ref": "' + b"a" * 65 + b'"}', b'\xff\xfe'):
        r = await hx.detect(bad)
        assert r.status_code == 400, bad
        conforms("/v1/detect", "post", r)


async def test_multipart_framing_errors_400(hx):
    good, h = multipart({}, image_bytes())
    cases = {
        "no image": b"--" + BOUNDARY.encode() + b'\r\nContent-Disposition: form-data; name="params"\r\n\r\n{}\r\n--'
                    + BOUNDARY.encode() + b"--\r\n",
        "truncated": good[:len(good) // 2],
        "duplicate": good.replace(b'name="params"', b'name="image"', 1),
        "unknown part": good.replace(b'name="params"', b'name="url"', 1),
        "garbage": b"hello",
    }
    for name, body in cases.items():
        r = await hx.client.post("/v1/detect", content=body, headers={**H, **h})
        assert r.status_code == 400, name
        conforms("/v1/detect", "post", r)


async def test_not_multipart_415(hx):
    r = await hx.client.post("/v1/detect", json={}, headers=H)
    assert r.status_code == 415
    conforms("/v1/detect", "post", r)
    r = await hx.client.post("/v1/detect/snapshot", content=b"{}", headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 400
    conforms("/v1/detect/snapshot", "post", r)


async def test_snapshot_source_id_is_never_a_url_or_path(hx):
    for sid in ("rtsp://10.0.0.5/live", "../etc/passwd", "C:\\x", "http://169.254.169.254/", "a" * 33, "", 5, None):
        r = await hx.snap({"source_id": sid})
        assert r.status_code == 400, sid
    r = await hx.snap({"source_id": "cam0", "url": "http://x"})
    assert r.status_code == 400
    r = await hx.client.post("/v1/detect/snapshot", content=b"x" * 5000, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert hx.backend.calls == 0
