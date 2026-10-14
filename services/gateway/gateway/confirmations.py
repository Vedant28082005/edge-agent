"""Confirmation requests and tokens. The gateway is the ONLY minter; device-hub consumes through the internal listener.

Token: 256-bit random (base64url), stored only as SHA-256, bound to user + action_hash (device-hub's, never the agent's or
the client's) + TTL, single use. The plaintext token appears only in the direct-flow response body and in the loopback
hand-off to the agent (agent-internal.yaml `POST /v1/confirmations/{request_id}`)."""
from __future__ import annotations

import json
import logging
import re

from . import policy
from .auth import AuthService, User
from .db import DB
from .events import Broadcaster, Event
from .security import SECRETS, new_id, new_token, sha256_hex
from .upstream import AgentClient, HubClient, UpstreamError

log = logging.getLogger("gateway.confirm")

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQ_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
EP_RE = re.compile(r"^ep_[0-9a-f]{8}$")
SCALAR = (bool, int, float, str)


class ConfirmError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def canonical_action(a) -> dict | None:
    """Validate an action object exactly as agent-internal.yaml ConfirmAction (no extra keys). None = malformed."""
    if not isinstance(a, dict):
        return None
    if a.get("type") == "set_state" and set(a) == {"type", "endpoint_id", "capability", "value"}:
        if isinstance(a["endpoint_id"], str) and EP_RE.match(a["endpoint_id"]) and isinstance(a["capability"], str) \
                and len(a["capability"]) <= 64 and isinstance(a["value"], SCALAR):
            return dict(a)
    if a.get("type") == "run_scene" and set(a) == {"type", "scene_id", "actions"}:
        acts = a["actions"]
        if isinstance(a["scene_id"], str) and 0 < len(a["scene_id"]) <= 64 and isinstance(acts, list) and len(acts) <= 50 \
                and all(isinstance(x, dict) and set(x) == {"endpoint_id", "capability", "value"} and isinstance(x["endpoint_id"], str)
                        and EP_RE.match(x["endpoint_id"]) and isinstance(x["capability"], str) and len(x["capability"]) <= 64
                        and isinstance(x["value"], SCALAR) for x in acts):
            return {"type": "run_scene", "scene_id": a["scene_id"], "actions": [dict(x) for x in acts]}
    return None


class Confirmations:
    def __init__(self, db: DB, hub: HubClient, agent: AgentClient, bc: Broadcaster, users: AuthService, ttl_s: int = 60) -> None:
        self.db, self.hub, self.agent, self.bc, self.users, self.ttl = db, hub, agent, bc, users, ttl_s

    # ------------------------------------------------------------------ requests (chat stream or GET /v1/events)
    def register_request(self, user_id: str, session_id: str | None, ev: dict) -> bool:
        """Record a `confirmation_request` the agent emitted for this user. Untrusted input: validated and bounded; the
        hash is re-derived from the hub at approval time. True only for a request that was not known before."""
        rid, action = ev.get("request_id"), canonical_action(ev.get("action"))
        ah = ev.get("action_hash")
        if not (isinstance(rid, str) and REQ_RE.match(rid)) or action is None or not (isinstance(ah, str) and HASH_RE.match(ah)):
            return False
        exp = ev.get("expires_in_s")
        exp = max(1, min(int(exp), 300)) if isinstance(exp, int) and not isinstance(exp, bool) else self.ttl
        now = self.db.now()
        try:
            self.db.run("INSERT INTO confirm_requests(request_id,user_id,kind,action_json,action_hash,summary,session_id,status,"
                        "created,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (rid, user_id, action["type"], json.dumps(action, sort_keys=True), ah, str(ev.get("summary") or "")[:200],
                         session_id, "pending", now, now + exp))
        except Exception:          # duplicate request_id: the first registration wins
            return False
        self.db.audit(user_id, "confirm.requested", rid, {"kind": action["type"], "hash": ah[:12]})
        return True

    def assigned(self, user: User) -> set[str]:
        return {r["scene_id"] for r in self.db.all("SELECT scene_id FROM scene_assignments WHERE user_id=?", (user.user_id,))}

    # ------------------------------------------------------------------ expiry
    async def sweep(self) -> None:
        """Pending requests past their TTL, and approved ones whose token expired unconsumed, become `expired`; the agent and
        the user are told (reason `no_response` / `token_expired`)."""
        now = self.db.now()
        todo = [(r, "no_response") for r in self.db.all(
            "SELECT * FROM confirm_requests WHERE status='pending' AND expires_at<=?", (now,))]
        todo += [(r, "token_expired") for r in self.db.all(
            "SELECT r.* FROM confirm_requests r JOIN confirm_tokens t ON t.request_id=r.request_id "
            "WHERE r.status='approved' AND t.used=0 AND t.expires_at<=?", (now,))]
        for row, reason in todo:
            if not self.db.run("UPDATE confirm_requests SET status='expired' WHERE request_id=? AND status IN ('pending','approved')",
                               (row["request_id"],)).rowcount:
                continue
            self.db.audit(row["user_id"], "confirm.expired", row["request_id"], {"reason": reason})
            self.bc.publish(Event("confirmation_expired", {"request_id": row["request_id"], "summary": row["summary"],
                                                           "reason": reason}, user_id=row["user_id"]))
            await self._handoff(row, "expired")

    async def _handoff(self, row, decision: str, token: str | None = None) -> bool:
        user = self.users.get(row["user_id"])
        if user is None:
            return False
        body = {"decision": decision, "user_id": user.user_id, "role": user.role, "action": json.loads(row["action_json"]),
                "action_hash": row["action_hash"], **({"confirmation_token": token} if token else {})}
        try:
            return await self.agent.deliver_confirmation(row["request_id"], body)
        except UpstreamError:
            return False

    def _owned(self, user: User, request_id: str):
        row = self.db.one("SELECT * FROM confirm_requests WHERE request_id=?", (request_id,))
        if row is None:
            raise ConfirmError(404, "not_found")
        if row["user_id"] != user.user_id:
            self.db.audit(user.user_id, "confirm.denied_other_user", request_id, result="denied")
            raise ConfirmError(403, "forbidden", "not your request")       # admins included: no override
        return row

    @staticmethod
    def _conflict(status: str) -> ConfirmError:
        if status == "expired":
            return ConfirmError(409, "request_expired", "the request expired; use retry")
        return ConfirmError(409, "request_already_handled", "the request was already handled")

    # ------------------------------------------------------------------ live view from the hub
    async def _hub_json(self, method: str, path: str, **kw):
        try:
            status, body = await self.hub.hub(method, path, **kw)
        except UpstreamError as e:
            raise ConfirmError(e.status, e.code) from None
        if status == 404:
            raise ConfirmError(404, "not_found")
        if status >= 500 or status in (401, 403):
            raise ConfirmError(504 if status == 504 else 502, "upstream_timeout" if status == 504 else "upstream_error")
        if status != 200 or not isinstance(body, dict):
            raise ConfirmError(502, "upstream_error")
        return body

    async def _resolve(self, user: User, action: dict) -> tuple[dict, str]:
        """(canonical action, hash) from the hub's own view, enforcing the user's room access. Accepts the direct-flow
        shape {type: run_scene, scene} and the stored {type: run_scene, scene_id, actions}."""
        if action.get("type") == "set_state":
            ep = await self._hub_json("GET", f"/devices/{action['endpoint_id']}")
            if not user.is_admin and not policy.endpoint_visible(user, ep):
                raise ConfirmError(403, "forbidden")
            h = await self._hub_json("POST", f"/devices/{action['endpoint_id']}/action",
                                     json={"capability": action["capability"], "value": action["value"]})
            canon = canonical_action(h.get("action"))
            if canon is None or not HASH_RE.match(str(h.get("action_hash"))):
                raise ConfirmError(502, "upstream_error")
            return canon, h["action_hash"]
        sid = action.get("scene_id") or action["scene"]
        scene = await self._hub_json("GET", f"/scenes/{sid}")
        if user.role == "guest" or not policy.can_run_scene(user, scene, self.assigned(user)):
            raise ConfirmError(403, "forbidden")
        c = await self._hub_json("GET", f"/scenes/{scene['scene_id']}/confirmation")
        canon = canonical_action(c.get("action"))
        if canon is None:
            raise ConfirmError(502, "upstream_error")
        return canon, c["action_hash"]

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
            await self.sweep()
            row = self._owned(user, request_id)
            if row["status"] != "pending":
                raise self._conflict(row["status"])
            disp, live_hash = await self._resolve(user, json.loads(row["action_json"]))
            if live_hash != row["action_hash"]:
                self.db.run("UPDATE confirm_requests SET status='superseded' WHERE request_id=?", (request_id,))
                self.db.audit(user.user_id, "confirm.stale", request_id, result="denied")
                raise ConfirmError(409, "action_changed", "the action no longer matches; start a new request")
            claimed = self.db.run("UPDATE confirm_requests SET status='approved' WHERE request_id=? AND status='pending'", (request_id,))
            if not claimed.rowcount:
                raise self._conflict("approved")
            tok, exp = self._mint(user.user_id, live_hash, request_id)
            row = self.db.one("SELECT * FROM confirm_requests WHERE request_id=?", (request_id,))
            ok = await self._handoff({**dict(row), "action_json": json.dumps(disp, sort_keys=True), "action_hash": live_hash},
                                     "approved", tok)
            if not ok:
                # the token must never stay usable when the agent did not take it (agent-events.md step 3)
                self.db.run("UPDATE confirm_tokens SET used=1 WHERE token_hash=?", (sha256_hex(tok),))
                self.db.run("UPDATE confirm_requests SET status='pending' WHERE request_id=?", (request_id,))
                self.db.audit(user.user_id, "confirm.delivery_failed", request_id, result="error")
                raise ConfirmError(502, "upstream_error", "the agent did not accept the approval")
            self.db.audit(user.user_id, "confirm.approved", request_id, {"flow": "agent", "hash": live_hash[:12]})
            return {"status": "delivered_to_agent", "action_hash": live_hash, "expires_at": exp}
        disp, live_hash = await self._resolve(user, action)         # direct flow
        tok, exp = self._mint(user.user_id, live_hash, None)
        self.db.audit(user.user_id, "confirm.approved", disp.get("endpoint_id") or disp.get("scene_id", ""),
                      {"flow": "direct", "hash": live_hash[:12]})
        return {"status": "minted", "confirmation_token": tok, "action_hash": live_hash, "expires_at": exp}

    async def decline(self, user: User, request_id: str) -> None:
        await self.sweep()
        row = self._owned(user, request_id)
        if row["status"] != "pending":
            raise self._conflict(row["status"])
        if not self.db.run("UPDATE confirm_requests SET status='declined' WHERE request_id=? AND status='pending'",
                           (request_id,)).rowcount:
            raise self._conflict("declined")
        self.db.audit(user.user_id, "confirm.declined", request_id)
        self.bc.publish(Event("confirmation_declined", {"request_id": request_id, "summary": row["summary"]}, user_id=user.user_id))
        await self._handoff(row, "declined")                        # best effort; the decline is recorded here either way

    async def retry(self, user: User, request_id: str) -> str:
        await self.sweep()
        row = self._owned(user, request_id)
        if user.role == "guest":
            raise ConfirmError(403, "forbidden")
        if row["status"] == "retried":
            raise ConfirmError(409, "request_already_handled", "this request was already retried")
        if row["status"] not in ("declined", "expired"):
            raise ConfirmError(409, "request_not_retryable", "retry is only for declined or expired requests")
        disp, live_hash = await self._resolve(user, json.loads(row["action_json"]))
        new_id_ = new_id("cr_")
        now = self.db.now()
        with self.db.tx():
            if not self.db.run("UPDATE confirm_requests SET status='retried' WHERE request_id=? AND status IN ('declined','expired')",
                               (request_id,)).rowcount:
                raise ConfirmError(409, "request_already_handled")
            self.db.run("INSERT INTO confirm_requests(request_id,user_id,kind,action_json,action_hash,summary,session_id,status,"
                        "created,expires_at,retry_of) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id_, user.user_id, row["kind"], json.dumps(disp, sort_keys=True), live_hash, row["summary"],
                         row["session_id"], "pending", now, now + self.ttl, request_id))
        self.db.audit(user.user_id, "confirm.retried", new_id_, {"of": request_id})
        self.bc.publish(Event("confirmation_request", {"request_id": new_id_, "summary": row["summary"], "action": disp,
                                                       "action_hash": live_hash, "expires_in_s": self.ttl}, user_id=user.user_id))
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
