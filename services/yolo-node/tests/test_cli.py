"""Real process smoke test: python -m yolo_node --mock over a real socket. Stops only the PID it started."""
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_mock_server_end_to_end(tmp_path):
    from conftest import SECRET, H, image_bytes, multipart, conforms
    from yolo_node.auth import ApiAuth
    ApiAuth.from_secret(SECRET, str(tmp_path / "v.sha256"))
    port = _free_port()
    p = subprocess.Popen([sys.executable, "-m", "yolo_node", "--mock", "--verifier-file", str(tmp_path / "v.sha256"),
                          "--port", str(port)], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:%d" % port
        for _ in range(60):
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise AssertionError("server did not start")
        j = httpx.get(base + "/health").json()
        assert j["mock"] is True and j["ok"] is True
        assert httpx.get(base + "/v1/sources").status_code == 401
        body, h = multipart({}, image_bytes())
        r = httpx.post(base + "/v1/detect", content=body, headers={**H, **h})
        assert r.status_code == 200 and r.json()["detections"][0]["label"] == "person"
        assert httpx.post(base + "/v1/detect/snapshot", json={"source_id": "cam0"}, headers=H).status_code == 200
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
