"""GET /events by scope: the agent secret gets a server-side allowlist (live and replay), the gateway secret gets
everything, the agent still cannot call any admin route, and _meta.call_id is echoed on the settle events."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import WIFI, light_ep, make_announce
from test_api import Served
from test_s0c import curtain  # noqa: F401  (fixture)

GW = {"X-Service-Secret": "gw-secret-value"}
AG = {"X-Service-Secret": "agent-secret-value"}
AGENT_OK = {"registry_changed", "node_status", "state", "action_settled", "action_settle_timeout"}
N5 = "esp32-000000000005"


@pytest.fixture
def served(home, tmp_path):
    (tmp_path / "gateway.sha256").write_text(hashlib.sha256(b"gw-secret-value").hexdigest())
    (tmp_path / "agent.sha256").write_text(hashlib.sha256(b"agent-secret-value").hexdigest())
    home.hub.cfg.auth.verifier_dir = str(tmp_path)
    s = Served(home.hub, home.gw, insecure=False)
    yield s
    s.stop()


class Sse:
    """Background SSE reader: collects (id, event, raw data line) frames."""

    def __init__(self, url, headers, path="/events"):
        self.frames: list[dict] = []
        self.raw: list[str] = []
        self.status = None
        self.hello = threading.Event()
        self._stop = False
        threading.Thread(target=self._run, args=(url + path, headers), daemon=True).start()
        end = time.time() + 5
        while self.status is None and time.time() < end:
            time.sleep(0.01)

    def _run(self, url, headers):
        try:
            with httpx.stream("GET", url, headers=headers, timeout=30) as r:
                self.status = r.status_code
                if r.status_code != 200:
                    self.raw.append(r.read().decode())
                    return
                cur: dict = {}
                for line in r.iter_lines():
                    if self._stop:
                        return
                    self.raw.append(line)
                    if line.startswith("id:"):
                        cur["id"] = int(line[3:])
                    elif line.startswith("event:"):
                        cur["event"] = line[6:].strip()
                    elif line.startswith("data:"):
                        cur["data"] = json.loads(line[5:])
                    elif line == "" and cur:
                        if cur.get("event") == "hello":
                            self.hello.set()
                        else:
                            self.frames.append(cur)
                        cur = {}
        except httpx.HTTPError:
            self.status = self.status or -1

    def types(self):
        return [f["event"] for f in self.frames]

    def wait(self, pred, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred(self):
                return True
            time.sleep(0.02)
        return False

    def close(self):
        self._stop = True


def make_noise(home):
    """Events of every kind, including ones that must never reach the agent."""
    home.hub.admin.patch_endpoint(home.ids["light"], {"name": "Renamed"}, "admin")               # registry_changed
    home.hub.scenes.create_scene("Noise", [{"target": {"room": "Bedroom"}, "capability": "power", "value": False}], "u", "t")  # scenes_changed
    sess = home.hub.admin.create_provision_session({"node_id": N5, "wifi": WIFI}, "admin")       # secrets in the bundle
    m = sess["bundle"]["mqtt"]
    from device_hub.mock import FakeNode
    FakeNode(make_announce(N5, [light_ep()]), home.broker.client(m["username"], m["password"], N5, FakeNode.will(N5))).start()  # pending_node, node_status
    home.broker.flush()
    # a hypothetical future event that carries credential material: default-deny must keep it away from the agent
    home.hub.bus.publish("provision_session_created", {"bundle": sess["bundle"]})
    return sess


def test_agent_gets_only_the_allowlist_and_never_credentials(served, home):
    agent = Sse(served.url, AG)
    gw = Sse(served.url, GW)
    assert agent.status == 200 and gw.status == 200 and agent.hello.wait(3) and gw.hello.wait(3)
    sess = make_noise(home)
    assert agent.wait(lambda s: {"registry_changed", "node_status"} <= set(s.types()))
    assert gw.wait(lambda s: {"registry_changed", "scenes_changed", "pending_node", "node_status"} <= set(s.types()))
    time.sleep(0.6)                                                       # anything wrongly delivered would be here by now
    assert set(agent.types()) <= AGENT_OK, agent.types()
    assert not {"scenes_changed", "pending_node", "provision_session_created"} & set(agent.types())
    blob = "\n".join(agent.raw)
    for secret in (sess["bundle"]["mqtt"]["password"], WIFI["password"], WIFI["ssid"], "BEGIN CERTIFICATE", sess["session_id"], "bundle", "ntp_host", "Noise"):
        assert secret not in blob, f"agent stream leaked {secret[:6]}"
    # the gateway still gets everything it is entitled to (every known type), without the unknown future one
    assert {"registry_changed", "scenes_changed", "pending_node", "node_status"} <= set(gw.types())
    assert "provision_session_created" not in gw.types()
    agent.close(), gw.close()


def test_replay_is_filtered_the_same_way(served, home):
    make_noise(home)                                      # fills the ring buffer with everything
    agent = Sse(served.url, {**AG, "Last-Event-ID": "0"})
    gw = Sse(served.url, {**GW, "Last-Event-ID": "0"})
    assert agent.wait(lambda s: "registry_changed" in s.types())
    assert gw.wait(lambda s: "scenes_changed" in s.types() and "pending_node" in s.types())
    time.sleep(0.4)
    assert set(agent.types()) <= AGENT_OK and "registry_changed" in agent.types()
    blob = "\n".join(agent.raw)
    assert "scenes_changed" not in blob and "pending_node" not in blob and "provision_session_created" not in blob
    # ?since= works the same way
    a2 = Sse(served.url, AG, "/events?since=0")
    assert a2.wait(lambda s: "registry_changed" in s.types()) and set(a2.types()) <= AGENT_OK
    agent.close(), gw.close(), a2.close()


def test_sse_frame_format_is_the_contract_envelope(served, home):
    agent = Sse(served.url, AG)
    assert agent.hello.wait(3)
    home.hub.admin.patch_endpoint(home.ids["fan"], {"name": "Fan 2"}, "admin")
    assert agent.wait(lambda s: s.frames)
    f = agent.frames[0]
    assert set(f) == {"id", "event", "data"} and f["id"] == f["data"]["seq"] and f["event"] == f["data"]["type"]
    assert set(f["data"]) == {"seq", "type", "ts", "data"} and isinstance(f["data"]["ts"], int)
    assert f["data"]["data"]["reason"] == "patch" and home.ids["fan"] in f["data"]["data"]["endpoint_ids"]
    agent.close()


def test_types_param_for_the_agent(served, home):
    h = lambda p: httpx.get(served.url + p, headers=AG, timeout=5)  # noqa: E731
    for bad in ("scenes_changed", "pending_node", "registry_changed,scenes_changed", "scenes_changed,state"):
        r = h(f"/events?types={bad}")
        assert r.status_code == 403 and r.json()["error"] == "forbidden_event_type", bad
    assert h("/events?types=bogus").status_code == 400
    assert h("/events?types=registry_changed,bogus").status_code == 400
    assert h("/events?since=abc").status_code == 400
    # state is opt-in and allowed
    st = Sse(served.url, AG, "/events?types=state")
    assert st.status == 200 and st.hello.wait(3)
    default = Sse(served.url, AG)
    assert default.hello.wait(3)
    home.hub.executor.set_state(home.ids["light"], "brightness", 21)
    assert st.wait(lambda s: "state" in s.types())
    time.sleep(0.4)
    assert "state" not in default.types(), "state is not in the agent's default set"
    assert st.frames[0]["data"]["data"]["values"]["brightness"] == 21
    st.close(), default.close()


def test_gateway_keeps_every_type_and_the_type_filter(served, home):
    for t in ("registry_changed", "scenes_changed", "node_status", "pending_node", "state", "action_settled", "action_settle_timeout"):
        s = Sse(served.url, GW, f"/events?types={t}")
        assert s.status == 200, t
        s.close()
    assert httpx.get(served.url + "/events?types=bogus", headers=GW).status_code == 400


def test_agent_cannot_call_any_admin_or_other_route(served, home):
    routes = [("GET", "/admin/nodes"), ("POST", "/admin/provision-sessions"), ("GET", "/admin/provision-sessions"),
              ("GET", "/admin/audit"), ("GET", "/admin/rejections"), ("DELETE", "/admin/nodes/esp32-000000000001"),
              ("POST", "/admin/nodes/esp32-000000000001/approve"), ("POST", "/admin/nodes/esp32-000000000001/identify"),
              ("POST", "/admin/nodes/esp32-000000000001/unprovision"), ("PATCH", f"/admin/endpoints/{home.ids['light']}"),
              ("POST", f"/admin/endpoints/{home.ids['light']}/rebind"), ("GET", "/devices"), ("GET", f"/devices/{home.ids['light']}"),
              ("POST", f"/devices/{home.ids['light']}/set"), ("POST", f"/devices/{home.ids['light']}/action"),
              ("GET", "/scenes"), ("POST", "/scenes"), ("GET", "/scenes/sc_00000000"), ("PUT", "/scenes/sc_00000000"),
              ("DELETE", "/scenes/sc_00000000"), ("POST", "/scenes/sc_00000000/run"), ("GET", "/scenes/sc_00000000/confirmation"),
              ("GET", "/registry/version"), ("GET", "/rooms"), ("POST", "/mock/mint")]
    for m, p in routes:
        r = httpx.request(m, served.url + p, headers=AG, json={}, timeout=5)
        assert r.status_code == 403, (m, p, r.status_code)
    assert httpx.post(served.url + "/events", headers=AG, json={}).status_code == 403      # only GET
    assert httpx.get(served.url + "/events").status_code == 401


# ---------------------------------------------------------------------------------------------- call_id echo
def mcp_call(served, name, args, meta, secret="agent-secret-value"):
    async def go():
        async with streamablehttp_client(served.url + "/mcp", headers={"X-Service-Secret": secret}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return json.loads((await s.call_tool(name, args, meta=meta)).content[0].text)

    return asyncio.run(go())


def test_call_id_is_echoed_on_settle_events_over_the_agent_stream(served, curtain):  # noqa: F811
    home, node = curtain
    agent = Sse(served.url, AG)
    assert agent.hello.wait(3)
    cid = mcp_call(served, "set_state", {"endpoint_id": home.ids["curtain"], "capability": "position", "value": 70}, {"call_id": "call_42"})
    assert cid["status"] == "moving"
    assert agent.wait(lambda s: "action_settled" in s.types())
    d = next(f for f in agent.frames if f["event"] == "action_settled")["data"]["data"]
    assert d["call_id"] == "call_42" and d["observed"] == 70 and d["value"] == 70 and d["endpoint_id"] == home.ids["curtain"]
    # timeout variant
    node.stall = True
    node.announce = make_announce("esp32-000000000003", [{"local_id": "c1", "type": "cover", "capabilities": [
        {"id": "position", "kind": "actuator", "type": "int", "min": 0, "max": 100, "unit": "%", "boot_value": 0, "settle_s": 1}]}])
    node.publish_announce()
    home.broker.flush()
    mcp_call(served, "set_state", {"endpoint_id": home.ids["curtain"], "capability": "position", "value": 10}, {"call_id": "call_43"})
    assert agent.wait(lambda s: "action_settle_timeout" in s.types(), 6)
    t = next(f for f in agent.frames if f["event"] == "action_settle_timeout")["data"]["data"]
    assert t["call_id"] == "call_43" and t["settle_s"] == 1 and t["observed"] is None
    agent.close()


def test_call_id_truncated_ignored_when_not_a_string_and_optional(served, curtain):  # noqa: F811
    home, node = curtain
    agent = Sse(served.url, AG)
    assert agent.hello.wait(3)
    args = {"endpoint_id": home.ids["curtain"], "capability": "position"}
    mcp_call(served, "set_state", {**args, "value": 20}, {"call_id": "x" * 100})
    assert agent.wait(lambda s: len(s.frames) >= 1)
    assert agent.frames[-1]["data"]["data"]["call_id"] == "x" * 64
    mcp_call(served, "set_state", {**args, "value": 40}, {"call_id": 12345})
    assert agent.wait(lambda s: len(s.frames) >= 2)
    assert "call_id" not in agent.frames[-1]["data"]["data"]
    mcp_call(served, "set_state", {**args, "value": 60}, None)
    assert agent.wait(lambda s: len(s.frames) >= 3)
    assert "call_id" not in agent.frames[-1]["data"]["data"]
    agent.close()


def test_run_scene_moving_actions_echo_call_id(served, curtain):  # noqa: F811
    home, node = curtain
    home.hub.scenes.create_scene("Open", [{"target": {"endpoint_id": home.ids["curtain"]}, "capability": "position", "value": 100},
                                          {"target": {"endpoint_id": home.ids["light"]}, "capability": "power", "value": True}], "u", "t")
    agent = Sse(served.url, AG)
    assert agent.hello.wait(3)
    r = mcp_call(served, "run_scene", {"scene": "Open"}, {"call_id": "scene_call"})
    assert {x["status"] for x in r["results"]} == {"moving", "done"}
    assert agent.wait(lambda s: "action_settled" in s.types())
    assert agent.frames[-1]["data"]["data"]["call_id"] == "scene_call"
    agent.close()


def test_call_id_never_affects_decisions(served, home):
    """call_id plays no part in authorization or confirmation: a risk:confirm action is refused with any call_id."""
    r = mcp_call(served, "set_state", {"endpoint_id": home.ids["lock"], "capability": "lock", "value": "unlocked"},
                 {"call_id": "trusted-i-am", "user_id": "user:v"})
    assert r["error"] == "confirmation_required"
