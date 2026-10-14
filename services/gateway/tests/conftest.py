from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.app import create_public_app  # noqa: E402
from gateway.mock import Mock, build_mock  # noqa: E402
from gateway.security import install_log_redaction  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "contracts"


class Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class GW:
    """A mock-backed gateway with sync TestClients for the public and the internal listener."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.m: Mock = build_mock(clock=self.clock)
        self.ctx = self.m.ctx
        self.app = create_public_app(self.ctx)
        self.pub = TestClient(self.app)
        self.int = TestClient(self.m.internal_app)
        self._tok: dict[str, dict] = {}

    def tokens(self, name: str) -> dict:
        if name not in self._tok:
            r = self.pub.post("/auth/login", json={"username": name, "password": self.m.passwords[name]})
            assert r.status_code == 200, r.text
            self._tok[name] = r.json()
        return self._tok[name]

    def h(self, name: str | None) -> dict:
        return {} if name is None else {"Authorization": "Bearer " + self.tokens(name)["access_token"]}

    def uid(self, name: str) -> str:
        return self.ctx.db.one("SELECT user_id FROM users WHERE username=?", (name,))["user_id"]

    def secret_hdr(self) -> dict:
        return {"X-Service-Secret": self.m.device_hub_secret}

    def consume(self, token, user_id, action_hash, secret=True):
        return self.int.post("/internal/confirmations/consume", headers=self.secret_hdr() if secret else {},
                             json={"token": token, "user_id": user_id, "action_hash": action_hash})


@pytest.fixture()
def gw():
    install_log_redaction()
    g = GW()
    yield g
    g.pub.close()
    g.int.close()
