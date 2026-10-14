"""sim-esp32 command line: start N virtual nodes from a YAML file."""
import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from .clock import Clock
from .config import ConfigError, load_config, set_fault
from .creds import CredError, credentials_for
from .node import PREFIX, Node
from .schemas import Validator
from .transport import MemoryBus, MemoryTransport, PahoTransport


def build_parser():
    p = argparse.ArgumentParser(prog="simesp32", description="Virtual ESP32 nodes speaking the edge-agent contract.")
    p.add_argument("config", help="YAML file describing the nodes (see examples/)")
    p.add_argument("--host", default=os.environ.get("SIM_MQTT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("SIM_MQTT_PORT", "8883")))
    p.add_argument("--ca", default=os.environ.get("SIM_MQTT_CA"), help="CA certificate (PEM) that signed the broker cert")
    p.add_argument("--creds-dir", default=os.environ.get("SIM_MQTT_CREDS_DIR"),
                   help="folder with <node_id>.cred files (username=/password= lines), as written by "
                        "deploy/mosquitto/dynsec-node-user.ps1. Env alternatives: SIM_MQTT_PASSWORD[_<NODE_ID>]")
    p.add_argument("--dedup-size", type=int, default=None, help="cmd_id dedup ring size for every node (default: YAML value or 16)")
    p.add_argument("--contracts", default=None, help="contracts/ folder (default: <repo>/contracts)")
    p.add_argument("--only", action="append", default=[], help="start only these node_ids (repeatable)")
    p.add_argument("--fault", action="append", default=[], metavar="[NODE:]KEY=VALUE",
                   help="fault injection, e.g. drop_ack=0.3, delay_ack_s=4, duplicate_cmd=1, flapping=10:5, "
                        "malformed_announce=invalid_json. Prefix with a node_id to target one node.")
    p.add_argument("--mock", action="store_true", help="no broker: in-memory bus, messages echoed to stdout; "
                   "stdin lines '<node_id> <cmd json>' inject commands")
    p.add_argument("--check", action="store_true", help="validate the YAML and its announces, print a summary, exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def apply_cli_faults(nodes, specs):
    for spec in specs:
        target, _, rest = spec.partition("=")
        key, val = target, rest
        node_id = None
        if ":" in target and target.split(":", 1)[0].startswith("esp32-"):
            node_id, key = target.split(":", 1)
        hit = [n for n in nodes if node_id in (None, n.node_id)]
        if not hit:
            raise ConfigError(f"--fault {spec!r}: no such node")
        for n in hit:
            set_fault(n.faults, key, val)


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        nodes = load_config(args.config)
        if args.only:
            nodes = [n for n in nodes if n.node_id in args.only]
            if not nodes:
                raise ConfigError("--only matched no node")
        apply_cli_faults(nodes, args.fault)
        if args.dedup_size is not None:
            if args.dedup_size < 1:
                raise ConfigError("--dedup-size must be >= 1")
            for n in nodes:
                n.dedup_size = args.dedup_size
        validator = Validator(Path(args.contracts) if args.contracts else None)
        for n in nodes:                                   # the announce must satisfy the schema unless faulted on purpose
            if n.faults.malformed_announce is None:
                validator.validate("announce", n.announce())
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.check:
        for n in nodes:
            eps = ", ".join(f"{e.local_id}:{e.type}" for e in n.endpoints)
            print(f"ok {n.node_id} [{eps}]" + (f" faults={n.faults}" if n.faults != type(n.faults)() else ""))
        return 0

    clock = Clock()
    bus = MemoryBus(echo=print) if args.mock else None
    runtime = []
    try:
        if not args.mock:
            if not args.ca:
                raise CredError("--ca (or SIM_MQTT_CA) is required: the broker is TLS-only and the sim always verifies it")
            if not Path(args.ca).is_file():
                raise CredError(f"CA file not found: {args.ca}")
        for cfg in nodes:
            will = f"{PREFIX}{cfg.node_id}/status"
            if args.mock:
                tr = MemoryTransport(cfg.node_id, bus, clock, will)
            else:
                user, pw = credentials_for(cfg.node_id, args.creds_dir, len(nodes) == 1)
                tr = PahoTransport(cfg.node_id, args.host, args.port, args.ca, user, pw, will)
            runtime.append(Node(cfg, tr, validator, clock))
    except CredError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for n in runtime:
        n.boot()
    for n in runtime:
        n.t.start()
    print(f"{len(runtime)} node(s) running ({'mock bus' if args.mock else f'{args.host}:{args.port} TLS'}). Ctrl-C to stop.")
    if args.mock:
        threading.Thread(target=_stdin_injector, args=(bus,), daemon=True).start()
    try:
        while any(not n.stopped for n in runtime):
            for n in runtime:
                if args.mock:
                    n.t.poll()
                n.tick()
            time.sleep(0.1)
        print("all nodes unprovisioned/stopped")
    except KeyboardInterrupt:
        pass
    finally:
        for n in runtime:
            n.shutdown()
    for n in runtime:
        print(n.id, json.dumps(n.stats))
    return 0


def _stdin_injector(bus):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        nid, _, body = line.partition(" ")
        try:
            bus.inject(nid, body)
        except KeyError:
            print(f"unknown node {nid}", file=sys.stderr)
