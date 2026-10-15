"""X-Service-Secret check against a SHA-256 verifier file (hex), constant time. The node never holds the secret itself.
Secrets and verifiers are read from files, never from env/argv, and never logged."""
from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path

_HEX = re.compile(r"[0-9a-f]{64}")
MAX_SECRET_LEN = 256


class CallerAuth:
    def __init__(self, verifier_file: str):
        try:
            text = Path(verifier_file).read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeDecodeError):
            raise SystemExit("vision-node: verifier file missing or unreadable; refusing to start without caller authentication")
        if not _HEX.fullmatch(text):
            raise SystemExit("vision-node: verifier file is not a SHA-256 hex digest; refusing to start")
        self._verifier = text.encode("ascii")

    def check(self, presented: str | None) -> bool:
        if not presented or len(presented) > MAX_SECRET_LEN:
            return False
        try:
            digest = hashlib.sha256(presented.encode("utf-8")).hexdigest().encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(digest, self._verifier)
