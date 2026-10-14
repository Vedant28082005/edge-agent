"""Run the REAL device-hub (worktree edge-agent-s1, its own venv) in --mock style, wired to a REAL gateway:

* in-memory broker + scripted fake nodes (no Docker, no network except 127.0.0.1),
* caller authentication ON (verifier dir), so the hub only accepts the gateway's secret,
* the hub's gateway client is the production GatewayClient pointed at the gateway's internal listener.

Secrets are passed as FILE PATHS (never argv/env). Usage (cwd = the S1 device-hub folder):
    python hub_harness.py --port N --gateway-url URL --device-hub-secret-file F --verifier-dir D
"""
from __future__ import annotations

import argparse
import copy
import logging
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--gateway-url", required=True)
ap.add_argument("--device-hub-secret-file", required=True)
ap.add_argument("--verifier-dir", required=True)
ap.add_argument("--hub-dir", required=True, help="the S1 services/device-hub folder")
a = ap.parse_args()
sys.path.insert(0, a.hub_dir)

import uvicorn  # noqa: E402

from device_hub.api import create_app  # noqa: E402
from device_hub.gateway_client import GatewayClient  # noqa: E402
from device_hub.mock import FakeNode, build_mock_hub, demo_announces  # noqa: E402
from device_hub.util import install_log_redaction  # noqa: E402

install_log_redaction()
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.DEBUG)

hub, broker, fake_gw, nodes = build_mock_hub(":memory:", a.port, seed=True)
gc = GatewayClient.from_secret_file(a.gateway_url, a.device_hub_secret_file)
hub.gateway = gc
hub.executor.gateway = gc                       # production consume path -> the real gateway internal listener
hub.cfg.auth.verifier_dir = a.verifier_dir

# one extra, announced-but-NOT-approved node for the approve test
ann = copy.deepcopy(demo_announces()[0])
ann["node_id"] = "esp32-aabbccddee09"
ann["endpoints"] = [e for e in ann["endpoints"] if e["type"] == "light"]
ann["endpoints"][0]["local_id"] = "l9"
sess = hub.admin.create_provision_session({"node_id": ann["node_id"], "wifi": {"ssid": "mock", "password": "mock-wifi-pass"}}, "harness")
m = sess["bundle"]["mqtt"]
extra = FakeNode(ann, broker.client(m["username"], m["password"], ann["node_id"], FakeNode.will(ann["node_id"])))
extra.start()
broker.flush()

app = create_app(hub, mock_gateway=fake_gw, insecure_no_auth=False)
print("READY", flush=True)
uvicorn.run(app, host="127.0.0.1", port=a.port, log_config=None, access_log=False)
