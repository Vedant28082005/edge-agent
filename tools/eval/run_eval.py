"""S15 evaluation harness: runs the phrase list in phrases.json against a LIVE agent through the gateway, from the PC.

    python tools/eval/run_eval.py run     --base-url https://pi-brain.TAILNET.ts.net --user NAME --label baseline|new|compact --out eval-baseline
    python tools/eval/run_eval.py merge   --results eval-baseline.json --traces traces.json --out eval-baseline.csv
    python tools/eval/run_eval.py compare eval-baseline.csv eval-new.csv eval-compact.csv

`run` logs in (password prompt, never stored or printed), reads the live registry (so every phrase uses the REAL device names), puts
the non-risk devices back to a known state before each case, sends each phrase to POST /chat and reads the SSE stream (tool_call /
tool_result / confirmation_request / final), and scores it against the expectation (evalkit.py). Writes <out>.json and <out>.csv.
`merge` adds the server's own per-request numbers (tokens evaluated, cache hits, decision, S15 flags) by req_id from a trace dump
(tools/dump-traces.ps1). `compare` puts the CSVs side by side.

Safety: risk:confirm devices (locks) are never touched by a reset or a setup. A phrase that could raise a confirmation request for
one is listed first and only sent after you type SEND (or pass --confirm-risk send); the harness NEVER approves a confirmation, it
declines every request it sees, so nothing on those devices is executed. Only the gateway's public API is used."""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evalkit as ek  # noqa: E402


class RateLimited(Exception):
    def __init__(self, wait: float) -> None:
        self.wait = wait


class Gateway:
    def __init__(self, base: str, user: str, password: str, verify: bool = True) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=httpx.Timeout(30, connect=10), verify=verify)
        r = self.http.post(f"{self.base}/auth/login", json={"username": user, "password": password})
        if r.status_code != 200:
            raise SystemExit(f"login failed for {user!r}: HTTP {r.status_code}")
        j = r.json()
        self.access, self.refresh, self.role = j["access_token"], j["refresh_token"], j.get("role", "")

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.access}"}

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.http.request(method, f"{self.base}{path}", headers=self._h(), **kw)
        if r.status_code == 401 and self.refresh:
            rr = self.http.post(f"{self.base}/auth/refresh", json={"refresh_token": self.refresh})
            if rr.status_code == 200:
                self.access, self.refresh = rr.json()["access_token"], rr.json().get("refresh_token", self.refresh)
                r = self.http.request(method, f"{self.base}{path}", headers=self._h(), **kw)
        return r

    def devices(self) -> list[dict]:
        r = self._call("GET", "/devices")
        r.raise_for_status()
        return r.json()

    def set(self, ep: str, cap: str, value) -> bool:
        r = self._call("POST", f"/devices/{ep}/set", json={"capability": cap, "value": value})
        return r.status_code == 200

    def decline(self, request_id: str) -> bool:
        return self._call("POST", f"/confirmations/{request_id}/decline").status_code in (200, 204)

    def chat(self, session: str, text: str, timeout: float = 240.0) -> ek.Outcome:
        out, calls, declines = ek.Outcome(), {}, []
        t0 = time.monotonic()
        try:
            with self.http.stream("POST", f"{self.base}/chat", json={"session_id": session, "text": text}, headers=self._h(),
                                  timeout=httpx.Timeout(timeout, connect=10)) as r:
                if r.status_code == 429:
                    raise RateLimited(float(r.headers.get("retry-after") or 5))
                if r.status_code >= 400:
                    out.error = f"http {r.status_code}"
                    return out
                ev = ""
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:])
                    except ValueError:
                        continue
                    out.req_id = out.req_id or str(d.get("req_id") or "")
                    if ev == "tool_call":
                        row = {"tool": d.get("tool"), "args": d.get("args") or {}, "ok": None}
                        calls[d.get("call_id")] = row
                        out.tools.append(row)
                    elif ev == "tool_result" and d.get("call_id") in calls:
                        calls[d["call_id"]]["ok"] = bool(d.get("ok"))
                    elif ev == "confirmation_request":
                        out.confirmation_requested = True
                        declines.append(d.get("request_id"))
                    elif ev == "error":
                        out.error = str(d.get("code") or "error")
                        break
                    elif ev == "final":
                        out.reply, out.route, out.server_ms = str(d.get("text") or ""), str(d.get("route") or ""), int(d.get("latency_ms") or 0)
                        out.wall_ms = int((time.monotonic() - t0) * 1000)
                        break
        except httpx.HTTPError as e:
            out.error = type(e).__name__
        for rid in declines:                      # NEVER approve: declining a request executes nothing
            if rid:
                self.decline(rid)
        if not out.wall_ms:
            out.wall_ms = int((time.monotonic() - t0) * 1000)
        return out


def password_for(who: str, env: str) -> str:
    return os.environ.get(env) or getpass.getpass(f"Password for {who}: ")


# --------------------------------------------------------------------------------------------- reset / setup
def reset_devices(gw: Gateway, roles: ek.Roles, eps, settle: float) -> int:
    n = 0
    for ep in eps:
        d = roles.devices.get(ep)
        if not d or d.get("risk") == "confirm":
            continue                                         # a risk:confirm device is never touched by this tool
        caps = ek._caps(d)
        state = d.get("state") or {}
        if "power" in caps and caps["power"].get("kind", "actuator") == "actuator" and state.get("power") is not False:
            n += gw.set(ep, "power", False)
        if "position" in caps and caps["position"].get("kind", "actuator") == "actuator" and state.get("position") not in (0, None):
            n += gw.set(ep, "position", 0)
    if n and settle:
        time.sleep(settle)
    return n


def apply_setup(gw: Gateway, roles: ek.Roles, setup: list, settle: float) -> None:
    for s in setup:
        d = roles.devices.get(s["ep"])
        if d and d.get("risk") != "confirm":
            gw.set(s["ep"], s["cap"], s["value"])
    if setup and any(s["cap"] == "position" for s in setup):
        time.sleep(settle)


# --------------------------------------------------------------------------------------------- run
def cmd_run(a) -> None:
    raw = json.loads(Path(a.phrases).read_text(encoding="utf-8"))
    if a.only:
        wanted = set(a.only.split(","))
        raw = [c for c in raw if c["id"] in wanted or c["cat"] in wanted]
    gw = Gateway(a.base_url, a.user, password_for(a.user, "EDGE_EVAL_PASSWORD"), not a.insecure)
    guest = Gateway(a.base_url, a.guest_user, password_for(a.guest_user, "EDGE_EVAL_GUEST_PASSWORD"), not a.insecure) if a.guest_user else None
    registry = gw.devices()
    roles = ek.resolve_roles(registry)
    cases = ek.expand(raw, roles, have_guest=guest is not None)
    print(f"\nRegistry: {len(roles.devices)} online devices. Roles: " + ", ".join(f"{k}={roles.vars[k]}" for k in sorted(roles.vars)
                                                                                 if k in ("fan", "light_unique", "light_pair", "cover", "outlet", "lock", "temp_sensor", "room", "other_room")))
    runnable = [c for c in cases if not c.skip]
    risky = [s for c in runnable for s in c.steps if s.risk_confirm]
    send_risky = False
    if risky:
        names = ", ".join(sorted({roles.name(e) for e, d in roles.devices.items() if d.get("risk") == "confirm"})) or "(none found)"
        print(f"\n{len(risky)} phrase(s) could make the agent raise a confirmation request for a risk:confirm device ({names}):")
        for s in risky:
            print(f"   - [{s.case}] {s.say!r}")
        print("This tool never approves a confirmation - it DECLINES every request, so nothing on those devices is executed.")
        if a.confirm_risk == "send":
            send_risky = True
        elif a.confirm_risk == "ask":
            send_risky = input("Type SEND to include them, anything else to skip them: ").strip() == "SEND"
    skipped = [c for c in cases if c.skip]
    print(f"\n{sum(len(c.steps) for c in runnable)} steps in {len(runnable)} cases will run; {len(skipped)} case(s) skipped: "
          + "; ".join(f"{c.id} ({c.skip})" for c in skipped))
    if not a.yes and input("Start? [Y/n] ").strip().lower() in ("n", "no"):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rows: list[dict] = []
    touched: set[str] = set(roles.devices)
    print("Resetting devices to a known state ...")
    reset_devices(gw, roles, touched, a.settle)
    touched.clear()
    print("Warm-up turn (not recorded) ...")
    gw.chat(f"eval-{stamp}-warmup", "hello")
    for case in cases:
        if case.skip:
            rows.append({"config": a.label, "id": case.id, "step": 0, "category": case.cat, "role": case.role, "phrase": "", "correct": "skip", "notes": case.skip})
            continue
        who = guest if case.role == "guest" else gw
        reset_devices(gw, roles, touched, a.settle)
        touched.clear()
        apply_setup(gw, roles, case.setup, a.settle)
        touched.update(s["ep"] for s in case.setup)
        session = f"eval-{stamp}-{case.id}"
        for st in case.steps:
            if st.risk_confirm and not send_risky:
                rows.append({"config": a.label, "id": case.id, "step": st.index, "category": case.cat, "role": case.role, "phrase": st.say,
                             "correct": "skip", "notes": "risk:confirm phrase not approved"})
                continue
            for attempt in range(3):
                try:
                    out = who.chat(session, st.say)
                    break
                except RateLimited as rl:
                    time.sleep(min(60, rl.wait))
            else:
                out = ek.Outcome(error="rate_limited")
            ok, wrong, notes = ek.score(st.expect, out, roles)
            touched.update((t.get("args") or {}).get("endpoint_id") for t in out.tools if (t.get("args") or {}).get("endpoint_id"))
            rows.append({"config": a.label, "id": case.id, "step": st.index, "category": case.cat, "role": case.role, "phrase": st.say,
                         "route": out.route, "tools_called": ek.tools_string(out, roles), "correct": "yes" if ok else "no",
                         "wrong_device": "yes" if wrong else "no", "latency_ms": out.wall_ms, "server_latency_ms": out.server_ms,
                         "reply": out.reply[:160], "notes": "; ".join(notes), "req_id": out.req_id})
            print(f"  {'ok ' if ok else 'BAD'} {case.id}.{st.index:<2} {out.wall_ms:>6} ms  {st.say[:48]!r} -> {out.reply[:60]!r}" + ("" if ok else f"   [{'; '.join(notes)}]"))
            time.sleep(a.pause)
    reset_devices(gw, roles, touched | set(roles.devices), a.settle)
    result = {"label": a.label, "started": stamp, "rows": rows}
    Path(a.out + ".json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    write_csv(Path(a.out + ".csv"), rows)
    scored = [r for r in rows if r["correct"] in ("yes", "no")]
    print(f"\n{sum(r['correct'] == 'yes' for r in scored)}/{len(scored)} correct, {sum(r.get('wrong_device') == 'yes' for r in scored)} wrong-device. "
          f"Wrote {a.out}.json and {a.out}.csv (run `merge` with a trace dump for the token columns).")


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=ek.COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)


def cmd_merge(a) -> None:
    res = json.loads(Path(a.results).read_text(encoding="utf-8"))
    traces = {t.get("req_id"): t for t in json.loads(Path(a.traces).read_text(encoding="utf-8")) if t.get("req_id")}
    rows = [ek.merge_trace(r, traces.get(r.get("req_id"))) for r in res["rows"]]
    missing = sum(1 for r in rows if r.get("req_id") and r["req_id"] not in traces)
    write_csv(Path(a.out), rows)
    Path(a.results).write_text(json.dumps({**res, "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    warn = ek.flag_warning(res["label"], rows)
    print(f"merged {len(rows) - missing}/{len(rows)} rows with traces -> {a.out}" + (f" ({missing} had no trace: dump more rows)" if missing else ""))
    if warn:
        print(warn)


def cmd_compare(a) -> None:
    sets = []
    for p in a.csvs:
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        sets.append((rows[0]["config"] if rows else Path(p).stem, rows))
    print(f"{'config':<10} {'phrases':>7} {'correct':>8} {'wrong-dev':>9} {'mean ms':>8} {'LLM turns':>9} {'eval tok/LLM turn':>18} {'cache tok/LLM turn':>19}")
    for name, rows in sets:
        s = ek.summarize(rows)
        print(f"{name:<10} {s['phrases']:>7} {s['correct']:>8} {s['wrong_device']:>9} {s['mean_latency_ms'] or '-':>8} {s['llm_turns']:>9} "
              f"{s['mean_eval_tokens_per_llm_turn'] or '-':>18} {s['mean_cache_hit_tokens_per_llm_turn'] or '-':>19}")
    by_id = [{(r["id"], r["step"]): r for r in rows} for _, rows in sets]
    keys = sorted({k for d in by_id for k in d})
    diff = [k for k in keys if len({d.get(k, {}).get("correct") for d in by_id}) > 1]
    if diff:
        print("\nPhrases whose result differs between configs:")
        for k in diff:
            print(f"  {k[0]}.{k[1]:<2} " + "  ".join(f"{n}:{d.get(k, {}).get('correct', '-')}" for (n, _), d in zip(sets, by_id))
                  + f"   {next((d[k]['phrase'] for d in by_id if k in d), '')!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--base-url", required=True, help="the gateway, e.g. https://pi-brain.tailnet.ts.net")
    r.add_argument("--user", required=True)
    r.add_argument("--guest-user", default="", help="a guest account; without it the guest cases are skipped")
    r.add_argument("--label", required=True, choices=sorted(ek.EXPECTED_FLAGS), help="baseline = flags off, new = S15 flags on, compact = new + compact_json")
    r.add_argument("--out", required=True, help="output base name (writes .json and .csv)")
    r.add_argument("--phrases", default=str(Path(__file__).with_name("phrases.json")))
    r.add_argument("--only", default="", help="comma list of case ids or categories")
    r.add_argument("--confirm-risk", choices=["ask", "send", "skip"], default="ask")
    r.add_argument("--settle", type=float, default=3.0, help="seconds to wait after a cover reset/setup")
    r.add_argument("--pause", type=float, default=0.5)
    r.add_argument("--yes", action="store_true", help="do not ask 'Start?' (the risk:confirm question is separate: --confirm-risk)")
    r.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    m = sub.add_parser("merge")
    m.add_argument("--results", required=True)
    m.add_argument("--traces", required=True)
    m.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("csvs", nargs="+")
    a = ap.parse_args()
    {"run": cmd_run, "merge": cmd_merge, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()
