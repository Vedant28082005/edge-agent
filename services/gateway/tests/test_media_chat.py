from __future__ import annotations

import httpx
import pytest

from calls import PNG, WAV
from gateway.upstream import JsonClient


def up_png(gw, who="member", data=PNG):
    return gw.pub.post("/images", files={"image": ("a.png", data, "image/png")}, headers=gw.h(who))


def test_chat_forwards_gateway_identity_and_per_user_sessions(gw):
    a = gw.pub.post("/chat", json={"text": "hi", "session_id": "main"}, headers=gw.h("admin"))
    b = gw.pub.post("/chat", json={"text": "hi", "session_id": "main"}, headers=gw.h("member"))
    assert a.headers["x-session-id"] == "main" and "event: final" in a.text
    ra, rb = gw.m.agent.requests
    assert ra["user_id"] == gw.uid("admin") and rb["user_id"] == gw.uid("member")
    assert (ra["role"], rb["role"]) == ("admin", "member")                                       # role from the gateway's own DB
    assert set(ra) == {"user_id", "role", "session_id", "text"}                                  # exactly the ChatRequest fields
    assert ra["session_id"] != rb["session_id"] and len(ra["session_id"]) <= 64                  # same client id, separate sessions
    # the client cannot choose the user
    assert gw.pub.post("/chat", json={"text": "hi", "user_id": "u_other"}, headers=gw.h("member")).status_code == 400
    assert b.status_code == 200


def test_chat_rejects_a_bad_ref_without_consuming_the_good_ones(gw):
    good = up_png(gw).json()["image_ref"]
    r = gw.pub.post("/chat", json={"text": "x", "image_refs": [good, "img_nope"]}, headers=gw.h("member"))
    assert r.status_code == 400 and r.json()["error"] == "bad_media_ref"
    assert gw.pub.post("/chat", json={"text": "x", "image_refs": [good]}, headers=gw.h("member")).status_code == 200


def test_chat_input_limits(gw):
    h = gw.h("member")
    for body in ({}, {"text": "x" * 4001}, {"text": "hi", "image_refs": ["a"] * 5}, {"text": "hi", "session_id": "bad id!"},
                 {"audio_ref": "aud_nope"}, {"image_refs": ["img_nope"]}):
        assert gw.pub.post("/chat", json=body, headers=h).status_code == 400, body
    assert gw.m.agent.requests == []


def test_chat_mirrors_confirmation_requests_but_not_async_results(gw):
    seen = []
    gw.ctx.bc.publish = lambda ev: seen.append(ev)
    gw.pub.post("/chat", json={"text": "unlock the front door"}, headers=gw.h("member"))
    assert [e.type for e in seen] == ["confirmation_request"] and seen[0].user_id == gw.uid("member")
    seen.clear()
    gw.pub.post("/chat", json={"text": "unlock the front door"}, headers=gw.h("member"))     # new request id each turn
    assert [e.type for e in seen] == ["confirmation_request"]


def test_chat_rate_limit_agent_busy_and_body_cap(gw):
    gw.ctx.limiter.limits["chat"] = (2, 60)
    h = gw.h("member")
    assert [gw.pub.post("/chat", json={"text": "hi"}, headers=h).status_code for _ in range(2)] == [200, 200]
    r = gw.pub.post("/chat", json={"text": "hi"}, headers=h)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["error"] == "rate_limited"
    assert gw.pub.post("/chat", json={"text": "hi"}, headers=gw.h("admin")).status_code == 200      # per user
    big = gw.pub.post("/chat", content=b'{"text":"' + b"x" * 300000 + b'"}', headers={**gw.h("admin"), "Content-Type": "application/json"})
    assert big.status_code == 413 and big.json()["error"] == "too_large"

    def busy(request):
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "llm_busy"})
    gw.ctx.limiter.limits["chat"] = (100, 60)
    gw.ctx.agent._http = httpx.AsyncClient(base_url="http://agent", transport=httpx.MockTransport(busy))
    r = gw.pub.post("/chat", json={"text": "hi"}, headers=h)
    assert r.status_code == 429 and r.headers["retry-after"] == "7"


def test_upstream_errors_and_timeouts_use_the_contract_statuses(gw):
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    def broken(request):
        return httpx.Response(500, json={"error": "boom", "detail": "secret internals"})
    gw.ctx.hub._http = httpx.AsyncClient(base_url="http://hub", transport=httpx.MockTransport(timeout))
    r = gw.pub.post("/devices/ep_00000001/set", json={"capability": "power", "value": True}, headers=gw.h("admin"))
    assert r.status_code == 504 and r.json()["error"] == "upstream_timeout"
    gw.ctx.hub._http = httpx.AsyncClient(base_url="http://hub", transport=httpx.MockTransport(broken))
    r = gw.pub.post("/scenes/sc_00000001/run", headers=gw.h("admin"))
    assert r.status_code == 502 and r.json()["error"] == "upstream_error" and "secret internals" not in r.text
    gw.ctx.agent._http = httpx.AsyncClient(base_url="http://agent", transport=httpx.MockTransport(timeout))
    assert gw.pub.post("/chat", json={"text": "hi"}, headers=gw.h("admin")).status_code == 504


def test_chat_agent_down_is_a_clean_502(gw):
    def boom(request):
        raise httpx.ConnectError("down")
    gw.ctx.agent._http = httpx.AsyncClient(base_url="http://agent", transport=httpx.MockTransport(boom))
    r = gw.pub.post("/chat", json={"text": "hi"}, headers=gw.h("member"))
    assert r.status_code == 502 and r.json()["error"] == "upstream_error"


# ------------------------------------------------------------------ images
def test_image_is_memory_only_single_use_and_forwarded(gw, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", lambda self: (_ for _ in ()).throw(AssertionError("spooled to disk")))
    big = PNG + b"\x00" * (3 * 1024 * 1024)                                  # > Starlette's default 1 MiB spool limit
    ref = up_png(gw, data=big).json()["image_ref"]
    assert ref.startswith("img_") and len(gw.ctx.media) == 1
    r = gw.pub.post("/chat", json={"text": "look", "image_refs": [ref]}, headers=gw.h("guest"))                       # other user's ref
    assert r.status_code == 400 and r.json()["error"] == "bad_media_ref"
    r = gw.pub.post("/chat", json={"text": "look", "image_refs": [ref]}, headers=gw.h("member"))
    assert r.status_code == 200 and len(gw.ctx.media) == 0                   # deleted after use
    (req,) = gw.m.agent.requests
    assert "image_refs" not in req and "audio_ref" not in req                                  # the agent never gets refs
    assert req["images"][0]["media_type"] == "image/png" and req["images"][0]["ref"] == ref
    assert set(req["images"][0]) == {"ref", "media_type", "data_b64"} and req["role"] == "member"
    assert gw.pub.post("/chat", json={"text": "again", "image_refs": [ref]}, headers=gw.h("member")).status_code == 400


def test_image_ttl_type_size_and_quota(gw):
    ref = up_png(gw).json()["image_ref"]
    gw.clock.advance(gw.ctx.cfg.media_ttl_s + 1)
    assert gw.pub.post("/chat", json={"text": "x", "image_refs": [ref]}, headers=gw.h("member")).status_code == 400   # expired
    assert gw.pub.post("/images", files={"image": ("a.png", b"not an image at all", "image/png")}, headers=gw.h("member")).status_code == 415
    assert gw.pub.post("/images", files={"image": ("a.gif", b"GIF89a" + b"\x00" * 20, "image/gif")}, headers=gw.h("member")).status_code == 415
    assert up_png(gw, "admin", data=b"\xff\xd8\xff" + b"\x00" * 8).status_code == 200                            # jpeg magic, labelled png
    assert gw.pub.post("/images", files={"image": ("a.png", b"", "image/png")}, headers=gw.h("member")).status_code in (400, 415)
    too_big = PNG + b"\x00" * (gw.ctx.cfg.max_image_bytes + 1)
    assert up_png(gw, data=too_big).status_code == 413
    assert gw.pub.post("/images", content=PNG, headers={**gw.h("member"), "Content-Type": "image/png"}).status_code in (400, 422)
    codes = [up_png(gw).status_code for _ in range(gw.ctx.cfg.media_max_per_user + 1)]
    assert codes == [200] * gw.ctx.cfg.media_max_per_user + [429]
    r = up_png(gw)
    assert r.status_code == 429 and r.json()["error"] == "too_many_uploads" and int(r.headers["retry-after"]) >= 1
    assert up_png(gw, "admin").status_code == 200                           # other users are unaffected


# ------------------------------------------------------------------ voice
def test_voice_transcribes_and_chat_uses_the_transcript(gw):
    r = gw.pub.post("/voice", files={"audio": ("a.wav", WAV, "audio/wav")}, headers=gw.h("member"))
    assert r.status_code == 200 and r.json()["text"] == "turn on the bedroom light"
    ref = r.json()["audio_ref"]
    assert all(i.data is None for i in gw.ctx.media._items.values())           # audio bytes are not kept
    assert gw.pub.post("/chat", json={"audio_ref": ref}, headers=gw.h("admin")).status_code == 400   # not the owner
    assert gw.pub.post("/chat", json={"audio_ref": ref}, headers=gw.h("member")).status_code == 200
    assert gw.m.agent.requests[-1]["text"] == "turn on the bedroom light" and "audio_ref" not in gw.m.agent.requests[-1]
    ref2 = gw.pub.post("/voice", files={"audio": ("a.wav", WAV, "audio/wav")}, headers=gw.h("member")).json()["audio_ref"]
    assert gw.pub.post("/chat", json={"text": "and also", "audio_ref": ref2}, headers=gw.h("member")).status_code == 200
    assert gw.m.agent.requests[-1]["text"] == "and also\nturn on the bedroom light"                       # transcript is appended
    assert gw.pub.post("/chat", json={"audio_ref": ref}, headers=gw.h("member")).status_code == 400   # single use


def test_voice_limits_and_vision_failure(gw):
    h = gw.h("member")
    assert gw.pub.post("/voice", files={"audio": ("a.txt", WAV, "text/plain")}, headers=h).status_code == 200      # sniffed, not trusted
    assert gw.pub.post("/voice", files={"audio": ("a.wav", b"this is not audio data", "audio/wav")}, headers=h).status_code == 415
    assert gw.pub.post("/voice", files={"audio": ("a.wav", WAV + b"\x00" * gw.ctx.cfg.max_audio_bytes, "audio/wav")},
                       headers=h).status_code == 413
    assert len(gw.m.vision.received) == 1                                   # only the sniffed-OK upload reached vision-node
    gw.m.vision.fail = True
    r = gw.pub.post("/voice", files={"audio": ("a.wav", WAV, "audio/wav")}, headers=h)
    assert r.status_code == 502 and r.json()["error"] == "vision_unavailable"
    gw.m.vision.fail = False
    gw.ctx.limiter.limits["voice"] = (1, 60)
    gw.ctx.limiter._q.clear()
    assert gw.pub.post("/voice", files={"audio": ("a.wav", WAV, "audio/wav")}, headers=h).status_code == 200
    r = gw.pub.post("/voice", files={"audio": ("a.wav", WAV, "audio/wav")}, headers=h)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


# ------------------------------------------------------------------ health, memory
def test_health_aggregates_with_partial_results(gw):
    rows = {n["name"]: n for n in gw.pub.get("/system/health", headers=gw.h("guest")).json()["nodes"]}
    assert set(rows) == {"gateway", "device-hub", "agent", "vision-node", "yolo-node"} and all(r["up"] for r in rows.values())
    assert any(c["path"] == "/v1/health" and c["secret_ok"] for c in gw.m.agent.calls)
    assert rows["vision-node"]["mem_available_mb"] == 1500 and rows["vision-node"]["temp_c"] == 51.5 and rows["yolo-node"]["queue_depth"] == 1

    def timeout(request):
        raise httpx.ReadTimeout("slow")
    gw.ctx.yolo = JsonClient("yolo-node", "http://yolo", 1.0, None, httpx.MockTransport(timeout))
    gw.m.vision.fail = True
    rows = {n["name"]: n for n in gw.pub.get("/system/health", headers=gw.h("member")).json()["nodes"]}
    assert rows["yolo-node"] == {"name": "yolo-node", "up": False} and rows["device-hub"]["up"] and rows["agent"]["up"]


@pytest.mark.parametrize("role,expect", [("member", {"m1": "likes tea"}), ("admin", {"m1": "likes tea"})])
def test_memory_is_the_callers_own(gw, role, expect):
    gw.m.agent.memories[gw.uid(role)] = {"m1": "likes tea"}
    gw.m.agent.memories["someone-else"] = {"m9": "secret"}
    got = gw.pub.get("/memory", headers=gw.h(role)).json()
    assert {m["memory_id"]: m["text"] for m in got} == expect
    assert gw.pub.put("/memory/m9", json={"text": "hax"}, headers=gw.h(role)).status_code == 404       # not theirs
    assert gw.pub.delete("/memory/m9", headers=gw.h(role)).status_code == 404
    assert gw.m.agent.memories["someone-else"] == {"m9": "secret"}
    assert gw.pub.put("/memory/m1", json={"text": "x" * 501}, headers=gw.h(role)).status_code == 400


def test_guests_have_no_memory(gw):
    h = gw.h("guest")
    assert gw.pub.get("/memory", headers=h).json() == []
    assert gw.pub.put("/memory/m1", json={"text": "x"}, headers=h).status_code == 403
    assert gw.pub.delete("/memory/m1", headers=h).status_code == 403


def test_wifi_profile_routes_are_write_only_for_the_password(gw):
    a = gw.h("admin")
    r = gw.pub.post("/admin/wifi-profiles", json={"name": "home", "ssid": "HomeNet", "password": "profile-wifi-pass-9"}, headers=a)
    assert r.status_code == 201 and r.json() == {"name": "home", "ssid": "HomeNet"}
    assert gw.pub.post("/admin/wifi-profiles", json={"name": "home", "ssid": "HomeNet2", "password": "profile-wifi-pass-9"},
                       headers=a).status_code == 201                                       # create or replace by name
    listing = gw.pub.get("/admin/wifi-profiles", headers=a)
    assert listing.json() == [{"name": "home", "ssid": "HomeNet2"}] and "profile-wifi-pass-9" not in listing.text
    for bad in ({"name": "", "ssid": "x", "password": "p"}, {"name": "n", "ssid": "x" * 33, "password": "p"},
                {"name": "n", "ssid": "x", "password": "p" * 65}, {"name": "n", "ssid": "x"}, {"name": "n", "ssid": "x", "password": "p", "x": 1}):
        r = gw.pub.post("/admin/wifi-profiles", json=bad, headers=a)
        assert r.status_code == 400 and "p" * 65 not in r.text                             # validation errors never echo input
    raw = gw.ctx.db.one("SELECT ssid, password_enc FROM wifi_profiles")
    assert b"profile-wifi-pass-9" not in bytes(raw["password_enc"])
    assert "profile-wifi-pass-9" not in "\n".join(gw.ctx.db._c.iterdump())
    r = gw.pub.post("/admin/provision-sessions", json={"node_id": "esp32-aabbccddeeff", "wifi_profile": "home"}, headers=a)
    assert r.status_code == 201
    assert r.json()["bundle"]["wifi"] == {"ssid": "HomeNet2", "password": "profile-wifi-pass-9"}       # hub got inline wifi
    assert r.headers["cache-control"] == "no-store"
    assert gw.pub.post("/admin/provision-sessions", json={"node_id": "esp32-aabbccddeeff", "wifi_profile": "nope"}, headers=a).status_code == 404
    assert gw.pub.post("/admin/provision-sessions", json={"node_id": "esp32-aabbccddeeff"}, headers=a).status_code == 400
    audit = "".join(r["detail"] + r["target"] for r in gw.ctx.db.all("SELECT * FROM audit_log"))
    assert "profile-wifi-pass-9" not in audit and "wifi_profile_put" in "".join(r["action"] for r in gw.ctx.db.all("SELECT action FROM audit_log"))
    assert gw.pub.delete("/admin/wifi-profiles/home", headers=a).status_code == 204
    assert gw.pub.delete("/admin/wifi-profiles/home", headers=a).status_code == 404
    assert gw.pub.get("/admin/wifi-profiles", headers=a).json() == []


def test_hub_credential_problems_do_not_leak_as_user_errors(gw):
    gw.m.hub.secret = "rotated"                              # the hub no longer accepts our credential
    r = gw.pub.get("/devices", headers=gw.h("member"))
    assert r.status_code == 502 and r.json()["error"] == "upstream_error"


def test_admin_calls_carry_the_actor_and_hub_gets_user_scoped_bodies(gw):
    gw.pub.post("/admin/nodes/esp32-aabbccddeeff/identify", json={"duration_s": 5}, headers=gw.h("admin"))
    gw.pub.post("/devices/ep_00000001/set", json={"capability": "power", "value": True}, headers=gw.h("member"))
    assert [c["actor"] for c in gw.m.hub.calls if c["path"].endswith(("identify", "/set"))] == [gw.uid("admin"), gw.uid("member")]
