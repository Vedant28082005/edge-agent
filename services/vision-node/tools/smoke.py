"""Real-socket smoke test: starts `python -m vision_node --mock` (authentication ON, temp verifier) on a free loopback
port, calls it over TCP and stops ONLY the child process it started. No Docker, no models.
Usage: python services/vision-node/tools/smoke.py"""
import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = uuid.uuid4().hex + uuid.uuid4().hex     # random per run; only its hash touches disk (temp dir, deleted)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def png() -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (32, 24), (5, 100, 200)).save(b, "PNG")
    return b.getvalue()


def multipart(params: dict, part: str, data: bytes) -> tuple[bytes, str]:
    bd = "----smoke" + uuid.uuid4().hex
    head = (f'--{bd}\r\nContent-Disposition: form-data; name="params"\r\nContent-Type: application/json\r\n\r\n'
            f'{json.dumps(params)}\r\n--{bd}\r\nContent-Disposition: form-data; name="{part}"; filename="x"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n')
    return head.encode() + data + f"\r\n--{bd}--\r\n".encode(), f"multipart/form-data; boundary={bd}"


def main() -> int:
    port = free_port()
    with tempfile.TemporaryDirectory() as td:
        ver = Path(td) / "agent.sha256"
        ver.write_text(hashlib.sha256(SECRET.encode()).hexdigest() + "\n")
        cfg = Path(td) / "vision-node.json"
        cfg.write_text(json.dumps({"auth": {"verifier_file": str(ver)}}))
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        proc = subprocess.Popen([sys.executable, "-m", "vision_node", "--config", str(cfg), "--mock", "--port", str(port)],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            def call(method, path, body=None, headers=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request(method, path, body, headers or {})
                r = c.getresponse()
                return r.status, json.loads(r.read() or b"null")

            for _ in range(100):
                try:
                    st, health = call("GET", "/health")
                    break
                except OSError:
                    if proc.poll() is not None:
                        print("FAIL server exited:", proc.stderr.read().decode()[-500:])
                        return 1
                    time.sleep(0.1)
            else:
                print("FAIL server did not start")
                return 1
            ok = st == 200 and health["ok"] and health["mock"] and health["service"] == "vision-node"
            body, ct = multipart({"question": "Is the light on?"}, "image", png())
            st401, _ = call("POST", "/v1/vlm/yesno", body, {"Content-Type": ct})
            st200, res = call("POST", "/v1/vlm/yesno", body, {"Content-Type": ct, "X-Service-Secret": SECRET})
            ok = ok and st401 == 401 and st200 == 200 and res["answer"] in ("yes", "no", "unsure") and res["cached"] is False
            st2, res2 = call("POST", "/v1/vlm/yesno", body, {"Content-Type": ct, "X-Service-Secret": SECRET})
            ok = ok and st2 == 200 and res2["cached"] is True
            print("ok   real-socket smoke" if ok else f"FAIL real-socket smoke {st} {st401} {st200} {res}")
            return 0 if ok else 1
        finally:
            proc.terminate()                 # the child this script started, nothing else
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
