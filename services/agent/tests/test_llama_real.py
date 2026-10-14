"""One real llama-server turn. Runs ONLY when EDGE_TEST_GGUF points at a model file you provide (nothing is downloaded);
needs a `llama-server` binary on PATH or in EDGE_LLAMA_SERVER. Measures prompt tokens (real /tokenize vs our estimate),
prefill time and prefix-cache hits for two consecutive turns, and prints the numbers (run with `-s`).

WRITTEN BUT NOT RUN in the S3 session (no model path was given)."""
import asyncio
import os
import shutil
import socket
import subprocess
import time

import httpx
import pytest

from agent.config import AgentConfig
from agent.llm import LlmClient
from agent.mock import FakeHub, LocalServer
from agent.service import Agent
from agent.util import est_tokens
from conftest import SECRET, collect, one

GGUF = os.environ.get("EDGE_TEST_GGUF")
pytestmark = [pytest.mark.llama, pytest.mark.skipif(not GGUF, reason="EDGE_TEST_GGUF not set (no model provided)")]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_one_real_turn(tmp_path):
    exe = os.environ.get("EDGE_LLAMA_SERVER") or shutil.which("llama-server")
    if not exe:
        pytest.skip("llama-server binary not found (set EDGE_LLAMA_SERVER)")
    port = free_port()
    proc = subprocess.Popen([exe, "-m", GGUF, "--host", "127.0.0.1", "--port", str(port), "-c", "4096", "-np", "1",
                             "--cache-reuse", "256"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fake = LocalServer(None)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(600):
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            pytest.fail("llama-server did not become healthy")
        hub = FakeHub(SECRET)
        fake = LocalServer(hub.app())
        url = await fake.start()
        sf = tmp_path / "agent.secret"
        sf.write_text(SECRET)
        cfg = AgentConfig(hub_url=url, hub_secret_file=str(sf), db_path=":memory:", llm_url=base, llm_timeout_s=900)
        agent = Agent.from_config(cfg, LlmClient(base, constrained=True, timeout_s=900))
        await agent.start()
        try:
            exact = httpx.post(base + "/tokenize", json={"content": agent.builder.prefix}, timeout=30).json()["tokens"]
            print(f"\nprefix: estimated {est_tokens(agent.builder.prefix)} tokens, real {len(exact)} tokens")
            timings = []
            for text in ("turn on the bedroom fan", "turn off the bedroom fan"):
                real = []
                orig = agent.planner.llm.complete

                async def spy(m, s, orig=orig):
                    r = await orig(m, s)
                    real.append(r.timings)
                    return r

                agent.planner.llm.complete = spy
                ctx = agent.new_request("alice", "s1", text)
                ev = await collect(ctx, timeout=900)
                agent.planner.llm.complete = orig
                t = real[0]
                timings.append(t)
                print(f"turn {text!r}: prompt_n={t.get('prompt_n')} cache_n={t.get('cache_n')} "
                      f"prompt_ms={t.get('prompt_ms')} predicted_n={t.get('predicted_n')} final={one(ev, 'final')['text']!r}")
            assert [c for c in hub.calls if c["tool"] == "set_state"], "the real model produced no set_state call"
            assert timings[1].get("cache_n", 0) > 0, "second turn did not hit the prefix cache"
        finally:
            await agent.stop()
    finally:
        await fake.stop()
        proc.terminate()                                   # our own child process, by handle
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
