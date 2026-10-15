"""`simesp32 --ble-provision`: virtual nodes that boot WITHOUT credentials, open a BLE provisioning window and only join
the broker after a client has written a valid bundle over the (fake, loopback) GATT peer. The full S2b flow without
radio hardware: admin session -> BLE payload -> node connects -> announce -> pending.

stdout protocol (parsed by tools\\e2e-smoke.ps1):  `BLE-GATT <node_id> 127.0.0.1:<port>`  per node, then `ble ready`;
later `BLE-STORED <node_id>` and `BLE-CONNECTING <node_id>`. Secrets are never printed.
"""
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from .ble import BleBoard, Policy, new_pop, serve_board
from .node import PREFIX, Node
from .transport import MemoryBus, MemoryTransport, PahoTransport

log = logging.getLogger("sim.bleprov")


def load_or_create_pop(path: Path) -> str:
    """The per-board PoP is the 'printed label' secret: read from a file, created (owner-only) if missing.
    It is never accepted on the command line and never printed."""
    path = Path(path)
    if path.exists():
        return path.read_text(encoding="ascii").strip()
    pop = new_pop()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(pop + "\n")
    return pop


def run_ble(args, nodes, validator, clock, stop_check=None):
    if args.mock:
        bus = MemoryBus(echo=print)
    elif not args.ca and not args.ble_use_bundle_endpoint:
        print("error: --ca is required (or --ble-use-bundle-endpoint to trust the CA from the BLE bundle)", file=sys.stderr)
        return 2
    pop_dir = Path(args.ble_pop_dir) if args.ble_pop_dir else None
    if pop_dir is None:
        print("error: --ble-pop-dir is required with --ble-provision (one <node_id>.pop file per node)", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="simble-"))
    policy = Policy(window_s=args.ble_window_s, max_failures=args.ble_max_failures, lock_s=args.ble_lock_s)
    boards, servers, runtime, pending = {}, [], [], {}
    for cfg in nodes:
        pop = load_or_create_pop(pop_dir / f"{cfg.node_id}.pop")
        b = BleBoard(cfg.node_id, pop, clock, policy)
        b.boot(False)
        srv, port = serve_board(b)
        servers.append(srv)
        boards[cfg.node_id] = b
        pending[cfg.node_id] = cfg
        print(f"BLE-GATT {cfg.node_id} 127.0.0.1:{port}", flush=True)
    print("ble ready", flush=True)
    try:
        while True:
            if stop_check and stop_check():
                break
            for nid in list(pending):
                b = boards[nid]
                b.tick()
                stored = b.take_stored()
                if stored is None:
                    continue
                cfg = pending.pop(nid)
                print(f"BLE-STORED {nid}", flush=True)
                will = f"{PREFIX}{nid}/status"
                if args.mock:
                    tr = MemoryTransport(nid, bus, clock, will)
                else:
                    ca = tmp / f"{nid}.ca.pem"
                    ca.write_text(stored["ca_pem"], encoding="ascii")
                    host, port = (stored["mqtt_host"], stored["mqtt_port"]) if args.ble_use_bundle_endpoint else (args.host, args.port)
                    tr = PahoTransport(nid, host, port, str(ca), stored["mqtt_user"], stored["mqtt_pass"], will)
                n = Node(cfg, tr, validator, clock)
                n.boot()
                n.t.start()
                runtime.append(n)
                print(f"BLE-CONNECTING {nid}", flush=True)
            for n in runtime:
                if args.mock:
                    n.t.poll()
                n.tick()
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        for n in runtime:
            n.shutdown()
        for s in servers:
            s.shutdown()
    for nid, b in boards.items():
        print(nid, "ble", json.dumps(b.stats))
    for n in runtime:
        print(n.id, json.dumps(n.stats))
    return 0
