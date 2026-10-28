#!/usr/bin/env python3
"""Start / stop / inspect the saved simulated nodes (backs start-sims.bat and stop-sims.bat).

State lives in .sim/ (git-ignored):
  nodes.json                  manifest: [{label, profile, node_id, gateway?}]  (no secrets)
  <node_id>/ca.pem, <node_id>.cred   written by provision-sim.ps1
  pids                        JSON list of the sims this tool started: [{pid, node_id, label, started}]
  logs/<node_id>.log          output of each sim

Safety: a process is only ever stopped if it is recorded in a pid file AND its command line still shows this tool's
sim_node.py with that node id (a recycled PID is never touched), or if its command line shows this tool's sim_node.py
(leftovers from a crashed controller or an older start-room.ps1). Nothing is ever stopped by image name."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from profiles import PROFILES  # noqa: E402
from sim_node import ALIAS_RE, resolve_node_id  # noqa: E402

SIM_SCRIPT = HERE / "sim_node.py"
NODE_ID_DIR_RE_PREFIX = "esp32-"


class Paths:
    def __init__(self, sim_dir: Path):
        self.sim = Path(sim_dir)
        self.manifest = self.sim / "nodes.json"
        self.pids = self.sim / "pids"
        self.legacy_pids = self.sim / "room.pids.json"      # written by start-room.ps1
        self.logs = self.sim / "logs"


def default_paths() -> Paths:
    return Paths(Path(os.environ.get("SIM_DIR") or HERE / ".sim"))


# ------------------------------------------------------------------ manifest
def load_manifest(p: Paths) -> list:
    try:
        data = json.loads(p.manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and {"label", "profile", "node_id"} <= set(e)] \
        if isinstance(data, list) else []


def save_manifest(p: Paths, entries: list) -> None:
    p.sim.mkdir(parents=True, exist_ok=True)
    tmp = p.manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p.manifest)


def register(p: Paths, label: str, profile: str, node_id: str | None = None, gateway: str | None = None) -> dict:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r} (valid: {', '.join(sorted(PROFILES))})")
    if not ALIAS_RE.match(label):
        raise ValueError(f"label {label!r}: use [A-Za-z0-9._-], max 48 chars")
    nid = resolve_node_id(node_id or label)
    entries = [e for e in load_manifest(p) if e["node_id"] != nid]
    entry = {"label": label, "profile": profile, "node_id": nid}
    if gateway:
        entry["gateway"] = gateway
    else:
        old = next((e for e in load_manifest(p) if e["node_id"] == nid), {})
        if "gateway" in old:
            entry["gateway"] = old["gateway"]
    entries.append(entry)
    save_manifest(p, sorted(entries, key=lambda e: e["label"]))
    return entry


def saved_node_ids(p: Paths) -> list:
    """Node ids that have credentials on disk."""
    if not p.sim.is_dir():
        return []
    return sorted(d.name for d in p.sim.iterdir()
                  if d.is_dir() and (d / f"{d.name}.cred").is_file() and (d / "ca.pem").is_file())


def guess_label(node_id: str):
    """One-time migration for nodes provisioned before the manifest existed: try the labels this tool's docs and
    start-room.ps1 produce (sim-<profile>-NN) and keep the one whose derived id matches the saved folder."""
    for profile in sorted(PROFILES):
        for n in range(1, 100):
            label = f"sim-{profile}-{n:02d}"
            if resolve_node_id(label) == node_id:
                return label, profile
    return None


def migrate(p: Paths) -> tuple:
    """Adds saved-credential nodes missing from the manifest. Returns (added entries, unresolved node ids)."""
    known = {e["node_id"] for e in load_manifest(p)}
    added, unresolved = [], []
    for nid in saved_node_ids(p):
        if nid in known:
            continue
        g = guess_label(nid)
        if g:
            added.append(register(p, g[0], g[1], nid))
        else:
            unresolved.append(nid)
    return added, unresolved


# ------------------------------------------------------------------ processes
def _cmdline(proc) -> str:
    try:
        return " ".join(proc.cmdline())
    except (psutil.Error, OSError):
        return ""


def is_our_sim(pid: int, node_id: str | None = None) -> bool:
    """True only if `pid` is alive and its command line is this tool's sim_node.py (and that node id, if given)."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
    except psutil.Error:
        return False
    cl = _cmdline(proc)
    return str(SIM_SCRIPT).lower() in cl.lower() and (node_id is None or node_id in cl)


def _read_pid_file(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("pid"), int)] if isinstance(data, list) else []


def _terminate(pid: int) -> None:
    try:
        proc = psutil.Process(pid)
        tree = proc.children(recursive=True) + [proc]    # a venv python.exe on Windows is a launcher with a child
        for q in tree:
            try:
                q.terminate()             # hard stop on Windows: the broker fires the LWT, the node shows offline
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(tree, timeout=3)
        for q in alive:
            try:
                q.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


def stop_all(p: Paths, say=print) -> list:
    """Stops recorded sims, then any other process running this tool's sim_node.py. Returns the PIDs stopped."""
    stopped, seen = [], set()
    for f in (p.pids, p.legacy_pids):
        for rec in _read_pid_file(f):
            pid, nid = rec["pid"], rec.get("node_id")
            seen.add(pid)
            if is_our_sim(pid, nid):
                _terminate(pid)
                stopped.append(pid)
                say(f"stopped {rec.get('label', nid)} ({nid}) pid {pid}")
            else:
                say(f"skip {rec.get('label', nid)}: pid {pid} is not that sim any more (already stopped or PID reused)")
    me = os.getpid()
    for proc in psutil.process_iter():
        if proc.pid in seen or proc.pid == me:
            continue
        if str(SIM_SCRIPT).lower() in _cmdline(proc).lower() and "simctl" not in _cmdline(proc).lower():
            _terminate(proc.pid)
            stopped.append(proc.pid)
            say(f"stopped leftover sim_node.py pid {proc.pid}")
    for f in (p.pids, p.legacy_pids):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    return stopped


def launch(p: Paths, entry: dict, python: str = sys.executable, extra_args=()) -> subprocess.Popen:
    p.logs.mkdir(parents=True, exist_ok=True)
    log = open(p.logs / f"{entry['node_id']}.log", "wb")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [python, "-u", str(SIM_SCRIPT), "--profile", entry["profile"], "--node-id", entry["node_id"],
           "--sim-dir", str(p.sim), *extra_args]
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=flags, close_fds=True)
    finally:
        log.close()


def start_all(p: Paths, say=print, python: str = sys.executable, extra_args=()) -> list:
    stop_all(p, say)
    added, unresolved = migrate(p)
    for e in added:
        say(f"registered {e['label']} ({e['profile']}) -> {e['node_id']}")
    for nid in unresolved:
        say(f"cannot tell the profile of {nid}; run:  python simctl.py register --label <label> --profile <profile> "
            f"--node-id {nid}")
    started = []
    for e in load_manifest(p):
        d = p.sim / e["node_id"]
        if not ((d / f"{e['node_id']}.cred").is_file() and (d / "ca.pem").is_file()):
            say(f"skip {e['label']}: no saved credentials in {d}")
            continue
        proc = launch(p, e, python, extra_args)
        started.append({"pid": proc.pid, "node_id": e["node_id"], "label": e["label"], "profile": e["profile"],
                        "started": int(time.time())})
        say(f"started {e['label']:<20} {e['node_id']}  pid {proc.pid}")
    p.sim.mkdir(parents=True, exist_ok=True)
    p.pids.write_text(json.dumps(started, indent=2) + "\n", encoding="utf-8")
    return started


# ------------------------------------------------------------------ status
def last_log_line(p: Paths, node_id: str) -> str:
    try:
        lines = (p.logs / f"{node_id}.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lines = [ln for ln in lines if ln.strip()]
    return lines[-1] if lines else ""


def classify(p: Paths, rec: dict) -> tuple:
    """(state, hint) for one started sim."""
    nid = rec["node_id"]
    log = last_log_line(p, nid)
    if is_our_sim(rec["pid"], nid):
        return "running", ""
    if "credentials rejected" in log:
        gw = next((e.get("gateway") for e in load_manifest(p) if e["node_id"] == nid), None) or "<gateway>"
        return "crashed", (f"credentials rejected/expired: re-provision with  .\\provision-sim.ps1 -NodeId "
                           f"{rec.get('label', nid)} -GatewayUrl {gw} -Profile {rec.get('profile', '<profile>')}")
    return "crashed", "see " + str(p.logs / f"{nid}.log")


def status_table(p: Paths) -> tuple:
    rows, hints = [], []
    for rec in _read_pid_file(p.pids):
        state, hint = classify(p, rec)
        rows.append((rec.get("label", "?"), rec["node_id"], state, last_log_line(p, rec["node_id"])[:70]))
        if hint:
            hints.append(f"{rec.get('label')}: {hint}")
    return rows, hints


def print_status(p: Paths, say=print) -> int:
    rows, hints = status_table(p)
    if not rows:
        say("no sims are tracked (nothing started)")
        return 0
    w = [max(len(r[i]) for r in rows + [("label", "node id", "state", "")]) for i in range(3)]
    say(f"{'label':<{w[0]}}  {'node id':<{w[1]}}  {'state':<{w[2]}}  last log line")
    for r in rows:
        say(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]}")
    for h in hints:
        say("!! " + h)
    return 0 if all(r[2] == "running" for r in rows) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="simctl.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("--wait", type=float, default=10.0)
    s.add_argument("--host", default=None, help="broker host passed to every sim (default: sim_node.py default)")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("migrate")
    sub.add_parser("list")
    r = sub.add_parser("register", help="add a node to the manifest")
    r.add_argument("--label", required=True)
    r.add_argument("--profile", required=True, choices=sorted(PROFILES))
    r.add_argument("--node-id", default=None, help="esp32-<12 hex> (default: derived from the label)")
    r.add_argument("--gateway", default=None, help="gateway URL, remembered for the re-provision hint")
    a = ap.parse_args(argv)
    p = default_paths()
    if a.cmd == "register":
        try:
            e = register(p, a.label, a.profile, a.node_id, a.gateway)
        except ValueError as ex:
            print(f"error: {ex}", file=sys.stderr)
            return 2
        print(f"registered {e['label']} ({e['profile']}) -> {e['node_id']}")
        return 0
    if a.cmd == "migrate":
        added, unresolved = migrate(p)
        for e in added:
            print(f"registered {e['label']} ({e['profile']}) -> {e['node_id']}")
        for nid in unresolved:
            print(f"cannot tell the profile of {nid}; run: python simctl.py register --label <label> "
                  f"--profile <profile> --node-id {nid}")
        return 0
    if a.cmd == "list":
        for e in load_manifest(p):
            ok = "creds" if (p.sim / e["node_id"] / f"{e['node_id']}.cred").is_file() else "NO CREDS"
            print(f"{e['label']:<20} {e['profile']:<8} {e['node_id']}  {ok}")
        return 0
    if a.cmd == "stop":
        stopped = stop_all(p)
        print(f"{len(stopped)} sim process(es) stopped.")
        return 0
    if a.cmd == "status":
        return print_status(p)
    extra = ["--host", a.host] if a.host else []
    started = start_all(p, extra_args=extra)
    if not started:
        print("nothing to start: no node with saved credentials (provision one with provision-sim.ps1 -Profile ...)")
        return 1
    print(f"waiting {a.wait:g} s ...", flush=True)
    time.sleep(a.wait)
    return print_status(p)


if __name__ == "__main__":
    sys.exit(main())
