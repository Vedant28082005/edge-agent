"""Hashing, JWT, random tokens, constant-time compare, and log redaction of secrets."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(stored: str, pw: str) -> bool:
    try:
        return _ph.verify(stored, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_DUMMY = _ph.hash("dummy-password-for-timing")


def verify_dummy(pw: str) -> None:
    """Burn the same time as a real verification (unknown user): no username enumeration by timing."""
    verify_password(_DUMMY, pw)


def new_token() -> str:
    """256-bit random, base64url without padding (43 chars)."""
    return secrets.token_urlsafe(32)


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(8)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def ct_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def load_or_create_key(path: Path, fernet: bool = False) -> bytes:
    """Key material lives in a file outside the DB (mode 600 where the OS supports it)."""
    path = Path(path)
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    if fernet:
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
    else:
        key = secrets.token_urlsafe(48).encode()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


class Jwt:
    def __init__(self, key: bytes, access_ttl_s: int, clock=time.time) -> None:
        self._key, self._ttl, self._clock = key, access_ttl_s, clock

    def issue(self, user_id: str, role: str) -> str:
        now = int(self._clock())
        return jwt.encode({"sub": user_id, "role": role, "typ": "access", "iat": now, "exp": now + self._ttl,
                           "jti": secrets.token_hex(8)}, self._key, algorithm="HS256")

    def decode(self, token: str) -> dict | None:
        try:
            c = jwt.decode(token, self._key, algorithms=["HS256"],
                           options={"require": ["exp", "sub"], "verify_exp": False, "verify_iat": False})
        except jwt.PyJWTError:
            return None
        if c.get("typ") != "access" or c["exp"] <= self._clock():
            return None
        return c


# ------------------------------------------------------------------ redaction
class _Redactor(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._vals: set[str] = set()
        self._lock = threading.Lock()

    def add(self, v: str | None) -> None:
        if v and len(v) >= 6:
            with self._lock:
                self._vals.add(v)

    def redact(self, s: str) -> str:
        with self._lock:
            vals = list(self._vals)
        for v in vals:
            s = s.replace(v, "[redacted]")
        return s

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = self.redact(msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


SECRETS = _Redactor()


def install_log_redaction() -> None:
    """Redact registered secrets on the root logger's handlers (call after logging is configured)."""
    for h in logging.getLogger().handlers:
        if SECRETS not in h.filters:
            h.addFilter(SECRETS)
