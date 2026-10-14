"""Users, login, rotating refresh tokens with reuse detection, login rate limiting."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .db import DB
from .security import Jwt, hash_password, new_id, new_token, sha256_hex, verify_dummy, verify_password

ROLES = ("admin", "member", "guest")


@dataclass
class User:
    user_id: str
    username: str
    role: str
    rooms: frozenset = field(default_factory=frozenset)
    disabled: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_room(self, room: str | None) -> bool:
        return self.is_admin or (room is not None and room in self.rooms)

    def public(self) -> dict:
        return {"user_id": self.user_id, "username": self.username, "role": self.role,
                "rooms": sorted(self.rooms), "disabled": self.disabled}


class LoginLimiter:
    """Sliding window of failures per (username, ip) and per ip. Successful login clears the user+ip counter."""

    def __init__(self, max_user: int, max_ip: int, window_s: int, clock=time.time) -> None:
        self.max_user, self.max_ip, self.window, self.clock = max_user, max_ip, window_s, clock
        self._u: dict[tuple, deque] = defaultdict(deque)
        self._i: dict[str, deque] = defaultdict(deque)

    def _prune(self, q: deque) -> None:
        now = self.clock()
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, username: str, ip: str) -> int:
        """0 if allowed, else seconds until the oldest counted failure leaves the window."""
        wait = 0
        for q, mx in ((self._u[(username.lower(), ip)], self.max_user), (self._i[ip], self.max_ip)):
            self._prune(q)
            if len(q) >= mx:
                wait = max(wait, int(q[0] + self.window - self.clock()) + 1)
        return wait

    def failure(self, username: str, ip: str) -> None:
        now = self.clock()
        self._u[(username.lower(), ip)].append(now)
        self._i[ip].append(now)

    def success(self, username: str, ip: str) -> None:
        self._u.pop((username.lower(), ip), None)


class AuthError(Exception):
    def __init__(self, code: str = "unauthorized", status: int = 401, retry_after: int | None = None) -> None:
        self.code, self.status, self.retry_after = code, status, retry_after


class AuthService:
    def __init__(self, db: DB, jwt_: Jwt, refresh_ttl_s: int, limiter: LoginLimiter) -> None:
        self.db, self.jwt, self.refresh_ttl, self.limiter = db, jwt_, refresh_ttl_s, limiter

    # ---- users
    def _rooms(self, uid: str) -> frozenset:
        return frozenset(r["room"] for r in self.db.all("SELECT room FROM user_rooms WHERE user_id=?", (uid,)))

    def _user(self, row) -> User:
        return User(row["user_id"], row["username"], row["role"], self._rooms(row["user_id"]), bool(row["disabled"]))

    def get(self, uid: str) -> User | None:
        row = self.db.one("SELECT * FROM users WHERE user_id=?", (uid,))
        return self._user(row) if row else None

    def list(self) -> list[User]:
        return [self._user(r) for r in self.db.all("SELECT * FROM users ORDER BY created, username")]

    def admin_count(self) -> int:
        return self.db.one("SELECT COUNT(*) c FROM users WHERE role='admin' AND disabled=0")["c"]

    def create(self, username: str, password_hash: str, role: str, rooms: list[str] | None = None) -> User:
        uid = new_id("u_")
        with self.db.tx():
            self.db.run("INSERT INTO users(user_id,username,pw_hash,role,disabled,created) VALUES(?,?,?,?,0,?)",
                        (uid, username, password_hash, role, self.db.now()))
            for r in set(rooms or []):
                self.db.run("INSERT INTO user_rooms(user_id,room) VALUES(?,?)", (uid, r))
        return self.get(uid)

    def patch(self, uid: str, role=None, rooms=None, password_hash=None, disabled=None) -> User | None:
        with self.db.tx():
            if not self.db.one("SELECT 1 FROM users WHERE user_id=?", (uid,)):
                return None
            if role is not None:
                self.db.run("UPDATE users SET role=? WHERE user_id=?", (role, uid))
            if password_hash is not None:
                self.db.run("UPDATE users SET pw_hash=? WHERE user_id=?", (password_hash, uid))
            if disabled is not None:
                self.db.run("UPDATE users SET disabled=? WHERE user_id=?", (1 if disabled else 0, uid))
            if rooms is not None:
                self.db.run("DELETE FROM user_rooms WHERE user_id=?", (uid,))
                for r in set(rooms):
                    self.db.run("INSERT INTO user_rooms(user_id,room) VALUES(?,?)", (uid, r))
            if password_hash is not None or disabled or role is not None:
                self.db.run("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (uid,))
        return self.get(uid)

    def delete(self, uid: str) -> bool:
        with self.db.tx():
            self.db.run("DELETE FROM refresh_tokens WHERE user_id=?", (uid,))
            return self.db.run("DELETE FROM users WHERE user_id=?", (uid,)).rowcount > 0

    # ---- tokens
    def _issue(self, user: User, family: str | None = None) -> dict:
        rt = new_token()
        self.db.run("INSERT INTO refresh_tokens(token_hash,family_id,user_id,expires_at,created) VALUES(?,?,?,?,?)",
                    (sha256_hex(rt), family or new_id("f_"), user.user_id, self.db.now() + self.refresh_ttl, self.db.now()))
        return {"access_token": self.jwt.issue(user.user_id, user.role), "refresh_token": rt, "role": user.role}

    def authenticate_access(self, token: str) -> User | None:
        c = self.jwt.decode(token)
        if not c:
            return None
        u = self.get(c["sub"])
        return u if u and not u.disabled else None       # role and rooms always come from the DB, not the claim

    async def login(self, username: str, password: str, ip: str) -> dict:
        import asyncio
        wait = self.limiter.retry_after(username, ip)
        if wait:
            self.db.audit(username[:64], "auth.login", detail={"ip": ip}, result="rate_limited")
            raise AuthError("rate_limited", 429, wait)
        row = self.db.one("SELECT * FROM users WHERE username=?", (username,))
        if row is None:
            await asyncio.to_thread(verify_dummy, password)
            ok = False
        else:
            ok = await asyncio.to_thread(verify_password, row["pw_hash"], password) and not row["disabled"]
        if not ok:
            self.limiter.failure(username, ip)
            self.db.audit(username[:64], "auth.login", detail={"ip": ip}, result="denied")
            raise AuthError()
        self.limiter.success(username, ip)
        user = self._user(row)
        self.db.audit(user.user_id, "auth.login", detail={"ip": ip})
        return self._issue(user)

    def refresh(self, refresh_token: str) -> dict:
        h = sha256_hex(refresh_token)
        out = None
        with self.db.tx():                       # the error is raised after COMMIT so the family revocation sticks
            row = self.db.one("SELECT * FROM refresh_tokens WHERE token_hash=?", (h,))
            if row is not None:
                if row["used"] or row["revoked"]:
                    # reuse of a rotated token: someone holds a stale copy. Burn the whole family.
                    self.db.run("UPDATE refresh_tokens SET revoked=1 WHERE family_id=?", (row["family_id"],))
                    self.db.audit(row["user_id"], "auth.refresh_reuse", detail={"family": row["family_id"]},
                                  result="family_revoked")
                elif row["expires_at"] > self.db.now():
                    user = self.get(row["user_id"])
                    if user is not None and not user.disabled:
                        self.db.run("UPDATE refresh_tokens SET used=1 WHERE token_hash=?", (h,))
                        out = self._issue(user, row["family_id"])
                        self.db.audit(user.user_id, "auth.refresh")
        if out is None:
            raise AuthError()
        return out
