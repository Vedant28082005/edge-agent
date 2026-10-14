"""Integration with the REAL device-hub from the S1 worktree (branch s1-device-hub) in its --mock mode (in-memory broker
and nodes, no network but 127.0.0.1). Caller auth is ON: the agent uses a generated agent secret whose SHA-256 verifier is
the only thing the hub holds. Skipped (not passed) when the worktree or the hub's dependencies are missing."""
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import collect, names, one
from agent.config import AgentConfig
from agent.mock import ScriptedLLM
from agent.service import Agent

pytestmark = pytest.mark.integration

HUB_DIR = Path(os.environ.get("EDGE_HUB_DIR", Path(__file__).resolve().parents[2] / "device-hub"))   # merged S1 hub in this repo


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def hub(tmp_path):
    if not (HUB_DIR / "device_hub").is_dir():
        pytest.skip(f"S1 device-hub worktree not found at {HUB_DIR}")
    try:
        import mcp, paho  # noqa: F401
    except ImportError:
        pytest.skip("device-hub dependencies (mcp, paho-mqtt) not installed in this venv")
    agent_secret, gw_secret = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    vdir = tmp_path / "verifiers"
    vdir.mkdir()
    (vdir / "agent.sha256").write_text(hashlib.sha256(agent_secret.encode()).hexdigest())
    (vdir / "gateway.sha256").write_text(hashlib.sha256(gw_secret.encode()).hexdigest())
    port = free_port()
    # The S1 CLI cannot run --mock with caller auth ON (build_mock_hub ignores --config; see README), so drive its public
    # pieces the same way __main__ does, with the verifier directory set.
    driver = tmp_path / "run_hub.py"
    nl = chr(10)
    driver.write_text(nl.join([
        "import sys, uvicorn",
        f"sys.path.insert(0, {str(HUB_DIR)!r})",
        "from device_hub.mock import build_mock_hub",
        "from device_hub.api import create_app",
        f"hub, broker, gw, nodes = build_mock_hub(':memory:', {port})",
        f"hub.cfg.auth.verifier_dir = {str(vdir)!r}",
        "app = create_app(hub, mock_gateway=gw, insecure_no_auth=False)",
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_config=None, access_log=False)", ""]), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(driver)], cwd=str(HUB_DIR), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)       # our own child: terminated by handle below
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(url + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            proc.terminate()
            pytest.skip("hub did not start")
        yield url, agent_secret, gw_secret
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def make_agent(tmp_path, url, agent_secret):
    sf = tmp_path / "agent.secret"
    sf.write_text(agent_secret)
    cfg = AgentConfig(hub_url=url, hub_secret_file=str(sf), db_path=":memory:", confirmation_ttl_s=10, settle_wait_s=5)
    agent = Agent.from_config(cfg, ScriptedLLM())
    await agent.start()
    return agent


async def test_real_hub_end_to_end(tmp_path, hub):
    url, asec, gsec = hub
    agent = await make_agent(tmp_path, url, asec)
    try:
        assert len(agent.registry.devices) >= 3
        by_name = {d.name: d for d in agent.registry.devices.values()}
        assert "Fan" in by_name and "Front door" in by_name and by_name["Front door"].risk == "confirm"
        assert agent.builder.prefix_version >= 1 and by_name["Fan"].endpoint_id in agent.builder.prefix

        # 1. the headline: text -> LLM -> set_state on the real hub (through its mock broker and fake node)
        ctx = agent.new_request("alice", "s1", "turn on the bedroom fan")
        ev = await collect(ctx)
        res = one(ev, "tool_result")
        assert res["ok"] is True and res["result"]["status"] == "done" and res["result"]["verified"] is True, res
        assert names(ev)[-1] == "final"

        # 2. read tool + answer
        ctx = agent.new_request("alice", "s1", "what is the temperature in the bedroom")
        ev = await collect(ctx)
        assert "degrees" in one(ev, "final")["text"] or "temperature" in json.dumps(ev)

        # 3. risk:confirm through the real hub: token minted by the (mock) gateway, consumed by the hub
        ctx = agent.new_request("alice", "s1", "unlock the front door")
        task = asyncio.create_task(collect(ctx))
        for _ in range(100):
            if agent.confirmations.pending_ids():
                break
            await asyncio.sleep(0.05)
        rid = agent.confirmations.pending_ids()[0]
        action = agent.confirmations.pending_action(rid)
        async with httpx.AsyncClient() as c:
            hdr = {"X-Service-Secret": gsec}
            canon = (await c.post(f"{url}/devices/{action['endpoint_id']}/action", headers=hdr,
                                  json={"capability": action["capability"], "value": action["value"]})).json()
            minted = (await c.post(f"{url}/mock/mint", headers=hdr, json={"user_id": "alice", "action": action})).json()
        agent.confirmations.resolve(rid, "alice", True, minted["confirmation_token"])
        ev = await task
        req = one(ev, "confirmation_request")
        assert req["action_hash"] == canon["action_hash"] == minted["action_hash"]       # our JCS hash == the hub's
        tr = [d for n, d in ev if n == "tool_result"][-1]
        assert tr["ok"] is True and tr["result"]["verified"] is True, tr
        assert minted["confirmation_token"] not in json.dumps(ev)

        # 4. a token for another user does not work (hub -> gateway checks the user binding)
        ctx = agent.new_request("alice", "s1", "lock the front door")
        task = asyncio.create_task(collect(ctx))
        for _ in range(100):
            if agent.confirmations.pending_ids():
                break
            await asyncio.sleep(0.05)
        rid = agent.confirmations.pending_ids()[0]
        async with httpx.AsyncClient() as c:
            m2 = (await c.post(f"{url}/mock/mint", headers={"X-Service-Secret": gsec},
                               json={"user_id": "mallory", "action": agent.confirmations.pending_action(rid)})).json()
        agent.confirmations.resolve(rid, "alice", True, m2["confirmation_token"])
        ev = await task
        assert one(ev, "confirmation_expired")["reason"] == "token_expired"

        # 5. registry change reaches the agent through the real /events stream
        async with httpx.AsyncClient() as c:
            r = await c.patch(f"{url}/admin/endpoints/{by_name['Fan'].endpoint_id}", headers={"X-Service-Secret": gsec},
                              json={"name": "Ventilator"})
            assert r.status_code == 200
        for _ in range(100):
            if "Ventilator" in agent.builder.prefix:
                break
            await asyncio.sleep(0.05)
        assert "Ventilator" in agent.builder.prefix and agent.stream.last_id is not None

        # 7. a scene with a risk:confirm action: scene_id and canonical list/hash come from the hub; approval via the HTTP hand-off
        from agent.app import create_app
        from agent.auth import ApiAuth
        from agent.util import action_hash
        door, fan = by_name["Front door"].endpoint_id, by_name["Fan"].endpoint_id
        async with httpx.AsyncClient() as c:
            sc = (await c.post(f"{url}/scenes", headers={"X-Service-Secret": gsec}, json={
                "name": "Good night", "owner": "alice", "actions": [
                    {"target": {"endpoint_id": fan}, "capability": "power", "value": False},
                    {"target": {"endpoint_id": door}, "capability": "lock", "value": "locked"}]})).json()
            sid = sc["scene_id"]
            canon = (await c.get(f"{url}/scenes/{sid}/confirmation", headers={"X-Service-Secret": gsec})).json()
        ctx = agent.new_request("alice", "s1", "run the good night scene")
        task = asyncio.create_task(collect(ctx))
        for _ in range(100):
            if agent.confirmations.pending_ids():
                break
            await asyncio.sleep(0.05)
        rid = agent.confirmations.pending_ids()[0]
        action = agent.confirmations.pending_action(rid)
        assert action == canon["action"] and agent.confirmations.pending_hash(rid, "alice") == canon["action_hash"] == action_hash(action)
        assert not [n for n in agent.store.action_log() if n["status"] == "done" and n["tool"] == "run_scene"]   # nothing ran yet
        async with httpx.AsyncClient() as c:
            tok = (await c.post(f"{url}/mock/mint", headers={"X-Service-Secret": gsec}, json={"user_id": "alice", "action": action})).json()["confirmation_token"]
        auth = ApiAuth.from_secret("gw-test-secret", str(tmp_path / "v.sha256"))
        api = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(agent, auth)), base_url="http://agent",
                                headers={"X-Service-Secret": "gw-test-secret"})
        body = {"decision": "approved", "user_id": "alice", "role": "member", "action": action,
                "action_hash": canon["action_hash"], "confirmation_token": tok}
        assert (await api.post(f"/v1/confirmations/{rid}", json=body)).status_code == 202
        ev = await task
        last = [d for n, d in ev if n == "tool_result"][-1]
        assert last["ok"] is True and last["result"]["executed"] is True, last
        assert tok not in json.dumps(ev)

        # 6. scope: the agent secret cannot do anything but /mcp and allowlisted /events
        async with httpx.AsyncClient() as c:
            ah = {"X-Service-Secret": asec}
            assert (await c.get(f"{url}/devices", headers=ah)).status_code == 403
            assert (await c.get(f"{url}/admin/nodes", headers=ah)).status_code == 403
            assert (await c.get(f"{url}/events", headers=ah, params={"types": "pending_node"})).status_code == 403
    finally:
        await agent.stop()
