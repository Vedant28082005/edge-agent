"""(S8i) `Agent._warm_prefix`: one throwaway LLM request on startup/registry-change to pre-fill llama-server's
prefix (KV) cache on slot 0, so the first real user turn is not the one paying that cost. Off by default in
`conftest.make_env` (see its comment) so every other test's `env.llm.requests` count stays exactly the turns it
drove; exercised explicitly here."""
from __future__ import annotations

import asyncio

from conftest import make_env


async def _settle(agent) -> None:
    """Let any background task `_spawn` started (the warmup) actually run and finish."""
    for _ in range(50):
        if not agent._bg:
            return
        await asyncio.gather(*list(agent._bg))
    await asyncio.sleep(0)


async def test_warmup_fires_once_at_startup_on_id_slot_0(tmp_path):
    e = await make_env(tmp_path, warm_prefix=True)
    try:
        await _settle(e.agent)
        assert len(e.llm.requests) == 1
        warm = e.llm.requests[0]
        assert warm[0]["role"] == "system" and warm[0]["content"] == e.agent.builder.prefix
        assert warm[-1]["content"] == "User: hello"
        assert e.agent.llm.id_slot == 0          # the only slot the warmup (or any real turn) ever pins
    finally:
        await e.close()


async def test_warmup_is_off_by_default_in_tests(tmp_path):
    e = await make_env(tmp_path)
    try:
        await _settle(e.agent)
        assert e.llm.requests == []              # conftest's default keeps request counts exactly what a test drives
    finally:
        await e.close()


async def test_warmup_fires_again_after_a_registry_change_that_changes_the_prefix(tmp_path):
    e = await make_env(tmp_path, warm_prefix=True)
    try:
        await _settle(e.agent)
        assert len(e.llm.requests) == 1
        before = e.agent.builder.prefix_version
        e.fake.rename("ep_a1000001", "Ventilator")          # changes the prefix bytes (name is part of the device line)
        for _ in range(50):
            if e.agent.builder.prefix_version > before:
                break
            await asyncio.sleep(0.02)
        await _settle(e.agent)
        assert len(e.llm.requests) == 2
    finally:
        await e.close()


async def test_a_broken_llm_during_warmup_never_raises(tmp_path):
    e = await make_env(tmp_path, warm_prefix=True)
    try:
        await _settle(e.agent)

        async def boom(messages, schema):
            raise RuntimeError("llama-server is down")

        e.agent.llm.complete = boom
        await e.agent._warm_prefix()        # must swallow the error, not raise -- a caller awaiting it directly still sees that
    finally:
        await e.close()
