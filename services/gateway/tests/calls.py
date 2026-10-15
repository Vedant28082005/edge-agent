"""One representative, valid request per operationId (shared by the contract test and the role matrix)."""
from __future__ import annotations

import itertools

from gateway.mock import action_hash

_n = itertools.count(1)
NODE = "esp32-aabbccddeeff"
SET_ACTION = {"type": "set_state", "endpoint_id": "ep_00000001", "capability": "power", "value": True}
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SCENE_BODY = {"name": "Test scene", "actions": [{"target": {"endpoint_id": "ep_00000001"}, "capability": "power", "value": True}]}


def pending_request(gw, uid: str, status: str = "pending") -> str:
    rid = f"rq-{next(_n)}"
    gw.ctx.confirm.register_request(uid, "s1", {"request_id": rid, "summary": "Turn on?", "action": SET_ACTION,
                                                "action_hash": action_hash(SET_ACTION), "expires_in_s": 60})
    if status != "pending":
        gw.ctx.db.run("UPDATE confirm_requests SET status=? WHERE request_id=?", (status, rid))
    return rid


def own_scene(gw, uid: str) -> str:
    sid = f"sc_{0x100 + next(_n):08x}"
    gw.m.hub.add_scene(sid, "Mine", uid, SCENE_BODY["actions"])
    return sid


def new_user(gw) -> str:
    u = gw.ctx.auth.create(f"tmp{next(_n)}", "x", "member", ["Bedroom"])
    return u.user_id


def build(gw, op: str, role: str | None) -> dict:
    """-> {method, url, kw (json/files/params), status}. `role` owns any resource the call needs."""
    uid = gw.uid(role) if role else "none"
    if op in ("updateMyMemory", "deleteMyMemory", "listMyMemories"):
        gw.m.agent.memories.setdefault(uid, {})["m1"] = "likes tea"
    if op == "runScene":
        gw.ctx.db.run("INSERT OR IGNORE INTO scene_assignments(user_id,scene_id) SELECT user_id,'sc_00000001' FROM users WHERE role='guest'")
    t = {
        "login": ("POST", "/auth/login", {"json": {"username": "admin", "password": gw.m.passwords["admin"]}}, 200),
        "refresh": ("POST", "/auth/refresh", {"json": {"refresh_token": gw.pub.post("/auth/login", json={
            "username": "member", "password": gw.m.passwords["member"]}).json()["refresh_token"]}}, 200),
        "listDevices": ("GET", "/devices", {}, 200),
        "setDeviceState": ("POST", "/devices/ep_00000001/set", {"json": {"capability": "power", "value": True}}, 200),
        "listScenes": ("GET", "/scenes", {}, 200),
        "createScene": ("POST", "/scenes", {"json": SCENE_BODY}, 201),
        "updateScene": ("PUT", None, {"json": SCENE_BODY}, 200),
        "deleteScene": ("DELETE", None, {}, 204),
        "runScene": ("POST", "/scenes/sc_00000001/run", {}, 200),
        "mintConfirmation": ("POST", "/confirmations", {"json": {"action": SET_ACTION}}, 200),
        "declineConfirmation": ("POST", None, {}, 204),
        "retryConfirmation": ("POST", None, {}, 202),
        "chat": ("POST", "/chat", {"json": {"text": "hello"}}, 200),
        "uploadVoice": ("POST", "/voice", {"files": {"audio": ("a.wav", WAV, "audio/wav")}}, 200),
        "uploadImage": ("POST", "/images", {"files": {"image": ("a.png", PNG, "image/png")}}, 200),
        "listMyMemories": ("GET", "/memory", {}, 200),
        "updateMyMemory": ("PUT", "/memory/m1", {"json": {"text": "likes coffee"}}, 200),
        "deleteMyMemory": ("DELETE", "/memory/m1", {}, 204),
        "systemHealth": ("GET", "/system/health", {}, 200),
        "createWsTicket": ("POST", "/ws/ticket", {}, 200),
        "adminGetSceneAssignments": ("GET", "/admin/scenes/sc_00000001/assignments", {}, 200),
        "adminSetSceneAssignments": ("PUT", "/admin/scenes/sc_00000001/assignments", {"json": {"user_ids": [gw.uid("guest")]}}, 200),
        "adminListWifiProfiles": ("GET", "/admin/wifi-profiles", {}, 200),
        "adminCreateWifiProfile": ("POST", "/admin/wifi-profiles", {"json": {"name": "office", "ssid": "Office", "password": "office-pass-123"}}, 201),
        "adminDeleteWifiProfile": ("DELETE", None, {}, 204),
        "adminCreateProvisionSession": ("POST", "/admin/provision-sessions", {"json": {
            "node_id": NODE, "wifi": {"ssid": "home", "password": "wifi-pass-12345"}}}, 201),
        "adminListNodes": ("GET", "/admin/nodes", {}, 200),
        "adminApproveNode": ("POST", f"/admin/nodes/{NODE}/approve", {"json": {"endpoints": [
            {"local_id": "l1", "name": "Lamp", "room": "Hall", "tags": ["night"], "risk": "normal"}]}}, 200),
        "adminIdentifyNode": ("POST", f"/admin/nodes/{NODE}/identify", {"json": {"duration_s": 5}}, 202),
        "adminUnprovisionNode": ("POST", f"/admin/nodes/{NODE}/unprovision", {}, 202),
        "adminForgetNode": ("DELETE", f"/admin/nodes/{NODE}", {"params": {"confirm": "true"}}, 204),
        "adminPatchEndpoint": ("PATCH", "/admin/endpoints/ep_00000001", {"json": {"name": "Renamed", "tags": ["a"]}}, 200),
        "adminRebindEndpoint": ("POST", "/admin/endpoints/ep_00000001/rebind", {"json": {"node_id": NODE, "local_id": "l9"}}, 200),
        "adminActionLog": ("GET", "/admin/action-log", {"params": {"limit": "5"}}, 200),
        "adminListUsers": ("GET", "/admin/users", {}, 200),
        "adminCreateUser": ("POST", "/admin/users", {"json": {"username": f"new{next(_n)}", "password": "long-enough-pw", "role": "guest",
                                                              "rooms": ["Hall"]}}, 201),
        "adminPatchUser": ("PATCH", None, {"json": {"rooms": ["Kitchen"], "disabled": False}}, 200),
        "adminDeleteUser": ("DELETE", None, {}, 204),
    }[op]
    method, url, kw, status = t
    if op in ("updateScene", "deleteScene"):
        url = f"/scenes/{own_scene(gw, uid)}"
    elif op == "declineConfirmation":
        url = f"/confirmations/{pending_request(gw, uid)}/decline"
    elif op == "retryConfirmation":
        url = f"/confirmations/{pending_request(gw, uid, 'declined')}/retry"
    elif op == "adminDeleteWifiProfile":
        gw.ctx.profiles.put("tmp-profile", "Tmp", "tmp-wifi-pass-1")
        url = "/admin/wifi-profiles/tmp-profile"
    elif op in ("adminPatchUser", "adminDeleteUser"):
        url = f"/admin/users/{new_user(gw)}"
    return {"method": method, "url": url, "kw": kw, "status": status}


def call(gw, op: str, role: str | None):
    spec = build(gw, op, role)
    r = gw.pub.request(spec["method"], spec["url"], headers=gw.h(role) if op not in ("login", "refresh") else {}, **spec["kw"])
    return r, spec["status"]
