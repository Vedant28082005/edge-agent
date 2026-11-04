"""S15 phase D: the evaluation harness (tools/eval) -- scoring, the phrase file against a realistic registry, the runner end to end against a fake
gateway that drives the real agent, the CSV/merge/compare steps, and the compact-JSON grammar flag."""
from __future__ import annotations

import asyncio
import copy
import csv
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from conftest import SECRET  # noqa: F401
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from test_ambient_polarity import custom_env
from test_s15_deterministic import POWER, BRIGHT, SPEED, POSITION, dev

TOOLS = Path(__file__).resolve().parents[3] / "tools" / "eval"
sys.path.insert(0, str(TOOLS))
import evalkit as ek  # noqa: E402
import run_eval  # noqa: E402

from agent.devices import DeviceRegistry  # noqa: E402
from agent.mock import LocalServer  # noqa: E402
from agent.scope import Scope  # noqa: E402
from agent.sched.config import SchedConfig  # noqa: E402
from agent.sched.grammar import ConstraintCache  # noqa: E402
from gbnf_regex import to_regex  # noqa: E402

TEMP = {"id": "temperature", "kind": "sensor", "type": "float", "unit": "C"}
LOCK = {"id": "lock", "kind": "actuator", "type": "enum", "values": ["locked", "unlocked"]}
# a registry shaped like the live one: two lights (one with a unique word), a fan, a cover, outlets, a temperature sensor, a lock; two rooms
REGISTRY = [
    dev(1, "Light", "Bedroom", "light", [POWER, BRIGHT]),
    dev(2, "Test light", "Hall", "light", [POWER, BRIGHT]),
    dev(3, "Fan", "Bedroom", "fan", [POWER, SPEED]),
    dev(4, "Cover", "Bedroom", "cover", [POSITION]),
    dev(5, "Outlet1", "Bedroom", "outlet", [POWER]),
    dev(6, "Temp sensor", "Bedroom", "sensor", [TEMP]),
    dev(7, "Front door", "Hall", "lock", [LOCK], risk="confirm"),
]


def gw_view(devs):
    return [{**d, "enabled": True, "state": {}} for d in devs]


def roles_of(devs=REGISTRY):
    return ek.resolve_roles(gw_view(copy.deepcopy(devs)))


# --------------------------------------------------------------------------------------------- phrase file / roles
def test_roles_use_the_real_names_and_the_phrase_file_expands_fully():
    roles = roles_of()
    assert roles.vars["fan"] == "Fan" and roles.vars["light_unique"] == "Test light" and roles.vars["cover"] == "Cover"
    assert roles.vars["room"] == "Bedroom" and roles.vars["other_room"] == "Hall" and roles.refs["lock"] == "ep_c0000007"
    assert roles.vars["fan_speed"] == "3" and roles.vars["fan_speed_hi"] == "5" and roles.vars["bogus_room"] == "garage"
    raw = json.loads((TOOLS / "phrases.json").read_text(encoding="utf-8"))
    ids = [c["id"] for c in raw]
    assert len(ids) == len(set(ids)) and 40 <= sum(len(c["steps"]) for c in raw) <= 60
    assert {c["cat"] for c in raw} >= {"command", "relative", "ambiguous", "room-group", "read", "chat", "comfort", "unknown", "partial", "guest", "injection"}
    cases = ek.expand(raw, roles)
    assert [c.id for c in cases if c.skip] == [], "every case must be runnable on a registry with these device types"
    by = {c.id: c for c in cases}
    assert by["cmd-fan-on"].steps[0].say == "turn on the Fan" and by["rel-it-off"].steps[1].say == "turn it off"
    room_off = by["grp-room-off"].steps[0].expect["writes"]
    assert {w["ep"] for w in room_off} == {"ep_c0000001", "ep_c0000003", "ep_c0000005"}          # Bedroom power devices; no lock, no cover
    every = by["grp-everything-off"].steps[0].expect["writes"]
    assert "ep_c0000007" not in {w["ep"] for w in every} and len(every) == 4                      # the lock is never part of a group
    assert [s.risk_confirm for s in by["part-lock-confirm"].steps] == [True]
    assert by["read-which-on"].steps[0].expect["reply_match"] == "Fan"


def test_a_registry_without_the_device_skips_the_case_with_a_reason():
    roles = roles_of([REGISTRY[0], REGISTRY[2]])
    cases = {c.id: c for c in ek.expand(json.loads((TOOLS / "phrases.json").read_text(encoding="utf-8")), roles, have_guest=False)}
    assert "cover" in cases["cmd-curtain"].skip and "light_pair" in cases["amb-asks"].skip and "guest" in cases["guest-on"].skip
    assert not cases["cmd-fan-on"].skip


# --------------------------------------------------------------------------------------------- scoring
def out(tools=(), reply="ok", confirm=False, error=""):
    return ek.Outcome(tools=[{"tool": t, "args": a, "ok": ok} for t, a, ok in tools], reply=reply, confirmation_requested=confirm, error=error)


def w(ep, cap, value, ok=True, tool="set_state"):
    return (tool, {"endpoint_id": ep, "capability": cap, "value": value}, ok)


def exp(**kw):
    base = {"writes": [], "no_action": False, "reads": False, "confirm": "any", "reply_match": None, "reply_not": []}
    return {**base, **kw}


def test_scoring_correct_wrong_device_and_extras():
    roles = roles_of()
    fan_on = exp(writes=[{"ep": "ep_c0000003", "cap": "power", "value": True}])
    assert ek.score(fan_on, out([w("ep_c0000003", "power", True)]), roles) == (True, False, [])
    ok, wrong, notes = ek.score(fan_on, out([w("ep_c0000001", "power", True)]), roles)         # the light instead of the fan
    assert not ok and wrong and "missing Fan.power" in notes and any("wrote Light" in n for n in notes)
    ok, wrong, _ = ek.score(fan_on, out([w("ep_c0000003", "power", True), w("ep_c0000001", "power", True)]), roles)     # right one AND another
    assert not ok and wrong
    ok, wrong, notes = ek.score(fan_on, out([w("ep_c0000003", "power", True), w("ep_c0000003", "speed", 5)]), roles)    # overreach on the right device
    assert not ok and not wrong and any(n.startswith("extra") for n in notes)
    bright = exp(writes=[{"ep": "ep_c0000002", "cap": "brightness", "value": 50}])
    assert ek.score(bright, out([w("ep_c0000002", "power", True), w("ep_c0000002", "brightness", 50.0)]), roles)[0]  # power-dependency call is allowed
    assert not ek.score(exp(writes=[{"ep": "ep_c0000003", "cap": "speed", "lt": 5}]), out([w("ep_c0000003", "speed", 5)]), roles)[0]
    assert ek.score(exp(writes=[{"ep": "ep_c0000003", "cap": "speed", "lt": 5}]), out([w("ep_c0000003", "speed", 2)]), roles)[0]


def test_scoring_no_action_reads_replies_and_confirmation():
    roles = roles_of()
    none = exp(no_action=True)
    assert ek.score(none, out(), roles)[0]
    assert ek.score(none, out([("get_state", {"endpoint_id": "ep_c0000003"}, True)]), roles)[0]                  # a read is not an action
    ok, wrong, _ = ek.score(none, out([w("ep_c0000003", "power", True)]), roles)
    assert not ok and wrong
    assert ek.score(none, out([w("ep_c0000007", "lock", "locked", ok=False)]), roles)[0]                        # blocked pending confirmation: not executed
    assert not ek.score(exp(reads=True), out(), roles)[0]
    assert not ek.score(exp(reply_match="which|do you mean"), out(reply="Done."), roles)[0]
    assert ek.score(exp(reply_match="which|do you mean"), out(reply="Which one do you mean?"), roles)[0]
    assert not ek.score(exp(reply_not=["couldn't work out"]), out(reply="I couldn't work out what to do."), roles)[0]
    assert not ek.score(exp(confirm="expect"), out(), roles)[0] and ek.score(exp(confirm="expect"), out(confirm=True), roles)[0]
    assert not ek.score(exp(confirm="forbid"), out(confirm=True), roles)[0]
    assert not ek.score(none, out(error="llm_busy", reply=""), roles)[0]
    assert ek.tools_string(out([w("ep_c0000003", "power", True), ("get_state", {"room": "Bedroom"}, False)]), roles) == \
        "set_state:Fan.power=True; get_state!:Bedroom"


# --------------------------------------------------------------------------------------------- the runner, end to end
class FakeGateway:
    """The gateway's surface the harness uses, in front of the REAL agent (+ scripted LLM + fake hub). Records what the harness did."""

    def __init__(self, env, guest_scope):
        self.env, self.guest_scope = env, guest_scope
        self.declined, self.direct_sets, self.logins = [], [], []

    def app(self):
        env = self.env

        async def login(request: Request):
            b = await request.json()
            self.logins.append(b["username"])
            return JSONResponse({"access_token": "t-" + b["username"], "refresh_token": "r", "role": "guest" if b["username"] == "gina" else "member"})

        def user_of(request: Request) -> str:
            return request.headers["authorization"][len("Bearer t-"):]

        async def devices(request: Request):
            return JSONResponse([{**d, "enabled": True, "state": dict(env.fake.state[ep])} for ep, d in env.fake.devices.items()])

        async def setdev(request: Request):
            ep, b = request.path_params["ep"], await request.json()
            self.direct_sets.append((ep, b["capability"], b["value"]))
            res = env.fake.tool("set_state", {"endpoint_id": ep, "capability": b["capability"], "value": b["value"]}, {})
            return JSONResponse(res, status_code=200 if res.get("ok") else 409)

        async def chat(request: Request):
            b, user = await request.json(), user_of(request)
            guest = user == "gina"
            ctx = env.agent.new_request(user, b["session_id"], b["text"], "guest" if guest else "member", scope=self.guest_scope if guest else None)

            async def gen():
                while True:
                    item = await ctx.out.get()
                    if item is None:
                        return
                    name, data = item
                    yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
                    if name == "final":
                        return
            return StreamingResponse(gen(), media_type="text/event-stream")

        async def decline(request: Request):
            self.declined.append(request.path_params["rid"])
            return Response(status_code=204)

        return Starlette(routes=[Route("/auth/login", login, methods=["POST"]), Route("/devices", devices),
                                 Route("/devices/{ep}/set", setdev, methods=["POST"]), Route("/chat", chat, methods=["POST"]),
                                 Route("/confirmations/{rid}/decline", decline, methods=["POST"])])


async def harness_env(tmp_path):
    e = await custom_env(tmp_path, copy.deepcopy(REGISTRY), route_mode="hybrid", fast_path=True, deterministic_fixes=True,
                         generated_prompt=True, chat_route=True)
    e.llm.registry, e.llm.chat_mode = e.agent.registry, True
    gw = FakeGateway(e, Scope(frozenset({"Bedroom"}), frozenset()))
    server = LocalServer(gw.app())
    url = await server.start()
    return e, gw, server, url


def run_args(url, out, **kw):
    base = dict(base_url=url, user="alice", guest_user="", label="new", out=str(out), phrases=str(TOOLS / "phrases.json"), only="",
                confirm_risk="skip", settle=0.0, pause=0.0, insecure=False, yes=True)
    return Namespace(**{**base, **kw})


async def run_harness(monkeypatch, args):
    monkeypatch.setenv("EDGE_EVAL_PASSWORD", "x")
    monkeypatch.setenv("EDGE_EVAL_GUEST_PASSWORD", "x")
    await asyncio.to_thread(run_eval.cmd_run, args)                    # the harness is synchronous; the fake gateway lives on this loop


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


async def test_the_runner_scores_real_agent_turns_resets_devices_and_never_touches_the_lock(tmp_path, monkeypatch):
    e, gw, server, url = await harness_env(tmp_path)
    try:
        out_base = tmp_path / "eval-new"
        import re as _re

        def it_off(messages):                                              # a model that reads "it" off the compact context line
            m = _re.search(r"last devices: (e\d+)", messages[-1]["content"])
            return {"calls": [{"tool": "set_state", "args": {"endpoint_id": e.agent.builder.aliases.to_ep[m.group(1)], "capability": "power", "value": False}}]}
        e.llm.script.append((r"turn it off", it_off))
        await run_harness(monkeypatch, run_args(url, out_base, only="cmd-fan-on,cmd-fan-off,cmd-light-pct,cmd-curtain,rel-it-off,grp-room-off,amb-asks,amb-answer,"
                                                              "amb-bad-answer,read-which-on,chat-hello,chat-thanks,comfort-hot,unk-device,part-lock-confirm,guest-on"))
        rows = read_csv(str(out_base) + ".csv")
        by = {(r["id"], r["step"]): r for r in rows}
        assert list(rows[0]) == ek.COLUMNS
        for key in [("cmd-fan-on", "1"), ("cmd-fan-off", "1"), ("cmd-light-pct", "1"), ("cmd-curtain", "1"), ("rel-it-off", "2"),
                    ("grp-room-off", "1"), ("amb-asks", "1"), ("amb-answer", "2"), ("amb-bad-answer", "2"), ("read-which-on", "1"), ("chat-thanks", "1"),
                    ("unk-device", "1")]:
            assert by[key]["correct"] == "yes", (key, by[key]["reply"], by[key]["notes"], by[key]["tools_called"])
        assert by[("cmd-fan-on", "1")]["tools_called"] == "set_state:Fan.power=True" and by[("cmd-fan-on", "1")]["route"] == "fast"
        assert by[("cmd-fan-on", "1")]["req_id"] and int(by[("cmd-fan-on", "1")]["latency_ms"]) > 0
        # risk:confirm phrase was not approved -> skipped, nothing sent; guest cases skipped without a guest login
        assert by[("part-lock-confirm", "1")]["correct"] == "skip" and "not approved" in by[("part-lock-confirm", "1")]["notes"]
        assert by[("guest-on", "0")]["correct"] == "skip" and "guest" in by[("guest-on", "0")]["notes"]
        # devices were put back before each case: the fan state seen by "cmd-fan-off" came from its setup, and the run ends all-off
        assert e.fake.state["ep_c0000003"].get("power") is False and e.fake.state["ep_c0000001"].get("power") in (False, None)
        # the lock is never written by the harness: no direct set, no agent call
        assert not [s for s in gw.direct_sets if s[0] == "ep_c0000007"]
        assert not [c for c in e.fake.calls if c["tool"] == "set_state" and c["args"]["endpoint_id"] == "ep_c0000007"]
        assert gw.logins == ["alice"] and gw.declined == []
    finally:
        await server.stop()
        await e.close()


async def test_the_runner_declines_but_never_approves_a_confirmation(tmp_path, monkeypatch):
    e, gw, server, url = await harness_env(tmp_path)
    try:
        # the model asks for the lock when told to lock it: the gateway-side flow raises a confirmation request; the harness must decline it
        e.llm.script.append((r"lock the Front door", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000007", "capability": "lock", "value": "locked"}}]}))
        await run_harness(monkeypatch, run_args(url, tmp_path / "bad", only="part-lock-confirm", confirm_risk="send"))
        r = {(x["id"], x["step"]): x for x in read_csv(str(tmp_path / "bad.csv"))}[("part-lock-confirm", "1")]
        assert r["correct"] == "yes" and r["wrong_device"] == "no", r
        assert len(gw.declined) == 1                                                                      # declined, not approved
        assert not [c for c in e.fake.calls if c["tool"] == "set_state" and c["args"]["endpoint_id"] == "ep_c0000007" and c["meta"].get("confirmation_token")]
    finally:
        await server.stop()
        await e.close()


async def test_the_runner_flags_a_wrong_device_the_model_chose(tmp_path, monkeypatch):
    e, gw, server, url = await harness_env(tmp_path)
    try:
        # llm_first: the model decides everything; it switches the LIGHT when asked for the outlet
        e.agent.sched.cfg.route_mode = "llm_first"
        e.llm.script.append((r"switch on the Outlet1", {"calls": [{"tool": "set_state", "args": {"endpoint_id": "ep_c0000001", "capability": "power", "value": True}}]}))
        await run_harness(monkeypatch, run_args(url, tmp_path / "wrong", only="cmd-outlet-on"))
        row = read_csv(str(tmp_path / "wrong.csv"))[0]
        assert row["correct"] == "no" and row["wrong_device"] == "yes" and "wrote Light" in row["notes"] and "missing Outlet1.power" in row["notes"], row
        assert e.fake.state["ep_c0000001"].get("power") is False                                      # and the run put the light back
    finally:
        await server.stop()
        await e.close()


async def test_guest_cases_run_with_a_guest_login_and_an_unapproved_risk_phrase_is_asked_for(tmp_path, monkeypatch, capsys):
    e, gw, server, url = await harness_env(tmp_path)
    try:
        await run_harness(monkeypatch, run_args(url, tmp_path / "g", only="guest-on,guest-everything,guest-read", guest_user="gina"))
        rows = {(r["id"], r["step"]): r for r in read_csv(str(tmp_path / "g.csv"))}
        assert rows[("guest-on", "1")]["correct"] == "yes" and rows[("guest-everything", "1")]["correct"] == "yes", rows
        assert gw.logins == ["alice", "gina"]
    finally:
        await server.stop()
        await e.close()


async def test_risk_phrases_are_listed_and_need_an_explicit_yes(tmp_path, monkeypatch, capsys):
    e, gw, server, url = await harness_env(tmp_path)
    try:
        monkeypatch.setattr("builtins.input", lambda prompt="": "no thanks")                 # anything but SEND skips them
        await run_harness(monkeypatch, run_args(url, tmp_path / "ask", only="part-lock-confirm,inj-admin", confirm_risk="ask"))
        printed = capsys.readouterr().out
        assert "could make the agent raise a confirmation request" in printed and "Front door" in printed and "DECLINES" in printed
        assert {r["correct"] for r in read_csv(str(tmp_path / "ask.csv"))} == {"skip"}
        assert not [c for c in e.fake.calls if c["tool"] == "set_state" and c["meta"].get("call_id")]       # the agent was never asked to act
    finally:
        await server.stop()
        await e.close()


# --------------------------------------------------------------------------------------------- merge / compare
def test_merge_adds_server_numbers_by_req_id_and_warns_on_a_wrong_config(tmp_path, capsys):
    rows = [{"config": "new", "id": "a", "step": 1, "category": "command", "phrase": "p", "correct": "yes", "wrong_device": "no", "latency_ms": 2000, "req_id": "r1"},
            {"config": "new", "id": "b", "step": 1, "category": "chat", "phrase": "q", "correct": "no", "wrong_device": "no", "latency_ms": 4000, "req_id": "r2"},
            {"config": "new", "id": "c", "step": 1, "category": "chat", "phrase": "skipped", "correct": "skip"}]
    res = tmp_path / "r.json"
    res.write_text(json.dumps({"label": "new", "rows": rows}), encoding="utf-8")
    traces = tmp_path / "t.json"
    flags = {"deterministic_fixes": True, "generated_prompt": True, "chat_route": True, "compact_json": False}
    traces.write_text(json.dumps([
        {"req_id": "r1", "route": "fast", "decision": None, "llm_calls": 0, "prompt_eval_tokens": 0, "cache_hit_tokens": 0, "s15": flags},
        {"req_id": "r2", "route": "llm", "decision": "chat", "llm_calls": 2, "prompt_eval_tokens": 90, "cache_hit_tokens": 1000, "s15": flags}]), encoding="utf-8")
    run_eval.cmd_merge(Namespace(results=str(res), traces=str(traces), out=str(tmp_path / "m.csv")))
    got = {r["id"]: r for r in read_csv(tmp_path / "m.csv")}
    assert got["b"]["prompt_eval_tokens"] == "90" and got["b"]["cache_hit_tokens"] == "1000" and got["b"]["decision"] == "chat"
    assert json.loads(got["b"]["config_flags"]) == flags and "WARNING" not in capsys.readouterr().out
    res.write_text(json.dumps({"label": "baseline", "rows": rows}), encoding="utf-8")                  # labelled baseline but the flags are on
    run_eval.cmd_merge(Namespace(results=str(res), traces=str(traces), out=str(tmp_path / "m2.csv")))
    assert "WARNING: label 'baseline'" in capsys.readouterr().out
    s = ek.summarize(read_csv(tmp_path / "m.csv"))
    assert s["phrases"] == 2 and s["correct"] == 1 and s["llm_turns"] == 1 and s["mean_eval_tokens_per_llm_turn"] == 90.0


def test_compare_lists_the_phrases_that_differ(tmp_path, capsys):
    def mk(name, ok):
        p = tmp_path / f"{name}.csv"
        run_eval.write_csv(p, [{"config": name, "id": "x", "step": "1", "phrase": "turn on the Fan", "correct": ok, "wrong_device": "no", "latency_ms": "1000"}])
        return str(p)
    run_eval.cmd_compare(Namespace(csvs=[mk("baseline", "no"), mk("new", "yes")]))
    printed = capsys.readouterr().out
    assert "baseline" in printed and "x.1" in printed and "baseline:no" in printed and "new:yes" in printed


# --------------------------------------------------------------------------------------------- compact JSON grammar flag
def test_compact_json_flag_is_off_by_default_and_forces_the_gbnf_backend():
    assert SchedConfig().compact_json is False and SchedConfig().constraint_backend == "json_schema"
    c = SchedConfig.from_dict({"compact_json": True})
    assert c.compact_json and c.constraint_backend == "gbnf"
    assert SchedConfig.from_dict({"compact_json": False}).constraint_backend == "json_schema"


def test_compact_grammar_accepts_only_compact_plans_and_the_default_grammar_is_unchanged():
    reg = DeviceRegistry()
    reg.replace(copy.deepcopy(REGISTRY), 1)
    compact = ConstraintCache().get(reg, None, "", 0, False, True)
    normal = ConstraintCache().get(reg, None, "", 0, False, False)
    a = compact.aliases.alias("ep_c0000001")
    two = f'[["s","{a}","power",true],["s","{a}","brightness",50]]'
    spaced = f'[[ "s", "{a}", "power", true ], [ "s", "{a}", "brightness", 50 ]]'
    assert to_regex(compact.gbnf).fullmatch(two) and not to_regex(compact.gbnf).fullmatch(spaced) and to_regex(compact.gbnf).fullmatch("[]")
    assert to_regex(normal.gbnf).fullmatch(spaced) and to_regex(normal.gbnf).fullmatch(two)          # the default grammar still allows both
    assert " ws" not in compact.gbnf and "ws ::=" not in compact.gbnf and "ws ::=" in normal.gbnf
    assert compact.key != normal.key and len(compact.gbnf) < len(normal.gbnf)
    # a string literal containing the letters "ws" survives the whitespace stripping
    from agent.sched.grammar import _strip_ws
    assert _strip_ws('"[" ws "ws" ws "]"') == '"[" "ws" "]"'


async def test_compact_flag_sends_the_compact_grammar_to_the_server_and_the_trace_records_the_flags(tmp_path):
    e = await custom_env(tmp_path, copy.deepcopy(REGISTRY), route_mode="llm_first", compact_json=True, generated_prompt=True, chat_route=True,
                         deterministic_fixes=True)
    try:
        assert e.agent.sched.cfg.constraint_backend == "gbnf" and e.agent.llm.constraint.name == "gbnf"
        body = e.agent.llm.constraint
        schema = e.agent.sched.schema()
        sent = {}
        body.apply(sent, schema)
        assert "grammar" in sent and " ws" not in sent["grammar"] and "response_format" not in sent
        e.llm.registry = e.agent.registry
        await e.chat("hello there")
        t = e.agent.store.traces(1)[0]["detail"]
        assert t["s15"] == {"deterministic_fixes": True, "generated_prompt": True, "chat_route": True, "compact_json": True}
    finally:
        await e.close()
