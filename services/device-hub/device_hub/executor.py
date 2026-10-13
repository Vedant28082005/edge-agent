"""Runtime executor: the only code path that turns a request into a device command (set_state, scenes, identify).

Order of checks in set_state: registry validation -> confirmation (consume the token LAST, so an invalid action never
burns an approval) -> publish cmd -> wait for ack (3 s) -> read back and compare.
"""
from __future__ import annotations

import logging
from typing import Any

from .bridge import MqttBridge
from .config import HubConfig
from .contracts import coerce_value, values_equal
from .errors import HubError, NotFound
from .gateway_client import GatewayClient
from .registry import Registry
from .transport import TransportDown
from .util import BY_RE, Clock, ENDPOINT_ID_RE, action_hash, safe_repr, sanitize_text, set_state_action

log = logging.getLogger("device_hub.executor")

DEVICE_PASSTHROUGH = {"unknown_endpoint", "unknown_capability", "out_of_range", "bad_type"}


def make_by(user_id: str | None, scene_id: str | None = None) -> str:
    """cmd `by` field (cmd.schema.json): scene:<id>, user:<name> or agent."""
    if scene_id:
        return f"scene:{scene_id}"
    if user_id:
        if BY_RE.match(user_id):
            return user_id
        name = "".join(ch for ch in sanitize_text(user_id, 64) if ch.isalnum() or ch in "_.-")
        if name:
            return f"user:{name}"[:69]
    return "agent"


class Executor:
    def __init__(self, registry: Registry, bridge: MqttBridge, gateway: GatewayClient, cfg: HubConfig,
                 clock: Clock) -> None:
        self.registry = registry
        self.bridge = bridge
        self.gateway = gateway
        self.cfg = cfg
        self.clock = clock

    # ------------------------------------------------------------------------------------------ queries
    def list_devices(self, room: str | None = None, etype: str | None = None) -> dict:
        if etype is not None and etype not in self.registry.endpoint_types():
            return {"devices": [], "ok": False, "error": "bad_request"}
        devices = []
        for ep in self.registry.list_endpoints(room=room, etype=etype, include_disabled=False):
            devices.append({
                "endpoint_id": ep["endpoint_id"], "name": ep["name"], "room": ep["room"], "type": ep["type"],
                "online": ep["online"], "risk": ep["risk"],
                "capabilities": [{k: v for k, v in c.items() if k in ("id", "kind", "type", "min", "max", "values", "unit")}
                                 for c in ep["capabilities"]],
            })
        return {"devices": devices}

    def get_state(self, endpoint_id: str | None = None, room: str | None = None) -> dict:
        if (endpoint_id is None) == (room is None):
            return {"ok": False, "error": "bad_request", "states": []}
        if endpoint_id is not None:
            try:
                eps = [self.registry.get_endpoint(endpoint_id)]
            except NotFound:
                return {"ok": False, "error": "unknown_endpoint", "states": []}
            if not eps[0]["enabled"]:
                return {"ok": False, "error": "disabled", "states": []}
        else:
            eps = self.registry.list_endpoints(room=room, include_disabled=False)
            if not eps and room.lower() not in [r.lower() for r in self.registry.rooms()]:
                return {"ok": False, "error": "unknown_room", "states": []}
        states = []
        for ep in eps:
            values, ts = self.registry.values_for(ep["node_id"], ep["local_id"], ep["capabilities"])
            states.append({"endpoint_id": ep["endpoint_id"], "online": ep["online"], "values": values, "ts": ts})
        return {"states": states}

    def read_sensor(self, endpoint_id: str, capability: str) -> dict:
        try:
            ep = self.registry.get_endpoint(endpoint_id)
        except NotFound:
            return {"ok": False, "error": "unknown_endpoint", "endpoint_id": endpoint_id}
        cap = next((c for c in ep["capabilities"] if c["id"] == capability), None)
        if cap is None:
            return {"ok": False, "error": "unknown_capability", "endpoint_id": endpoint_id, "capability": capability}
        if cap["kind"] != "sensor":
            return {"ok": False, "error": "not_sensor", "endpoint_id": endpoint_id, "capability": capability}
        values, ts = self.registry.values_for(ep["node_id"], ep["local_id"], ep["capabilities"])
        value = values.get(capability)
        interval = cap.get("interval_s", 0) or 0
        stale = (not ep["online"]) or value is None or ts == 0 \
            or (interval > 0 and self.clock.now() - ts > self.cfg.stale_factor * interval)
        return {"endpoint_id": endpoint_id, "capability": capability, "value": value, "unit": cap.get("unit"),
                "ts": ts, "stale": bool(stale)}

    # ------------------------------------------------------------------------------------------ validation
    @staticmethod
    def check_action(ep: dict, capability: Any, value: Any) -> tuple[dict | None, Any, str | None]:
        """Registry-level validation of one action on one endpoint view. Returns (cap, coerced_value, error_code)."""
        if not ep["enabled"]:
            return None, value, "disabled"
        cap = next((c for c in ep["capabilities"] if c["id"] == capability), None) if isinstance(capability, str) else None
        if cap is None:
            return None, value, "unknown_capability"
        if cap["kind"] != "actuator":
            return cap, value, "not_actuator"
        ok, code, coerced = coerce_value(cap, value)
        if not ok:
            return cap, value, code
        return cap, coerced, None

    # ------------------------------------------------------------------------------------------ set_state
    def set_state(self, endpoint_id: Any, capability: Any, value: Any, user_id: str | None = None,
                  token: str | None = None) -> dict:
        res = self._set_state(endpoint_id, capability, value, user_id, token)
        self.registry.audit(make_by(user_id), "device.set_state", str(endpoint_id)[:20],
                            {"capability": safe_repr(capability, 24), "value": safe_repr(value, 24),
                             "result": "ok" if res.get("ok") else res.get("error")},
                            "ok" if res.get("ok") else "denied")
        return res

    def _fail(self, endpoint_id: Any, capability: Any, value: Any, error: str, detail: str | None = None) -> dict:
        out = {"ok": False, "endpoint_id": endpoint_id, "capability": capability, "value": value, "error": error}
        if detail:
            out["detail"] = detail
        return out

    def _set_state(self, endpoint_id: Any, capability: Any, value: Any, user_id: str | None, token: str | None) -> dict:
        if not isinstance(endpoint_id, str) or not ENDPOINT_ID_RE.match(endpoint_id):
            return self._fail(endpoint_id, capability, value, "unknown_endpoint")
        try:
            ep = self.registry.get_endpoint(endpoint_id)
        except NotFound:
            return self._fail(endpoint_id, capability, value, "unknown_endpoint")
        cap, coerced, err = self.check_action(ep, capability, value)
        if err:
            return self._fail(endpoint_id, capability, value, err)
        if not ep["online"]:
            return self._fail(endpoint_id, capability, value, "offline")
        if ep["risk"] == "confirm":
            if not self.confirm_ok(set_state_action(endpoint_id, capability, value), user_id, token):
                return self._fail(endpoint_id, capability, value, "confirmation_required")
        return self.apply(ep, cap, coerced, make_by(user_id), value)

    def confirm_ok(self, action: dict, user_id: str | None, token: str | None) -> bool:
        """Validate+consume a confirmation token for exactly `action` (hash computed here from our own view)."""
        if not token or not user_id:
            return False
        res = self.gateway.consume(token, user_id, action_hash(action))
        self.registry.audit(make_by(user_id), "confirmation.consume", action.get("type", ""),
                            {"granted": res.granted, "reason": res.reason, "hash": action_hash(action)[:12]},
                            "ok" if res.granted else "denied")
        return res.granted

    def apply(self, ep: dict, cap: dict, coerced: Any, by: str, echo_value: Any = None) -> dict:
        """Send the desired-state cmd, wait for the ack, read back. The action has already been validated (and
        confirmed if risky)."""
        out_value = coerced if echo_value is None else echo_value
        cmd_id = self.bridge.new_cmd_id()
        cmd = {"cmd_id": cmd_id, "endpoint": ep["local_id"], "set": {cap["id"]: coerced}, "by": by}
        try:
            pending = self.bridge.send_cmd(ep["node_id"], cmd)
        except TransportDown:
            return self._fail(ep["endpoint_id"], cap["id"], out_value, "offline", "broker_unreachable")
        ack = self.bridge.wait_ack(pending, self.cfg.ack_timeout_s)   # type: ignore[arg-type]
        if ack is None:
            return self._fail(ep["endpoint_id"], cap["id"], out_value, "ack_timeout")
        if not ack["ok"]:
            code = ack["error"]
            return self._fail(ep["endpoint_id"], cap["id"], out_value,
                              code if code in DEVICE_PASSTHROUGH else "device_error", code)
        # read back: the ack carries the applied values; fall back to the last reported state
        node, local = ep["node_id"], ep["local_id"]
        applied = ack["values"]
        verified = False
        if cap["id"] in applied:
            if not values_equal(cap, applied[cap["id"]], coerced):
                return self._fail(ep["endpoint_id"], cap["id"], out_value, "device_error", "readback_mismatch")
            verified = True
            self.registry.live.merge_values(node, local, applied, int(self.clock.now()))
        else:
            got = self.registry.live.get_values(node, local)
            if got and cap["id"] in got[0]:
                if not values_equal(cap, got[0][cap["id"]], coerced):
                    return self._fail(ep["endpoint_id"], cap["id"], out_value, "device_error", "readback_mismatch")
                verified = True
        return {"ok": True, "endpoint_id": ep["endpoint_id"], "capability": cap["id"], "value": out_value,
                "verified": verified}

    # ------------------------------------------------------------------------------------------ node commands
    def node_command(self, node_id: str, cmd: dict) -> dict:
        """identify / unprovision. Returns {sent, acked, error?}."""
        cmd = {"cmd_id": self.bridge.new_cmd_id(), **cmd}
        try:
            pending = self.bridge.send_cmd(node_id, cmd)
        except TransportDown:
            raise HubError("broker_unreachable", "MQTT broker connection is down", 503) from None
        ack = self.bridge.wait_ack(pending, self.cfg.ack_timeout_s)   # type: ignore[arg-type]
        if ack is None:
            return {"sent": True, "acked": False}
        return {"sent": True, "acked": bool(ack["ok"]), **({"error": ack["error"]} if not ack["ok"] else {})}
