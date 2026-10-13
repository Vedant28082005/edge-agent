"""MCP server: EXACTLY the five runtime tools of contracts/mcp-tools.md and nothing else.

Nothing here can add, remove, rename, rebind or re-provision a device, change a risk level, touch scenes/users or MQTT
credentials: this module only receives the Executor and SceneService, never AdminService or the registry's writers.
The confirmation token travels out-of-band in the MCP request `_meta` (plus `_meta.user_id`, the user the executor acts
for); it is not in any inputSchema and a token found in the model's own arguments is refused and never used.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from .contracts import ENDPOINT_TYPES, VOCAB
from .executor import Executor
from .scenes import SceneService

log = logging.getLogger("device_hub.mcp")

TOOL_NAMES = ("list_devices", "get_state", "set_state", "run_scene", "read_sensor")

_EP = {"type": "string", "pattern": "^ep_[0-9a-f]{8}$"}
_ACTUATORS = sorted(k for k, v in VOCAB.items() if v["kind"] == "actuator")
_SENSORS = sorted(k for k, v in VOCAB.items() if v["kind"] == "sensor")

TOOLS: list[types.Tool] = [
    types.Tool(
        name="list_devices",
        description="List controllable devices (endpoints) with room, type, online status, risk level and capabilities. "
                    "No live values; use get_state for those.",
        inputSchema={"type": "object", "additionalProperties": False, "properties": {
            "room": {"type": "string", "maxLength": 48},
            "type": {"type": "string", "enum": list(ENDPOINT_TYPES)}}}),
    types.Tool(
        name="get_state",
        description="Current values of one device (endpoint_id) or of all devices in a room. Give exactly one of them.",
        inputSchema={"type": "object", "additionalProperties": False, "properties": {
            "endpoint_id": _EP, "room": {"type": "string", "maxLength": 48}},
            "oneOf": [{"required": ["endpoint_id"]}, {"required": ["room"]}]}),
    types.Tool(
        name="set_state",
        description="Set one capability of one device to a desired value (never a toggle). Validated against the device "
                    "registry; waits for the device's acknowledgement.",
        inputSchema={"type": "object", "additionalProperties": False,
                     "required": ["endpoint_id", "capability", "value"], "properties": {
                         "endpoint_id": _EP, "capability": {"type": "string", "enum": _ACTUATORS},
                         "value": {"oneOf": [{"type": "boolean"}, {"type": "number"}, {"type": "string", "maxLength": 24}]}}}),
    types.Tool(
        name="run_scene",
        description="Run a saved scene by id or exact name. Offline devices are skipped and reported, never queued.",
        inputSchema={"type": "object", "additionalProperties": False, "required": ["scene"], "properties": {
            "scene": {"type": "string", "minLength": 1, "maxLength": 48}}}),
    types.Tool(
        name="read_sensor",
        description="Last reported value of a sensor capability with its timestamp and a staleness flag.",
        inputSchema={"type": "object", "additionalProperties": False, "required": ["endpoint_id", "capability"], "properties": {
            "endpoint_id": _EP, "capability": {"type": "string", "enum": _SENSORS}}}),
]


def meta_fields(meta: Any) -> tuple[str | None, str | None]:
    """(confirmation_token, user_id) from the request `_meta`. Anything not a string is ignored."""
    if meta is None:
        return None, None
    extra = getattr(meta, "model_extra", None) or {}

    def get(k: str) -> str | None:
        v = extra.get(k, getattr(meta, k, None))
        return v if isinstance(v, str) else None

    return get("confirmation_token"), get("user_id")


def dispatch(executor: Executor, scenes: SceneService, name: str, args: dict, token: str | None,
             user_id: str | None) -> dict:
    if name == "list_devices":
        return executor.list_devices(args.get("room"), args.get("type"))
    if name == "get_state":
        return executor.get_state(args.get("endpoint_id"), args.get("room"))
    if name == "read_sensor":
        return executor.read_sensor(args["endpoint_id"], args["capability"])
    if name == "set_state":
        return executor.set_state(args["endpoint_id"], args["capability"], args["value"], user_id=user_id, token=token)
    if name == "run_scene":
        return scenes.run_scene(args["scene"], user_id=user_id, token=token)
    raise ValueError(f"unknown tool {name}")


def build_mcp_server(executor: Executor, scenes: SceneService) -> Server:
    server: Server = Server("device-hub", instructions="Runtime smart-home tools. Five tools, fixed list.")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return list(TOOLS)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> dict:
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown tool {name}")
        if "confirmation_token" in arguments:        # unreachable (additionalProperties:false) but defence in depth
            log.warning("confirmation_token in tool arguments refused (tool=%s)", name)
            raise ValueError("confirmation_token is not an argument")
        try:
            meta = server.request_context.meta
        except LookupError:
            meta = None
        token, user_id = meta_fields(meta)
        return await asyncio.to_thread(dispatch, executor, scenes, name, arguments, token, user_id)

    return server
