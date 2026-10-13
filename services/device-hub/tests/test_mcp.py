"""MCP surface: exactly five tools, nothing that manages devices, confirmation token only via _meta."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from device_hub.mcp_server import TOOL_NAMES, TOOLS, build_mcp_server
from device_hub.util import set_state_action

U = "user:vedant"
ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def call(home, name, args, meta=None):
    async def go():
        server = build_mcp_server(home.hub.executor, home.hub.scenes)
        async with connect(server) as client:
            r = await client.call_tool(name, args, meta=meta)
            return r

    return run(go())


def payload(r):
    assert r.content, r
    return json.loads(r.content[0].text)


def test_exactly_the_five_tools_of_the_contract(home):
    async def go():
        async with connect(build_mcp_server(home.hub.executor, home.hub.scenes)) as c:
            return (await c.list_tools()).tools

    tools = run(go())
    assert sorted(t.name for t in tools) == sorted(["list_devices", "get_state", "set_state", "run_scene", "read_sensor"])
    assert len(tools) == 5 and TOOL_NAMES == ("list_devices", "get_state", "set_state", "run_scene", "read_sensor")


def test_tool_list_is_the_same_before_and_after_devices_change(home):
    async def names():
        async with connect(build_mcp_server(home.hub.executor, home.hub.scenes)) as c:
            return [t.model_dump() for t in (await c.list_tools()).tools]

    before = run(names())
    home.hub.admin.forget_node("esp32-000000000001", confirm=True, force=False, actor="t")
    assert run(names()) == before


def test_no_tool_schema_mentions_confirmation_or_admin_things():
    text = json.dumps([t.model_dump() for t in TOOLS]).lower()
    for word in ("confirm", "token", "provision", "forget", "rebind", "mqtt", "password", "secret", "approve"):
        assert word not in text, word


def test_schemas_reject_extra_arguments(home):
    r = call(home, "set_state", {"endpoint_id": home.ids["light"], "capability": "power", "value": True,
                                 "confirmation_token": "A" * 43})
    assert r.isError
    r = call(home, "set_state", {"endpoint_id": home.ids["light"], "capability": "power", "value": True, "confirmed": True})
    assert r.isError
    assert home.nodes["esp32-000000000001"].cmds_seen == []


@pytest.mark.parametrize("name,args", [
    ("set_state", {"endpoint_id": "light", "capability": "power", "value": True}),
    ("set_state", {"endpoint_id": "ep_00000000", "capability": "power"}),
    ("set_state", {"endpoint_id": "ep_00000000", "capability": "nope", "value": 1}),
    ("set_state", {"endpoint_id": "ep_00000000", "capability": "power", "value": {"x": 1}}),
    ("get_state", {}),
    ("get_state", {"endpoint_id": "ep_00000000", "room": "x"}),
    ("run_scene", {}),
    ("run_scene", {"scene": "x" * 100}),
    ("read_sensor", {"endpoint_id": "ep_00000000", "capability": "power"}),
    ("list_devices", {"type": "toaster"}),
    ("approve_node", {}), ("forget_node", {"node_id": "esp32-000000000001"}), ("add_device", {}), ("update_risk", {}),
])
def test_bad_arguments_and_unknown_tools_are_errors(home, name, args):
    r = call(home, name, args)
    assert r.isError


def test_set_state_over_mcp(home):
    r = call(home, "set_state", {"endpoint_id": home.ids["light"], "capability": "brightness", "value": 42})
    assert payload(r)["ok"] is True and home.nodes["esp32-000000000001"].values["l1"]["brightness"] == 42
    r = call(home, "set_state", {"endpoint_id": home.ids["light"], "capability": "brightness", "value": 420})
    assert payload(r)["error"] == "out_of_range"


def test_read_tools_over_mcp(home):
    assert len(payload(call(home, "list_devices", {}))["devices"]) == 4
    assert payload(call(home, "get_state", {"room": "Bedroom"}))["states"]
    assert payload(call(home, "read_sensor", {"endpoint_id": home.ids["temp"], "capability": "temperature"}))["capability"] == "temperature"


def test_token_in_meta_confirms_and_user_comes_from_meta(home):
    args = {"endpoint_id": home.ids["lock"], "capability": "lock", "value": "unlocked"}
    r = payload(call(home, "set_state", args, meta={"user_id": U}))
    assert r["error"] == "confirmation_required"
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "unlocked"))
    r = payload(call(home, "set_state", args, meta={"confirmation_token": token, "user_id": U}))
    assert r["ok"] is True
    assert home.nodes["esp32-000000000002"].cmds_seen[-1]["by"] == U


def test_token_in_meta_without_matching_user_is_denied(home):
    args = {"endpoint_id": home.ids["lock"], "capability": "lock", "value": "unlocked"}
    token = home.token(U, set_state_action(home.ids["lock"], "lock", "unlocked"))
    assert payload(call(home, "set_state", args, meta={"confirmation_token": token, "user_id": "user:eve"}))["ok"] is False
    assert payload(call(home, "set_state", args, meta={"confirmation_token": token}))["ok"] is False
    assert payload(call(home, "set_state", args, meta={"confirmation_token": 12345, "user_id": U}))["ok"] is False


def test_run_scene_over_mcp_needs_confirmation_then_runs(home):
    home.hub.scenes.create_scene("Leave", [{"target": {"endpoint_id": home.ids["lock"]}, "capability": "lock", "value": "locked"},
                                           {"target": {"endpoint_id": home.ids["light"]}, "capability": "power", "value": False}], "u", "t")
    r = payload(call(home, "run_scene", {"scene": "Leave"}, meta={"user_id": U}))
    assert r["status"] == "needs_confirmation" and r["executed"] is False
    h = home.hub.scenes.action_for_scene("Leave")["action_hash"]
    r = payload(call(home, "run_scene", {"scene": "Leave"}, meta={"user_id": U, "confirmation_token": home.gw.mint(U, h)}))
    assert r["executed"] is True and r["ok"] is True


def test_mcp_module_cannot_reach_admin_code():
    """Static guard: the MCP module imports neither the admin service, provisioning, dynsec nor registry writers."""
    tree = ast.parse((ROOT / "device_hub" / "mcp_server.py").read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level:
            imported.add(n.module)
    assert imported <= {"contracts", "executor", "scenes"}, imported
