from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig  # noqa: E402
from agent.mock import FakeHub, LocalServer, ScriptedLLM  # noqa: E402
from agent.scope import Scope  # noqa: E402
from agent.service import Agent  # noqa: E402
from agent.util import _SECRETS  # noqa: E402

SECRET = "test-agent-secret-0123456789"
# A guest assigned every demo room and the demo scene (guests still never set_state / run_scene: S8b)
GUEST_SCOPE = Scope(frozenset({"Bedroom", "Hall", "Living room"}), frozenset({"sc_9a8b7c6d"}))


class Env:
    def __init__(self, agent, fake, llm, server, tmp):
        self.agent, self.fake, self.llm, self.server, self.tmp = agent, fake, llm, server, tmp

    async def close(self):
        await self.agent.stop()
        self.fake.drop_streams()
        await self.server.stop()
        _SECRETS.discard(SECRET)

    async def chat(self, text, user="alice", session="s1", timeout=10.0, wait_close=True, role="member", scope=None):
        ctx = self.agent.new_request(user, session, text, role, scope=scope)
        return ctx, await collect(ctx, timeout, wait_close)


async def collect(ctx, timeout=10.0, wait_close=True):
    out = []

    async def run():
        while True:
            item = await ctx.out.get()
            if item is None:
                return
            out.append(item)
            if not wait_close and item[0] == "final":
                return

    await asyncio.wait_for(run(), timeout)
    return out


def names(events):
    return [n for n, _ in events]


def one(events, name):
    hits = [d for n, d in events if n == name]
    assert hits, f"no {name} event in {names(events)}"
    return hits[0]


async def make_env(tmp_path, **cfg_kw):
    secret_file = tmp_path / "agent.secret"
    secret_file.write_text(SECRET)
    cfg_kw.setdefault("sched", {"fast_path": False, "router": False})   # the v1 tests exercise the LLM path; sched tests opt in
    cfg_kw.setdefault("warm_prefix", False)    # off by default: keeps env.llm.requests counting exactly the turns a test drives
    fake = FakeHub(SECRET, settle_delay=cfg_kw.pop("settle_delay", 0.2))
    server = LocalServer(fake.app())
    url = await server.start()
    cfg = AgentConfig(hub_url=url, hub_secret_file=str(secret_file), db_path=":memory:", confirmation_ttl_s=cfg_kw.pop("ttl", 2.0),
                      settle_wait_s=cfg_kw.pop("settle_wait", 5.0), **cfg_kw)
    llm = ScriptedLLM(cfg.constrained)
    agent = Agent.from_config(cfg, llm)
    await agent.start()
    return Env(agent, fake, llm, server, tmp_path)


@pytest_asyncio.fixture
async def env(tmp_path):
    e = await make_env(tmp_path)
    yield e
    await e.close()
