"""The gateway-facing API of contracts/openapi/agent-internal.yaml (1.3.0)."""
import asyncio
import base64
import json
import logging
import subprocess
import sys

import httpx
import pytest

from agent.app import create_app
from agent.auth import ApiAuth
from agent.mock import LocalServer
from agent.util import action_hash
from conftest import GUEST_SCOPE, SECRET, collect, names, one

GW = "gateway-secret-for-tests-0123456789"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


@pytest.fixture
def api(env, tmp_path):
    auth = ApiAuth.from_secret(GW, str(tmp_path / "agent-api.sha256"))
    app = create_app(env.agent, auth)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent",
                               headers={"X-Service-Secret": GW})
    env.client, env.app, env.auth = client, app, auth
    return env


def chat_body(**kw):
    # allowed_rooms / allowed_scenes null = unrestricted (agent-internal.yaml 1.3.0); a missing field would mean "nothing allowed"
    return {"user_id": "alice", "role": "member", "session_id": "s1", "text": "turn on the bedroom fan",
            "allowed_rooms": None, "allowed_scenes": None, **kw}


async def pending(env, n=1):
    for _ in range(150):
        if len(env.agent.confirmations.pending_ids()) >= n:
            return env.agent.confirmations.pending_ids()[-1]
        await asyncio.sleep(0.02)
    raise AssertionError("no pending confirmation")


def decision(env, rid, kind, action, user="alice", role="member", token=None, h=None):
    body = {"decision": kind, "user_id": user, "role": role, "action": action, "action_hash": h or action_hash(action)}
    if token:
        body["confirmation_token"] = token
    return env.client.post(f"/v1/confirmations/{rid}", json=body)


LOCK = {"type": "set_state", "endpoint_id": "ep_a1000004", "capability": "lock", "value": "unlocked"}


# ------------------------------------------------------------------------------------------------ authentication
async def test_every_operation_requires_the_gateway_secret(api):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://agent")
    for method, url in (("POST", "/v1/chat"), ("POST", "/v1/confirmations/cr_x"), ("GET", "/v1/events"),
                        ("GET", "/v1/memory?user_id=alice"), ("PUT", "/v1/memory/m_x"), ("DELETE", "/v1/memory/m_x?user_id=a"),
                        ("GET", "/v1/health"), ("GET", "/v1/stats"), ("POST", "/v1/runtime")):
        r = await c.request(method, url, json={})
        assert r.status_code == 401, (method, url)
        r = await c.request(method, url, json={}, headers={"X-Service-Secret": "wrong"})
        assert r.status_code == 401
        # the agent's HUB secret is a different secret and authorizes nothing here
        r = await c.request(method, url, json={}, headers={"X-Service-Secret": SECRET})
        assert r.status_code == 401
    assert (await api.client.get("/v1/health")).status_code == 200


def test_verifier_file_is_mandatory(tmp_path):
    with pytest.raises(SystemExit):
        ApiAuth(str(tmp_path / "missing.sha256"))
    (tmp_path / "bad").write_text("not a digest")
    with pytest.raises(SystemExit):
        ApiAuth(str(tmp_path / "bad"))


def test_insecure_no_auth_only_with_mock():
    r = subprocess.run([sys.executable, "-m", "agent", "--insecure-no-auth"], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "only together with --mock" in r.stderr
    r = subprocess.run([sys.executable, "-m", "agent", "--config", "none.json"], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0                                           # real mode without a verifier file refuses to start


async def test_health(api):
    j = (await api.client.get("/v1/health")).json()
    assert j == {"ok": True, "llm": "mock", "hub": "up"}
    await api.server.stop()
    j = (await api.client.get("/v1/health")).json()
    assert j["hub"] == "down" and j["ok"] is False


# ------------------------------------------------------------------------------------------------ chat
async def test_chat_validation_and_role(api):
    c = api.client
    for body in ({}, {**chat_body(), "role": "root"}, {k: v for k, v in chat_body().items() if k != "role"},
                 {**chat_body(), "audio_ref": "a.wav"}, {**chat_body(), "image_refs": ["r1"]},
                 {**chat_body(), "text": "x" * 4001}, {**chat_body(), "extra": 1},
                 {"user_id": "alice", "role": "member", "session_id": "s"},               # neither text nor images
                 {**chat_body(), "images": [{"ref": "r", "media_type": "image/gif", "data_b64": PNG}]},
                 {**chat_body(), "images": [{"ref": "r", "media_type": "image/png", "data_b64": PNG}] * 5}):
        assert (await c.post("/v1/chat", json=body)).status_code == 400, body
    r = await c.post("/v1/chat", content=b"not json")
    assert r.status_code == 400


async def test_chat_images_validated_inline_only(api):
    c = api.client
    bad_b64 = {"ref": "r", "media_type": "image/png", "data_b64": "!!!not base64!!!"}
    wrong_type = {"ref": "r", "media_type": "image/jpeg", "data_b64": PNG}                   # PNG bytes labelled JPEG
    big = {"ref": "r", "media_type": "image/png",
           "data_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)).decode()}
    for im in (bad_b64, wrong_type, big):
        assert (await c.post("/v1/chat", json=chat_body(images=[im]))).status_code == 400
    ctx = api.agent.new_request("alice", "s1", "", "member", images=1)                       # images only
    ev = await collect(ctx)
    assert "can't analyse images" in one(ev, "final")["text"] and not api.llm.requests
    ctx = api.agent.new_request("alice", "s1", "turn on the bedroom fan", "member", images=1)
    ev = await collect(ctx)
    assert one(ev, "final")["text"].endswith("(I can't analyse images yet.)")


async def test_chat_stream_over_http_and_429(api):
    srv = LocalServer(api.app)
    base = await srv.start()
    try:
        async with httpx.AsyncClient(timeout=20, headers={"X-Service-Secret": GW}) as c:
            frames, name = [], None
            async with c.stream("POST", base + "/v1/chat", json=chat_body(images=[
                    {"ref": "r1", "media_type": "image/png", "data_b64": PNG}])) as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                async for line in r.aiter_lines():
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        frames.append((name, json.loads(line[5:])))
            assert names(frames)[-1] == "final" and [c for c in api.fake.calls if c["tool"] == "set_state"]
            api.llm.delay = 1.0
            ctxs = [api.agent.new_request("bob", f"s{i}", "turn on the bedroom fan") for i in range(3)]   # fills 1 + 2 queued
            await asyncio.sleep(0.1)
            r = await c.post(base + "/v1/chat", json=chat_body())
            assert r.status_code == 429 and r.headers["retry-after"] == "5" and r.json()["error"] == "llm_busy"
            api.llm.delay = 0
            await asyncio.gather(*[collect(x) for x in ctxs])
    finally:
        await srv.stop()


async def test_role_comes_from_the_gateway_not_from_the_user_id(api):
    """A guest role disables persistence and confirmations even for a normal-looking id; a member named 'guest' is a member."""
    await api.chat("turn on the bedroom fan", user="carol", session="g", role="guest")
    assert api.agent.store.recent("carol", "g") == []                          # nothing persisted for a guest
    await api.chat("turn on the bedroom fan", user="guest", session="g", role="member")
    assert len(api.agent.store.recent("guest", "g")) == 2                      # persisted: id alone means nothing
    ctx, ev = await api.chat("unlock the front door", user="dave", role="guest", scope=GUEST_SCOPE)
    assert "confirmation_request" not in names(ev) and "guests can't" in one(ev, "final")["text"]
    ctx = api.agent.new_request("erin", "s", "unlock the front door", "member")
    await pending(api)
    api.agent.confirmations.resolve(api.agent.confirmations.pending_ids()[0], "erin", False)
    await collect(ctx)


# ------------------------------------------------------------------------------------------------ confirmations
async def test_approved_decision_recomputes_hash_and_executes(api, caplog):
    caplog.set_level(logging.DEBUG)
    ctx = api.agent.new_request("alice", "s1", "unlock the front door", "member")
    task = asyncio.create_task(collect(ctx))
    rid = await pending(api)
    api.llm.requests.clear()
    tok = api.fake.mint("alice", LOCK)
    r = await decision(api, rid, "approved", LOCK, token=tok)
    assert r.status_code == 202
    ev = await task
    assert one(ev, "tool_result")["ok"] is True
    sets = [c for c in api.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 1 and sets[0]["meta"]["confirmation_token"] == tok and sets[0]["meta"]["user_id"] == "alice"
    assert api.agent.confirmations.pending_ids() == []
    # repeat of a handled decision: 202 and nothing happens
    assert (await decision(api, rid, "approved", LOCK, token=tok)).status_code == 202
    assert len([c for c in api.fake.calls if c["tool"] == "set_state"]) == 1
    # the token is nowhere: logs, events, the action log, messages, the ring, health/stats
    dump = (caplog.text + json.dumps(ev) + json.dumps(api.agent.store.action_log(500))
            + json.dumps(api.agent.store.recent("alice", "s1", 50)) + json.dumps([e.data for e in api.agent.ring._buf])
            + json.dumps(api.agent.stats()) + json.dumps(api.llm.requests))
    assert tok not in dump and GW not in dump and SECRET not in dump


async def test_hash_mismatch_is_refused_and_nothing_runs(api):
    ctx = api.agent.new_request("alice", "s1", "unlock the front door", "member")
    task = asyncio.create_task(collect(ctx))
    rid = await pending(api)
    tok = api.fake.mint("alice", LOCK)
    other = {**LOCK, "value": "locked"}
    r = await decision(api, rid, "approved", LOCK, token=tok, h=action_hash(other))      # claimed hash != action
    assert r.status_code == 409 and r.json()["error"] == "action_hash_mismatch"
    r = await decision(api, rid, "approved", other, token=tok)                           # hash fits the action, not the request
    assert r.status_code == 409
    assert not [c for c in api.fake.calls if c["tool"] == "set_state"]
    assert api.agent.confirmations.pending_ids() == [rid]                                # still waiting, untouched
    assert tok not in r.text
    assert (await decision(api, rid, "declined", LOCK)).status_code == 202
    await task


async def test_declined_and_expired_clear_pending_state(api):
    for kind, event in (("declined", "confirmation_declined"), ("expired", "confirmation_expired")):
        ctx = api.agent.new_request("alice", "s1", "unlock the front door", "member")
        task = asyncio.create_task(collect(ctx))
        rid = await pending(api)
        assert (await decision(api, rid, kind, LOCK)).status_code == 202
        ev = await task
        assert event in names(ev) and names(ev).count("confirmation_request") == 1
        assert api.agent.confirmations.pending_ids() == []
        assert (await decision(api, rid, kind, LOCK)).status_code == 202                  # repeat: no-op
    assert not [c for c in api.fake.calls if c["tool"] == "set_state"]
    assert (await decision(api, "cr_unknown", "declined", LOCK)).status_code == 404


async def test_other_users_request_is_404_and_untouched(api):
    ctx = api.agent.new_request("alice", "s1", "unlock the front door", "member")
    task = asyncio.create_task(collect(ctx))
    rid = await pending(api)
    for kind in ("declined", "approved"):
        tok = api.fake.mint("mallory", LOCK) if kind == "approved" else None
        assert (await decision(api, rid, kind, LOCK, user="mallory", token=tok)).status_code == 404
    assert api.agent.confirmations.pending_ids() == [rid]
    await decision(api, rid, "declined", LOCK)
    await task


async def test_guest_role_cannot_use_the_hand_off(api):
    r = await decision(api, "cr_x", "approved", LOCK, role="guest", token=api.fake.mint("alice", LOCK))
    assert r.status_code == 400 and not [c for c in api.fake.calls if c["tool"] == "set_state"]


async def test_decision_body_validation(api):
    tok = api.fake.mint("alice", LOCK)
    base = {"decision": "approved", "user_id": "alice", "role": "member", "action": LOCK, "action_hash": action_hash(LOCK)}
    for body in ({**base}, {**base, "confirmation_token": "short"}, {**base, "decision": "declined", "confirmation_token": tok},
                 {**base, "confirmation_token": tok, "extra": 1}, {**base, "confirmation_token": tok, "action_hash": "zz"},
                 {**base, "confirmation_token": tok, "action": {"type": "set_state", "endpoint_id": "bad"}},
                 {**base, "confirmation_token": tok, "action": {"type": "run_scene", "scene": "x", "needs_confirmation": []}}):
        r = await api.client.post("/v1/confirmations/cr_x", json=body)
        assert r.status_code == 400 and tok not in r.text, body
    assert not [c for c in api.fake.calls if c["tool"] == "set_state"]


async def test_approved_unknown_request_runs_the_exact_action_retry_path(api):
    """A Retry request exists only at the gateway; its approval carries the full action and the agent executes exactly that."""
    await api.chat("hello", user="alice")                       # the agent remembers alice's scope from her latest chat
    tok = api.fake.mint("alice", LOCK)
    r = await decision(api, "cr_from_gateway_retry", "approved", LOCK, token=tok)
    assert r.status_code == 202
    for _ in range(100):
        if [c for c in api.fake.calls if c["tool"] == "set_state"]:
            break
        await asyncio.sleep(0.05)
    sets = [c for c in api.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 1 and sets[0]["args"] == {"endpoint_id": "ep_a1000004", "capability": "lock", "value": "unlocked"}
    assert sets[0]["meta"]["confirmation_token"] == tok
    await asyncio.sleep(0.2)
    note = [e for e in api.agent.ring._buf if e.name == "notification"]
    assert note and note[-1].data["user_id"] == "alice" and note[-1].data["level"] == "action_result"
    assert tok not in json.dumps([e.data for e in api.agent.ring._buf])
    # a token for another action burns and does nothing (hub: confirmation_required) -> reported as expired, not executed
    tok2 = api.fake.mint("alice", {**LOCK, "value": "locked"})
    await decision(api, "cr_retry2", "approved", LOCK, token=tok2)
    await asyncio.sleep(0.4)
    assert len([c for c in api.fake.calls if c["tool"] == "set_state"]) == 2
    assert api.fake.state["ep_a1000004"]["lock"] == "unlocked"


async def test_scene_confirmation_uses_hub_scene_id_and_hash(api):
    ctx = api.agent.new_request("alice", "s1", "run the good night scene", "member")
    task = asyncio.create_task(collect(ctx))
    rid = await pending(api)
    action = api.agent.confirmations.pending_action(rid)
    assert action == {"type": "run_scene", "scene_id": "sc_9a8b7c6d",
                      "actions": [{"endpoint_id": "ep_a1000004", "capability": "lock", "value": "locked"}]}
    blocked_hash = [c for c in api.fake.calls if c["tool"] == "run_scene"]                 # first call: no token, blocked
    assert len(blocked_hash) == 1 and "confirmation_token" not in blocked_hash[0]["meta"]
    assert api.agent.confirmations.pending_hash(rid, "alice") == action_hash(action)
    tok = api.fake.mint("alice", action)
    assert (await decision(api, rid, "approved", action, token=tok)).status_code == 202
    ev = await task
    req = one(ev, "confirmation_request")
    assert req["action"] == action and req["action_hash"] == action_hash(action) and "needs_confirmation" not in req["action"]
    assert [d for n, d in ev if n == "tool_result"][-1]["ok"] is True
    assert api.fake.state["ep_a1000004"]["lock"] == "locked" and api.fake.state["ep_a1000002"]["power"] is False
    assert tok not in json.dumps(ev)


async def test_scene_with_inconsistent_hub_hash_is_not_offered(api):
    orig = api.fake.run_scene

    def bad(scene, meta):
        r = orig(scene, meta)
        return {**r, "action_hash": "0" * 64} if r.get("status") == "needs_confirmation" else r

    api.fake.run_scene = bad
    ctx, ev = await api.chat("run the good night scene")
    assert "confirmation_request" not in names(ev) and api.agent.confirmations.pending_ids() == []


# ------------------------------------------------------------------------------------------------ events
async def test_events_stream_tags_user_replays_and_resets(api):
    ring = api.agent.ring
    ring.publish("notification", {"type": "notification", "level": "info", "text": "a"}, "alice")
    ring.publish("action_settled", {"call_id": "c1", "endpoint_id": "ep_a1000005"}, None)
    ring.publish("notification", {"type": "notification", "level": "info", "text": "c"}, "bob")
    srv = LocalServer(api.app)
    base = await srv.start()

    async def read(headers, n, timeout=5):
        got, name, last_id = [], None, None
        async with httpx.AsyncClient(timeout=timeout, headers={"X-Service-Secret": GW, **headers}) as c:
            async with c.stream("GET", base + "/v1/events") as r:
                assert r.status_code == 200
                async for line in r.aiter_lines():
                    if line.startswith("id:"):
                        last_id = line[3:].strip()
                    elif line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        got.append((name, last_id, json.loads(line[5:])))
                        if len(got) >= n:
                            return got
        return got

    try:
        got = await read({"Last-Event-ID": "1"}, 2)
        assert [(n, d["user_id"]) for n, _, d in got] == [("action_settled", "household"), ("notification", "bob")]
        assert [i for _, i, _ in got] == ["2", "3"]
        # live event after connect, tagged with its owner
        t = asyncio.create_task(read({"Last-Event-ID": "3"}, 1))
        await asyncio.sleep(0.3)
        ring.publish("confirmation_request", {"request_id": "cr_1"}, "alice")
        got = await t
        assert got[0][0] == "confirmation_request" and got[0][2]["user_id"] == "alice" and got[0][1] == "4"
        # an id from a previous agent run, and one older than the buffer -> reset first
        assert (await read({"Last-Event-ID": "999"}, 1))[0][0] == "reset"
        ring._buf.popleft()
        assert (await read({"Last-Event-ID": "0"}, 1))[0][0] == "reset"
    finally:
        await srv.stop()


async def test_late_settle_after_disconnect_reaches_the_ring_with_user_id(api):
    api.fake.settle_delay = 0.3
    ctx = api.agent.new_request("alice", "s1", "open the blinds", "member")
    ev = await collect(ctx, wait_close=False)                         # got `final`, motion still running
    ctx.detached = True                                               # the gateway dropped the chat stream
    for _ in range(60):
        if any(e.name == "action_settled" for e in api.agent.ring._buf):
            break
        await asyncio.sleep(0.05)
    e = [e for e in api.agent.ring._buf if e.name == "action_settled"][0]
    assert e.data["user_id"] == "alice" and e.data["call_id"] == one(ev, "tool_call")["call_id"]


async def test_unmatched_settle_timeout_is_a_household_notification(api):
    await api.agent.settles.on_hub_event("action_settle_timeout", {"endpoint_id": "ep_a1000005", "capability": "position",
                                                                    "value": 10, "observed": None, "settle_s": 30})
    e = [e for e in api.agent.ring._buf if e.name == "notification"][0]
    assert e.data["user_id"] == "household" and e.data["level"] == "action_failed" and e.data["type"] == "notification"


# ------------------------------------------------------------------------------------------------ memory
async def test_memory_endpoints_are_per_user(api):
    c, st = api.client, api.agent.store
    a = st.add_memory("alice", "prefers 21C")
    b = st.add_memory("bob", "bob only")
    r = await c.get("/v1/memory", params={"user_id": "alice"})
    assert [m["text"] for m in r.json()] == ["prefers 21C"] and set(r.json()[0]) >= {"memory_id", "text", "updated_at"}
    assert (await c.get("/v1/memory")).status_code == 400                          # no cross-user listing
    r = await c.put(f"/v1/memory/{a['memory_id']}", json={"user_id": "alice", "text": "prefers 22C"})
    assert r.status_code == 200 and r.json()["text"] == "prefers 22C" and r.json()["memory_id"] == a["memory_id"]
    assert (await c.put(f"/v1/memory/{a['memory_id']}", json={"user_id": "bob", "text": "hijack"})).status_code == 404
    assert (await c.put(f"/v1/memory/{b['memory_id']}", json={"user_id": "alice", "text": "hijack"})).status_code == 404
    assert (await c.put(f"/v1/memory/{a['memory_id']}", json={"user_id": "alice", "text": ""})).status_code == 400
    assert (await c.put(f"/v1/memory/{a['memory_id']}", json={"user_id": "alice", "text": "x" * 501})).status_code == 400
    assert (await c.put(f"/v1/memory/{a['memory_id']}", json={"user_id": "alice", "text": "ok", "extra": 1})).status_code == 400
    assert (await c.delete(f"/v1/memory/{a['memory_id']}", params={"user_id": "bob"})).status_code == 404
    assert (await c.delete(f"/v1/memory/{a['memory_id']}", params={"user_id": "alice"})).status_code == 204
    assert (await c.delete(f"/v1/memory/{a['memory_id']}", params={"user_id": "alice"})).status_code == 404
    assert [m["text"] for m in st.list_memories("bob")] == ["bob only"]


async def test_memories_reach_the_prompt_tail_for_members_not_guests(api):
    api.agent.store.add_memory("alice", "likes the fan on low")
    await api.chat("turn on the bedroom fan", user="alice", role="member")
    await api.chat("turn on the bedroom fan", user="alice", role="guest")
    member_req, guest_req = api.llm.requests[-2], api.llm.requests[-1]
    assert "likes the fan on low" in member_req[-1]["content"] and "likes the fan on low" not in json.dumps(guest_req)
    assert "likes the fan on low" not in member_req[0]["content"]                   # tail only, never the cached prefix


# ------------------------------------------------------------------------------------------------ 1.3.0 scope fields
async def test_chat_scope_fields_are_validated_and_parsed_fail_closed(api):
    seen = []
    orig = api.agent.new_request

    def spy(user, sess, text, role="member", images=0, scope=None):
        seen.append((role, scope))
        return orig(user, sess, text, role, images, scope)

    api.agent.new_request = spy
    base = {k: v for k, v in chat_body().items() if k not in ("allowed_rooms", "allowed_scenes")}
    base["text"] = "hello there"
    for body in ({**base, "allowed_rooms": ["a", "a"]}, {**base, "allowed_rooms": [1]}, {**base, "allowed_rooms": "Bedroom"},
                 {**base, "allowed_rooms": [""]}, {**base, "allowed_rooms": ["x" * 49]},
                 {**base, "allowed_rooms": [str(i) for i in range(65)]}, {**base, "allowed_scenes": [str(i) for i in range(201)]},
                 {**base, "allowed_scenes": ["s" * 65]}, {**base, "allowed_scenes": {}}):
        assert (await api.client.post("/v1/chat", json=body)).status_code == 400
    assert seen == []
    cases = [({}, "member", (frozenset(), frozenset())),                                  # missing = nothing
             ({"allowed_rooms": None, "allowed_scenes": None}, "member", (None, None)),   # explicit null = unrestricted
             ({"allowed_rooms": None, "allowed_scenes": None}, "admin", (None, None)),
             ({"allowed_rooms": None, "allowed_scenes": None}, "guest", (frozenset(), frozenset())),   # never for a guest
             ({"allowed_rooms": ["Bedroom"]}, "guest", (frozenset({"Bedroom"}), frozenset())),
             ({"allowed_rooms": ["Bedroom"], "allowed_scenes": ["sc_9a8b7c6d"]}, "member",
              (frozenset({"Bedroom"}), frozenset({"sc_9a8b7c6d"})))]
    for extra, role, (rooms, scenes) in cases:
        seen.clear()
        r = await api.client.post("/v1/chat", json={**base, "role": role, **extra})
        assert r.status_code == 200, (extra, role)
        assert len(seen) == 1 and seen[0][0] == role and (seen[0][1].rooms, seen[0][1].scenes) == (rooms, scenes), (extra, role)


async def test_a_member_chat_without_scope_fields_cannot_touch_any_device_end_to_end(api):
    api.agent.llm.constrained = False
    api.agent.llm.script.append((".", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_a1000002", "capability": "power", "value": True}}], "say": "ok"}))
    body = {k: v for k, v in chat_body().items() if k not in ("allowed_rooms", "allowed_scenes")}
    r = await api.client.post("/v1/chat", json=body)
    assert r.status_code == 200 and '"error":"out_of_scope"' in r.text
    assert not [c for c in api.fake.calls if c["tool"] == "set_state"]
