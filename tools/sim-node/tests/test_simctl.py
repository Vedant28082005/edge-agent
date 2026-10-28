import json
import subprocess
import sys
import time

import psutil
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

import simctl
import sim_node
from simctl import Paths, SIM_SCRIPT, resolve_node_id

FAN = resolve_node_id("sim-fan-01")
LIGHT = resolve_node_id("sim-light-01")
ODD = "esp32-abcdef012345"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


@pytest.fixture
def p(tmp_path):
    return Paths(tmp_path / ".sim")


def save_creds(p, nid):
    d = p.sim / nid
    d.mkdir(parents=True, exist_ok=True)
    (d / "ca.pem").write_text("x")
    (d / f"{nid}.cred").write_text(f"username={nid}\npassword=x\n")


def fake_sim(nid):
    """A stand-in whose command line looks like this tool's sim: <python> -c ... <sim_node.py> <node_id>."""
    return subprocess.Popen(SLEEPER + [str(SIM_SCRIPT), nid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def procs():
    made = []
    yield made
    for pr in made:
        try:
            for c in psutil.Process(pr.pid).children(recursive=True):
                c.kill()
            pr.kill()
        except psutil.Error:
            pass


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def wait_dead(pid, t=5):
    end = time.time() + t
    while time.time() < end and alive(pid):
        time.sleep(0.05)
    return not alive(pid)


def wait_cmdline(pid, t=5):
    end = time.time() + t
    while time.time() < end and not simctl.is_our_sim(pid):
        time.sleep(0.05)


# ------------------------------------------------------------------ manifest
def test_register_roundtrip_and_upsert(p):
    simctl.register(p, "sim-fan-01", "fan", gateway="https://gw")
    simctl.register(p, "sim-light-01", "light")
    assert [e["label"] for e in simctl.load_manifest(p)] == ["sim-fan-01", "sim-light-01"]
    e = simctl.register(p, "sim-fan-01", "curtain")             # same node id: replaced, not duplicated, gateway kept
    assert len(simctl.load_manifest(p)) == 2
    assert e == {"label": "sim-fan-01", "profile": "curtain", "node_id": FAN, "gateway": "https://gw"}


def test_register_rejects_bad_input(p):
    with pytest.raises(ValueError):
        simctl.register(p, "x", "toaster")
    with pytest.raises(ValueError):
        simctl.register(p, "bad label", "fan")


def test_manifest_has_no_secrets_and_survives_corruption(p):
    simctl.register(p, "sim-fan-01", "fan")
    assert "password" not in p.manifest.read_text()
    p.manifest.write_text("{not json")
    assert simctl.load_manifest(p) == []
    p.manifest.write_text(json.dumps([{"label": "a"}, "junk", {"label": "b", "profile": "fan", "node_id": FAN}]))
    assert [e["label"] for e in simctl.load_manifest(p)] == ["b"]


def test_migrate_guesses_known_labels_and_reports_unknown(p):
    for nid in (FAN, LIGHT, ODD):
        save_creds(p, nid)
    added, unresolved = simctl.migrate(p)
    assert {(e["label"], e["profile"]) for e in added} == {("sim-fan-01", "fan"), ("sim-light-01", "light")}
    assert unresolved == [ODD]
    assert simctl.migrate(p) == ([], [ODD])                     # idempotent
    simctl.register(p, "my-odd-one", "dht", ODD)
    assert simctl.migrate(p) == ([], [])


def test_nodes_without_credentials_are_not_listed(p):
    (p.sim / FAN).mkdir(parents=True)
    assert simctl.saved_node_ids(p) == []


# ------------------------------------------------------------------ processes
def test_is_our_sim_requires_script_and_node_id(procs):
    pr = fake_sim(FAN)
    procs.append(pr)
    wait_cmdline(pr.pid)
    assert simctl.is_our_sim(pr.pid, FAN) and simctl.is_our_sim(pr.pid)
    assert not simctl.is_our_sim(pr.pid, LIGHT)
    assert not simctl.is_our_sim(999_999_999)


def test_stop_skips_stale_and_reused_pids_and_never_touches_unrelated(p, procs):
    other = subprocess.Popen(SLEEPER)                            # unrelated python, PID "reused"
    procs.append(other)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    p.sim.mkdir(parents=True)
    p.pids.write_text(json.dumps([{"pid": other.pid, "node_id": FAN, "label": "sim-fan-01"},
                                  {"pid": dead.pid, "node_id": LIGHT, "label": "sim-light-01"}]))
    out = []
    assert simctl.stop_all(p, out.append) == []
    assert alive(other.pid)
    assert sum(1 for line in out if line.startswith("skip")) == 2
    assert not p.pids.exists()                                   # stale file cleaned


def test_stop_kills_recorded_and_leftover_sims_but_not_unrelated(p, procs):
    rec, left, unrelated = fake_sim(FAN), fake_sim(LIGHT), subprocess.Popen(SLEEPER)
    procs += [rec, left, unrelated]
    wait_cmdline(rec.pid)
    wait_cmdline(left.pid)
    p.sim.mkdir(parents=True)
    p.pids.write_text(json.dumps([{"pid": rec.pid, "node_id": FAN, "label": "sim-fan-01"}]))
    stopped = simctl.stop_all(p)
    assert rec.pid in stopped and stopped            # the venv launcher and its child are one sim; either pid may be reported
    assert wait_dead(rec.pid) and wait_dead(left.pid)
    assert alive(unrelated.pid)


def test_legacy_room_pid_file_is_honoured(p, procs):
    pr = fake_sim(FAN)
    procs.append(pr)
    wait_cmdline(pr.pid)
    p.sim.mkdir(parents=True)
    p.legacy_pids.write_text(json.dumps({"pid": pr.pid, "node_id": FAN, "label": "sim-fan-01"}))   # single-object form
    assert pr.pid in simctl.stop_all(p)
    assert wait_dead(pr.pid) and not p.legacy_pids.exists()


@pytest.fixture
def fake_launch(monkeypatch, procs):
    def launch(p, entry, python=None, extra_args=()):
        pr = fake_sim(entry["node_id"])
        procs.append(pr)
        p.logs.mkdir(parents=True, exist_ok=True)
        (p.logs / f"{entry['node_id']}.log").write_text("connected\n")
        return pr
    monkeypatch.setattr(simctl, "launch", launch)


def test_start_twice_never_leaves_duplicates(p, fake_launch):
    save_creds(p, FAN)
    save_creds(p, LIGHT)
    first = simctl.start_all(p, say=lambda _: None)
    assert {s["label"] for s in first} == {"sim-fan-01", "sim-light-01"}      # migrated from the saved folders
    for s in first:
        wait_cmdline(s["pid"])
    second = simctl.start_all(p, say=lambda _: None)
    for s in first:
        assert wait_dead(s["pid"]), "old sim must be stopped before the new one starts"
    for s in second:
        wait_cmdline(s["pid"])
    ours = {q.pid for q in psutil.process_iter()
            if str(SIM_SCRIPT).lower() in simctl._cmdline(q).lower() and any(n in simctl._cmdline(q) for n in (FAN, LIGHT))}
    assert {s["pid"] for s in second} <= ours
    assert not ({s["pid"] for s in first} & ours)
    assert [r["pid"] for r in json.loads(p.pids.read_text())] == [s["pid"] for s in second]
    simctl.stop_all(p, lambda _: None)


def test_start_skips_nodes_without_credentials(p, fake_launch):
    simctl.register(p, "sim-fan-01", "fan")
    out = []
    assert simctl.start_all(p, say=out.append) == []
    assert any("no saved credentials" in line for line in out)


# ------------------------------------------------------------------ status
def test_status_running_and_rejected_credentials_hint(p, procs):
    simctl.register(p, "sim-fan-01", "fan", gateway="https://gw")
    p.logs.mkdir(parents=True)
    run = fake_sim(FAN)
    procs.append(run)
    wait_cmdline(run.pid)
    (p.logs / f"{FAN}.log").write_text("12:00:00 INFO link up\n")
    rec = {"pid": run.pid, "node_id": FAN, "label": "sim-fan-01", "profile": "fan"}
    assert simctl.classify(p, rec) == ("running", "")
    run.kill()
    wait_dead(run.pid)
    (p.logs / f"{FAN}.log").write_text("12:00:09 ERROR credentials rejected by the broker (expired or removed). Re-run\n")
    state, hint = simctl.classify(p, rec)
    assert state == "crashed" and "provision-sim.ps1 -NodeId sim-fan-01 -GatewayUrl https://gw -Profile fan" in hint
    (p.logs / f"{FAN}.log").write_text("Traceback ...\n")
    state, hint = simctl.classify(p, rec)
    assert state == "crashed" and hint.startswith("see ")


def test_status_table_lists_label_node_state_and_last_line(p):
    p.sim.mkdir(parents=True)
    p.logs.mkdir()
    p.pids.write_text(json.dumps([{"pid": 999_999_999, "node_id": FAN, "label": "sim-fan-01", "profile": "fan"}]))
    (p.logs / f"{FAN}.log").write_text("a\nlast line here\n\n")
    out = []
    assert simctl.print_status(p, out.append) == 1
    text = "\n".join(out)
    assert "sim-fan-01" in text and FAN in text and "crashed" in text and "last line here" in text


# ------------------------------------------------------------------ broker-side behaviour of the sim
def test_auth_refusal_detection():
    assert sim_node.is_auth_refusal(ReasonCode(PacketTypes.CONNACK, "Not authorized"))
    assert sim_node.is_auth_refusal(ReasonCode(PacketTypes.CONNACK, "Bad user name or password"))
    assert not sim_node.is_auth_refusal(ReasonCode(PacketTypes.CONNACK, "Server unavailable"))


def test_auth_watch_counts_consecutive_refusals_only(monkeypatch):
    calls = []
    monkeypatch.setattr(sim_node.PahoTransport, "_on_connect", lambda self, *a: calls.append(a))
    t = object.__new__(sim_node.AuthWatchTransport)
    t.auth_refusals = 0
    bad = ReasonCode(PacketTypes.CONNACK, "Not authorized")
    down = ReasonCode(PacketTypes.CONNACK, "Server unavailable")
    ok = ReasonCode(PacketTypes.CONNACK, "Success")
    for _ in range(2):
        t._on_connect(None, None, None, bad, None)
    assert t.auth_refusals == 2
    t._on_connect(None, None, None, down, None)           # broker busy/starting: not a credential problem
    assert t.auth_refusals == 0
    t._on_connect(None, None, None, bad, None)
    t._on_connect(None, None, None, ok, None)
    assert t.auth_refusals == 0 and len(calls) == 5
