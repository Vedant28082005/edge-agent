"""Reference provisioning client (what the dashboard / Flutter app does), driving a fake GATT peer:

    python -m simesp32.provclient --peer 127.0.0.1:PORT --gateway http://127.0.0.1:8080 \\
        --pop-file X.pop --token-file admin.token --wifi-ssid HomeNet [--bundle-out FILE]

1 read `info` (node_id)  2 POST /admin/provision-sessions {node_id, wifi}  3 BLE handshake + encrypted bundle
4 wait for result `stored`. The admin token comes from a file or $PROV_ADMIN_TOKEN, the Wi-Fi password from
$PROV_WIFI_PASSWORD (never argv). Secrets are never printed; the summary has no credential in it.
Exit codes: 0 stored, 1 provisioning failed (the BLE code is printed), 2 usage/gateway error.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .ble import BleClient, ProvisionError, TcpPeer


def create_session(gateway: str, token: str, node_id: str, ssid: str, wifi_pw: str) -> dict:
    body = json.dumps({"node_id": node_id, "wifi": {"ssid": ssid, "password": wifi_pw}}).encode()
    req = urllib.request.Request(gateway.rstrip("/") + "/admin/provision-sessions", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="simesp32.provclient")
    ap.add_argument("--peer", help="host:port of the fake GATT peer (printed as BLE-GATT by the sim)")
    ap.add_argument("--bleak", metavar="NAME", help="REAL board: advertised name, e.g. Setup-E5F6 (needs `pip install bleak`; unverified)")
    ap.add_argument("--gateway", help="gateway base URL (admin API)")
    ap.add_argument("--token-file", help="file with the admin bearer token (or env PROV_ADMIN_TOKEN)")
    ap.add_argument("--pop-file", help="file with the board's PoP (the label); omit only for a board without PoP")
    ap.add_argument("--wifi-ssid", default="e2e-sim")
    ap.add_argument("--bundle-file", help="use this bundle JSON instead of calling the gateway (tests); contains secrets")
    ap.add_argument("--chunk", type=int, default=180)
    a = ap.parse_args(argv)
    if bool(a.peer) == bool(a.bleak):
        print("error: give exactly one of --peer / --bleak", file=sys.stderr)
        return 2
    pop = Path(a.pop_file).read_text(encoding="ascii").strip() if a.pop_file else ""
    try:
        if a.bleak:
            from .blepeer_bleak import BleakPeer
            peer = BleakPeer(a.bleak)
        else:
            host, _, port = a.peer.rpartition(":")
            peer = TcpPeer(host, int(port))
        client = BleClient(peer, pop)
        info = client.info()
        node_id = info["node_id"]
        if a.bundle_file:
            bundle = json.loads(Path(a.bundle_file).read_text(encoding="utf-8"))
        else:
            token = Path(a.token_file).read_text().strip() if a.token_file else os.environ.get("PROV_ADMIN_TOKEN", "")
            if not a.gateway or not token:
                print("error: --gateway and an admin token (--token-file / PROV_ADMIN_TOKEN) are required", file=sys.stderr)
                return 2
            sess = create_session(a.gateway, token, node_id, a.wifi_ssid, os.environ.get("PROV_WIFI_PASSWORD", ""))
            bundle = sess["bundle"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"error: {type(e).__name__}", file=sys.stderr)          # no body: it could echo request data
        return 2
    try:
        res = client.provision(bundle, node_id, a.chunk)
    except ProvisionError as e:
        print(json.dumps({"node_id": node_id, "ok": False, "code": e.code}))
        return 1
    finally:
        peer.disconnect()
    print(json.dumps({"node_id": node_id, "ok": True, "state": res["state"], "code": res["code"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
