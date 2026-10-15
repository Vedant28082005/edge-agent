import hashlib
import hmac as hmac_mod

import pytest

from conftest import H, SECRET, conforms, image_bytes, multipart
from yolo_node.auth import ApiAuth
from yolo_node import __main__ as cli

BAD = [{}, {"X-Service-Secret": ""}, {"X-Service-Secret": "wrong"}, {"X-Service-Secret": SECRET + "x"},
       {"X-Service-Secret": "A" * 600}, {"X-Service-Secret": SECRET.upper()}]


@pytest.mark.parametrize("hdr", BAD)
async def test_every_operation_requires_secret(hx, hdr):
    body, h = multipart({}, image_bytes())
    calls = [hx.client.post("/v1/detect", content=body, headers={**h, **hdr}),
             hx.client.post("/v1/detect/snapshot", json={"source_id": "cam0"}, headers=hdr),
             hx.client.get("/v1/sources", headers=hdr),
             hx.client.get("/anything-else", headers=hdr)]
    for c in calls:
        r = await c
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized", "message": "missing or wrong X-Service-Secret"}
    assert hx.backend.calls == 0


async def test_health_is_open_and_secret_header_irrelevant(hx):
    assert (await hx.client.get("/health")).status_code == 200
    assert (await hx.client.get("/health", headers={"X-Service-Secret": "wrong"})).status_code == 200


async def test_auth_checked_before_body_is_read(hx):
    # 401 even with an oversized body: no byte is parsed for an unauthenticated caller
    r = await hx.client.post("/v1/detect", content=b"x" * (6 * 1024 * 1024),
                             headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert r.status_code == 401


async def test_correct_secret_ok(hx):
    assert (await hx.client.get("/v1/sources", headers=H)).status_code == 200


def test_constant_time_compare_and_verifier_only(tmp_path, monkeypatch):
    calls = []
    real = hmac_mod.compare_digest
    monkeypatch.setattr("yolo_node.auth.hmac.compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    a = ApiAuth.from_secret(SECRET, str(tmp_path / "v"))
    assert a.check(SECRET) and not a.check("nope")
    assert len(calls) == 2 and calls[0][1] == hashlib.sha256(SECRET.encode()).hexdigest()
    assert SECRET not in (tmp_path / "v").read_text()                   # the node holds the verifier, never the secret


def test_refuses_to_start_without_verifier(tmp_path):
    with pytest.raises(SystemExit):
        ApiAuth(str(tmp_path / "missing"))
    (tmp_path / "bad").write_text("not-a-digest")
    with pytest.raises(SystemExit):
        ApiAuth(str(tmp_path / "bad"))


def test_insecure_no_auth_only_with_mock():
    assert cli.main(["--insecure-no-auth"]) == 2


def test_refuses_wildcard_bind():
    with pytest.raises(SystemExit):
        cli.main(["--mock", "--insecure-no-auth", "--host", "0.0.0.0"])
