"""Confirmation requests and tokens. The gateway is the ONLY minter; device-hub consumes through the internal listener.

Token: 256-bit random (base64url), stored only as SHA-256, bound to user + action_hash + TTL, single use.
The plaintext token appears only in the direct-flow response body and in the loopback hand-off to the agent."""
from __future__ import annotations

import json
import logging
import re

from . import policy
from .auth import User
from .db import DB
from .events import Broadcaster, Event
from .security import SECRETS, new_id, new_token, sha256_hex
from .upstream import AgentClient, HubClient, UpstreamError

log = logging.getLogger("gateway.confirm")

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
REQ_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ConfirmError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


class Confirmations:
    def __init__(self, db: DB, hub: HubClient, agent: AgentClient, bc: Broadcaster, ttl_s: int = 60) -> None:
        self.db, self.hub, self.agent, self.bc, self.ttl = db, hub, agent, bc, ttl_s

    # ------------------------------------------------------------------ requests seen on the proxied chat stream
    def register_request(self, user_id: str, session_id: str | None, ev: dict) -> None:
        """Record a `confirmation_request` SSE event the agent emitted for this user. Untrusted input: validated
        and bounded; the hash is re-derived from the hub at approval time, never trusted from here."""
        rid, action = ev.get("request_id"), ev.get("action")
        if not (isinstance(rid, str) and REQ_RE.match(rid)) or not isinstance(action, dict):
            return
        kind = action.get("type")
        if kind not in ("set_state", "run_scene"):
            return
        ah = ev.get("action_hash")
        if not (isinstance(ah, str) and HASH_RE.match(ah)):
            return
        blob = json.dumps(action, sort_keys=True)
        if len(blob) > 8192:
            return
        exp = ev.get("expires_in_s")
        exp = max(1, min(int(exp), 300)) if isinstance(exp, int) and not isinstance(exp, bool) else self.ttl
        now = self.db.now()
        summary = str(ev.get("summary") or "")[:200]
        try:
            self.db.run("INSERT INTO confirm_requests(request_id,user_id,kind,action_json,action_hash,summary,session_id,status,"
                        "created,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (rid, user_id, kind, blob, ah, summary, session_id, "pending", now, now + exp))
        except Exception:          # duplicate request_id: the first registration wins
            return
        self.db.audit(user_id, "confirm.requested", rid, {"kind": kind, "hash": ah[:12]})

    def _row(self, request_id: str):
        row = self.db.one("SELECT * FROM confirm_requests WHERE request_id=?", (request_id,))
        if row and row["status"] == "pending" and row["expires_at"] <= self.db.now():
            self.db.run("UPDATE confirm_requests SET status='expired' WHERE request_id=? AND status='pending'", (request_id,))
            self.db.audit(row["user_id"], "confirm.expired", request_id, {"reason": "no_response"})
            row = self.db.one("SELECT * FROM confirm_requests WHERE request_id=?", (request_id,))
        return row

    def _owned(self, user: User, request_id: str):
        row = self._row(request_id)
        if row is None:
            raise ConfirmError(404, "not_found")
        if row["user_id"] != user.user_id:
            self.db.audit(user.user_id, "confirm.denied_other_user", request_id, result="denied")
            raise ConfirmError(403, "forbidden", "not your request")       # admins included: no override
        return row

    # ------------------------------------------------------------------ live view from the hub
    async def _hub_json(self, method: str, path: str, **kw):
        try:
            status, body = await self.hub.hub(method, path, **kw)
        except UpstreamError as e:
            raise ConfirmError(e.status, e.code) from None
        if status == 404:
            raise ConfirmError(404, "not_found")
        if status != 200 or not isinstance(body, dict):
            raise ConfirmError(502, "hub_error")
        return body

    async def _resolve(self, user: User, action: dict) -> tuple[dict, str]:
        """(display_action, live action_hash) for a ConfirmAction, enforcing the user's room access.
        The hash always comes from the hub's own view (never from a client or the agent)."""
        if action.get("type") == "set_state":
            ep = await self._hub_json("GET", f"/devices/{action['endpoint_id']}")
            if not user.is_admin and not policy.endpoint_visible(user, ep):
                raise ConfirmError(403, "forbidden")
            h = await self._hub_json("POST", f"/devices/{action['endpoint_id']}/action",
                                     json={"capability": action["capability"], "value": action["value"]})
            return ({"type": "set_state", "endpoint_id": action["endpoint_id"], "capability": action["capability"],
                     "value": action["value"]}, h["action_hash"])
        sid = action["scene"]
        scene = await self._hub_json("GET", f"/scenes/{sid}")
        assigned = self.assigned(user)
        if user.role == "guest" or not policy.can_run_scene(user, scene, assigned):
            raise ConfirmError(403, "forbidden")
        c = await self._hub_json("GET", f"/scenes/{scene['scene_id']}/confirmation")
        return ({"type": "run_scene", "scene": scene["scene_id"], "needs_confirmation": c.get("needs_confirmation", [])},
                c["action_hash"])

    def assigned(self, user: User) -> set[str]:
        return {r["scene_id"] for r in self.db.all("SELECT scene_id FROM scene_assignments WHERE user_id=?", (user.user_id,))}

    def _mint(self, user_id: str, action_hash: str, request_id: str | None) -> tuple[str, int]:
        tok = new_token()
        SECRETS.add(tok)
        exp = self.db.now() + self.ttl
        self.db.run("INSERT INTO confirm_tokens(token_hash,user_id,action_hash,expires_at,created,request_id) VALUES(?,?,?,?,?,?)",
                    (sha256_hex(tok), user_id, action_hash, exp, self.db.now(), request_id))
        return tok, exp

    # ------------------------------------------------------------------ public operations
    async def approve(self, user: User, request_id: str | None, action: dict | None) -> dict:
        if user.role == "guest":
            self.db.audit(user.user_id, "confirm.denied_guest", request_id or "", result="denied")
            raise ConfirmError(403, "forbidden", "guests cannot confirm")
        if request_id is not None:                                  # agent flow
            row = self._owned(user, request_id)
            if row["status"] != "pending":
                raise ConfirmError(409, f"request_{row['status']}", "request is not pending; use retry")
            disp, live_hash = await self._resolve(user, json.loads(row["action_json"]))
            if live_hash != row["action_hash"]:
                self.db.run("UPDATE confirm_requests SET status='superseded' WHERE request_id=?", (request_id,))
                self.db.audit(user.user_id, "confirm.stale", request_id, result="denied")
                raise ConfirmError(409, "action_changed", "the action no longer matches; retry for a fresh request")
            tok, exp = self._mint(user.user_id, live_hash, request_id)
            self.db.run("UPDATE confirm_requests SET status='approved' WHERE request_id=?", (request_id,))
            try:
                ok = await self.agent.deliver_confirmation(request_id, {
                    "decision": "approved", "user_id": user.user_id, "confirmation_token": tok,
                    "action": json.loads(row["action_json"]), "action_hash": live_hash})
            except UpstreamError:
                ok = False
            if not ok:
                self.db.run("UPDATE confirm_tokens SET used=1 WHERE token_hash=?", (sha256_hex(tok),))   # burn: never left usable
                self.db.run("UPDATE confirm_requests SET status='pending' WHERE request_id=?", (request_id,))
                self.db.audit(user.user_id, "confirm.delivery_failed", request_id, result="error")
                raise ConfirmError(502, "agent_unavailable")
            self.db.audit(user.user_id, "confirm.approved", request_id, {"flow": "agent", "hash": live_hash[:12]})
            return {"status": "delivered_to_agent", "action_hash": live_hash, "expires_at": exp}
        disp, live_hash = await self._resolve(user, action)         # direct flow
        tok, exp = self._mint(user.user_id, live_hash, None)
        self.db.audit(user.user_id, "confirm.approved", disp.get("endpoint_id") or disp.get("scene", ""),
                      {"flow": "direct", "hash": live_hash[:12]})
        return {"status": "minted", "confirmation_token": tok, "action_hash": live_hash, "expires_at": exp}

    async def decline(self, user: User, request_id: str) -> None:
        row = self._owned(user, request_id)
        if row["status"] != "pending":
            raise ConfirmError(409, f"request_{row['status']}")
        self.db.run("UPDATE confirm_requests SET status='declined' WHERE request_id=?", (request_id,))
        self.db.audit(user.user_id, "confirm.declined", request_id)
        self.bc.publish(Event("confirmation_declined", {"request_id": request_id, "summary": row["summary"]},
                              user_id=user.user_id))
        try:
            await self.agent.deliver_confirmation(request_id, {"decision": "declined", "user_id": user.user_id})
        except UpstreamError:
            pass                                                    # the decline is recorded here either way

    async def retry(self, user: User, request_id: str) -> str:
        row = self._owned(user, request_id)
        if user.role == "guest":
            raise ConfirmError(403, "forbidden")
        if row["status"] not in ("declined", "expired"):
            raise ConfirmError(409, f"request_{row['status']}", "retry is only for declined or expired requests")
        disp, live_hash = await self._resolve(user, json.loads(row["action_json"]))
        new_id_ = new_id("cr_")
        now = self.db.now()
        with self.db.tx():
            if not self.db.run("UPDATE confirm_requests SET status='retried' WHERE request_id=? AND status IN ('declined','expired')",
                               (request_id,)).rowcount:
                raise ConfirmError(409, "request_retried")
            self.db.run("INSERT INTO confirm_requests(request_id,user_id,kind,action_json,action_hash,summary,session_id,status,"
                        "created,expires_at,retry_of) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id_, user.user_id, row["kind"], json.dumps(disp, sort_keys=True), live_hash, row["summary"],
                         row["session_id"], "pending", now, now + self.ttl, request_id))
        self.db.audit(user.user_id, "confirm.retried", new_id_, {"of": request_id})
        self.bc.publish(Event("confirmation_request", {"request_id": new_id_, "summary": row["summary"], "action": disp,
                                                       "action_hash": live_hash, "expires_in_s": self.ttl},
                              user_id=user.user_id))
        return new_id_

    # ------------------------------------------------------------------ internal: consume
    def consume(self, token: str, user_id: str, action_hash: str) -> tuple[bool, dict]:
        """ONE atomic UPDATE ... RETURNING decides. No row -> denied (a read-only lookup only names the reason)."""
        th, now = sha256_hex(token), self.db.now()
        with self.db.tx():
            r = self.db.one("UPDATE confirm_tokens SET used=1 WHERE token_hash=? AND user_id=? AND used=0 AND expires_at>? "
                            "RETURNING action_hash", (th, user_id, now))
            if r is not None:
                if r["action_hash"] == action_hash:
                    self.db.audit("device-hub", "confirm.consume", user_id, {"hash": action_hash[:12]}, "granted")
                    return True, {"consumed": True, "action_hash": action_hash, "user_id": user_id}
                self.db.audit("device-hub", "confirm.consume", user_id, {"hash": action_hash[:12]}, "hash_mismatch")
                return False, {"consumed": False, "reason": "hash_mismatch"}          # token stays burned (fail closed)
            row = self.db.one("SELECT user_id, used, expires_at FROM confirm_tokens WHERE token_hash=?", (th,))
            if row is None or row["user_id"] != user_id:
                reason = "unknown"                                                      # wrong user: not burned
            elif row["used"]:
                reason = "used"
            else:
                reason = "expired"
            self.db.audit("device-hub", "confirm.consume", user_id, {"hash": action_hash[:12]}, reason)
            return False, {"consumed": False, "reason": reason}
