from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig  # noqa: E402
from agent.mock import FakeHub, LocalServer, ScriptedLLM  # noqa: E402
from agent.service import Agent  # noqa: E402
from agent.util import _SECRETS  # noqa: E402

SECRET = "test-agent-secret-0123456789"


class Env:
    def __init__(self, agent, fake, llm, server, tmp):
        self.agent, self.fake, self.llm, self.server, self.tmp = agent, fake, llm, server, tmp

    async def chat(self, text, user="alice", session="s1", timeout=10.0, wait_close=True):
        ctx = self.agent.new_request(user, session, text)
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
    await e.agent.stop()
    await e.server.stop()
    _SECRETS.discard(SECRET)
