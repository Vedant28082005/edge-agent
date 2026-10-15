"""Offline scheduler benchmark: per-route latency under each ablation, with a mock LLM and SIMULATED latencies.

Everything except the model and the vision services is real: the Agent, the executor, the fast-path resolver, the router,
the fake hub (in-process, loopback). The slow parts are simulated on a virtual clock (nothing sleeps):

    LLM turn   = (prompt tokens - prefix-cached tokens) / 9 tok/s  +  generated tokens / 2 tok/s      [est]
    YOLO       = 94 ms, VLM yes/no = 2.4 s, VLM bounded JSON = 13.5 s                                  [est]

The 9 tok/s prefill and 2 tok/s generation are the Pi 5 figures quoted in docs/ARCHITECTURE.md section 5; the vision
latencies are the router's seed table. So every latency printed here is `real PC wall time + simulated time` and is a
MODEL OF THE SCHEDULER, not a measurement of the Pi: it shows what routing and caching change, not absolute speed.
`ms real` is the part that was actually measured on this PC (resolver, router, executor, fake hub, SQLite).

    python tools/bench_sched.py [--json out.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig  # noqa: E402
from agent.mock import FakeHub, LocalServer, ScriptedLLM  # noqa: E402
from agent.sched.router import BackendAnswer  # noqa: E402
from agent.service import Agent  # noqa: E402

SECRET = "bench-agent-secret-0123456789"
PREFILL_TOK_S, GEN_TOK_S = 9.0, 2.0                           # [est] ARCHITECTURE section 5
VISION_MS = {"yolo_jetson": 94.0, "yolo_pi": 94.0, "vlm_yesno": 2400.0, "vlm_short": 5500.0, "vlm_json": 13500.0}   # [est]

# (utterance, kind, images)
CORPUS: list[tuple[str, str, int]] = (
    [(t, "plain", 0) for t in (
        "turn on the bedroom fan", "turn off the lamp", "switch off the ceiling light", "set the ceiling light to 40%",
        "set fan speed to 3", "open the blinds", "close the blinds", "set the blinds to 50 percent", "turn the fan off",
        "please turn on the lamp", "turn on the ceiling light", "turn on the bedroom fan", "switch on the lamp",
        "turn off the fan")]
    + [(t, "group", 0) for t in ("turn off all the lights", "turn on all lights in the bedroom", "turn off the lights")]
    + [(t, "scene", 0) for t in ("good night", "run good night scene")]
    + [(t, "ambiguous", 0) for t in ("turn on the light", "turn on the fan and the lamp", "make it brighter",
                                      "dim everything a bit", "hello", "what can you do")]
    + [(t, "invalid", 0) for t in ("turn on the garage light", "turn on the kitchen fan", "turn on the [hallucinate] fan",
                                   "turn on the [hallucinate] lamp")]
    + [(t, "sensor", 0) for t in ("what is the temperature in the bedroom?", "how warm is it in the bedroom",
                                  "what's the temperature in the bedroom", "is it hot in the bedroom?")]
    + [(t, "vision", 1) for t in ("is there a person at the door?", "is anyone in the driveway?", "is there a dog in the garden?",
                                  "is the porch light on in the picture?", "describe everything the camera sees",
                                  "is there a package on the porch?")]
)

CONFIGS: list[tuple[str, dict]] = [
    ("all on", {}),
    ("no fast_path", {"fast_path": False}),
    ("no router", {"router": False}),
    ("no constrained", {"constrained_decoding": False}),
    ("no prefix_cache", {"prefix_cache": False}),
    ("all off (always LLM)", {"fast_path": False, "router": False, "constrained_decoding": False, "prefix_cache": False}),
]


class Clock:
    def __init__(self) -> None:
        self.sim_ms = 0.0


class SimLLM(ScriptedLLM):
    """ScriptedLLM's replies, plus a virtual latency from its own token counts. With `prefix_cache` off nothing is reused."""

    def __init__(self, clock: Clock, constrained: bool = True) -> None:
        super().__init__(constrained)
        self.clock, self.prefix_cache = clock, True

    async def complete(self, messages, schema):
        res = await super().complete(messages, schema)
        pn, cn = res.timings["prompt_n"], res.timings["cache_n"] if self.prefix_cache else 0
        res.timings["cache_n"] = cn
        self.clock.sim_ms += (pn - cn) / PREFILL_TOK_S * 1000 + res.timings["predicted_n"] / GEN_TOK_S * 1000
        return res


class SimBackend:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def supports(self, source: str) -> bool:
        return source in VISION_MS

    async def answer(self, source: str, question: str, images: list) -> BackendAnswer:
        ms = VISION_MS[source]
        self.clock.sim_ms += ms
        return BackendAnswer(True, "Yes.", ms)


async def run_config(name: str, flags: dict, tmp: Path) -> dict:
    sf = tmp / f"{abs(hash(name))}.secret"
    sf.write_text(SECRET)
    fake = FakeHub(SECRET, settle_delay=0.01)
    server = LocalServer(fake.app())
    url = await server.start()
    cfg = AgentConfig(hub_url=url, hub_secret_file=str(sf), db_path=":memory:", settle_wait_s=2.0, confirmation_ttl_s=5.0,
                      sched=dict(flags))
    clock = Clock()
    llm = SimLLM(clock)
    agent = Agent.from_config(cfg, llm)
    agent.sched.router.avail.register(SimBackend(clock))
    await agent.start()

    async def decline_confirmations():                         # a gateway that always says no: keeps scene turns short
        while True:
            for rid in agent.confirmations.pending_ids():
                agent.confirmations.resolve(rid, "alice", False)
                agent.confirmations.resolve(rid, "bob", False)
            await asyncio.sleep(0.01)

    gw = asyncio.create_task(decline_confirmations())
    rows = []
    try:
        for i, (text, kind, images) in enumerate(CORPUS * 2):          # two passes: the second shows steady-state caching
            user = "alice" if i % 2 == 0 else "bob"
            sim0, t0 = clock.sim_ms, time.perf_counter()
            ctx = agent.new_request(user, f"s-{user}", text, "member", images)
            real_ms = None
            while True:                                            # latency is taken at `final`; drain to the close for the trace
                item = await asyncio.wait_for(ctx.out.get(), 30)
                if item is not None and item[0] == "final":
                    real_ms = (time.perf_counter() - t0) * 1000
                if item is None:
                    break
            tr = next(r["detail"] for r in agent.store.traces(50) if r["req_id"] == ctx.req_id)
            rows.append({"text": text, "kind": kind, "route": tr["route"], "outcome": tr["outcome"],
                         "real_ms": real_ms, "sim_ms": clock.sim_ms - sim0, "total_ms": real_ms + clock.sim_ms - sim0,
                         "llm_calls": tr["llm_calls"], "cache_hit": tr["cache_hit"]})
        stats = agent.metrics.as_dict()
    finally:
        gw.cancel()
        await agent.stop()
        fake.drop_streams()
        await server.stop()
    return {"name": name, "rows": rows, "metrics": stats}


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def fmt_ms(ms: float) -> str:
    return f"{ms:,.0f}" if ms >= 100 else f"{ms:.1f}"


def report(results: list[dict]) -> str:
    out = []
    routes = ["fast", "sensor", "yolo", "vlm", "llm", "denied"]
    out.append("Per-route latency (ms, total = real PC time + simulated) [sim; seeds are est]")
    out.append(f"{'config':<22}{'route':<8}{'n':>4}{'mean':>11}{'p50':>11}{'p95':>11}{'ms real':>9}")
    for r in results:
        for route in routes:
            xs = [x for x in r["rows"] if x["route"] == route]
            if not xs:
                continue
            tot = [x["total_ms"] for x in xs]
            out.append(f"{r['name']:<22}{route:<8}{len(xs):>4}{fmt_ms(statistics.mean(tot)):>11}{fmt_ms(pct(tot, .5)):>11}"
                       f"{fmt_ms(pct(tot, .95)):>11}{fmt_ms(statistics.mean(x['real_ms'] for x in xs)):>9}")
        out.append("")
    out.append("Per-config summary [sim]")
    out.append(f"{'config':<22}{'turns':>6}{'llm turns':>10}{'mean ms':>11}{'p95 ms':>11}{'sum s':>9}{'cache hit%':>11}{'rejected':>9}")
    base = None
    for r in results:
        rows = r["rows"]
        tot = [x["total_ms"] for x in rows]
        llm = [x for x in rows if x["route"] == "llm"]
        hits = [x["cache_hit"] for x in llm if x["cache_hit"] is not None]
        rej = r["metrics"]["calls_rejected_total"]
        out.append(f"{r['name']:<22}{len(rows):>6}{len(llm):>10}{fmt_ms(statistics.mean(tot)):>11}{fmt_ms(pct(tot, .95)):>11}"
                   f"{sum(tot) / 1000:>9.0f}{(100 * sum(hits) / len(hits) if hits else 0):>10.0f}%{rej:>9}")
        if base is None:
            base = statistics.mean(tot)
    out.append("")
    out.append("Per-kind route under 'all on' (n = turns; same turns appear twice, steady state)")
    allon = next(r for r in results if r["name"] == "all on")
    kinds = sorted({x["kind"] for x in allon["rows"]})
    for k in kinds:
        xs = [x for x in allon["rows"] if x["kind"] == k]
        rc = {}
        for x in xs:
            rc[x["route"]] = rc.get(x["route"], 0) + 1
        out.append(f"  {k:<10} " + ", ".join(f"{a}={b}" for a, b in sorted(rc.items())) +
                   f"   mean {fmt_ms(statistics.mean(x['total_ms'] for x in xs))} ms")
    out.append("")
    out.append("'rejected' = calls the LLM proposed that the executor refused (invalid endpoint etc.); none reached hardware.")
    return "\n".join(out)


async def main_async(json_path: str | None) -> None:
    logging.disable(logging.CRITICAL)
    results = []
    with tempfile.TemporaryDirectory(prefix="bench-sched-") as d:
        for name, flags in CONFIGS:
            results.append(await run_config(name, flags, Path(d)))
    print(report(results))
    if json_path:
        Path(json_path).write_text(json.dumps(results, indent=1), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="also write the raw per-turn rows here")
    args = ap.parse_args()
    asyncio.run(main_async(args.json))


if __name__ == "__main__":
    main()
