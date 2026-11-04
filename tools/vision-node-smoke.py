"""Smoke test of a running vision-node (contract vision-node-internal.yaml) with a REAL image. Standard library only.

    python vision-node-smoke.py --url http://100.122.169.70:8300 --secret-file SECRET --image photo.jpg [--question "..."]

The secret is read from a file and sent only in X-Service-Secret; it is never printed or logged. Answers are untrusted model
text (already restricted to a safe alphabet by the node) and are shown as data. Timings:
  wall   = what this client measured (network + queue + model)
  node   = latency_ms the node reports (queue wait included)
Expected on the vlmpi, from the benchmark (224 px, -c 1024, -np 1, -t 4) [measured by the owner]: yes/no about 2.3 s,
trimmed JSON about 7 s. Short free text has no measurement yet [est ~5.5 s]. The first call after a restart is slower
(model pages in). The node answers repeats of the same image+question from a 30 s cache (cached=true, ~0 ms): the script
varies the question text to measure real work.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

EXPECTED_MS = {"yesno": 2300, "json": 7000, "short": 5500}      # short is an estimate


def multipart(params: dict, image: bytes) -> tuple[bytes, str]:
    b = "----smoke" + uuid.uuid4().hex
    out = bytearray()
    out += (f"--{b}\r\nContent-Disposition: form-data; name=\"params\"\r\nContent-Type: application/json\r\n\r\n").encode()
    out += json.dumps(params).encode() + b"\r\n"
    ctype = "image/png" if image[:4] == b"\x89PNG" else "image/webp" if image[8:12] == b"WEBP" else "image/jpeg"
    out += (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"frame\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    out += image + f"\r\n--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


def call(url: str, secret: str, path: str, params: dict, image: bytes, timeout: float) -> tuple[int, dict, int]:
    body, ctype = multipart(params, image)
    req = urllib.request.Request(url + path, data=body, method="POST",
                                 headers={"Content-Type": ctype, "X-Service-Secret": secret})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": type(e).__name__}, int((time.monotonic() - t0) * 1000)
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"error": "not_json"}
    return code, data if isinstance(data, dict) else {"error": "not_object"}, int((time.monotonic() - t0) * 1000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://100.122.169.70:8300")
    ap.add_argument("--secret-file", required=True)
    ap.add_argument("--image", required=True, action="append", help="jpg/png/webp (max 5 MiB); repeatable")
    ap.add_argument("--question", default="Is there a person in the image?")
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args()
    with open(a.secret_file, encoding="utf-8") as f:
        secret = f.read().strip()
    if not secret:
        print("secret file is empty")
        return 2
    url = a.url.rstrip("/")

    try:
        with urllib.request.urlopen(url + "/health", timeout=10) as r:
            h = json.loads(r.read())
        models = ", ".join(f"{m.get('name')}={'loaded' if m.get('loaded') else 'NOT loaded'}" for m in h.get("models", []))
        print(f"health: ok={h.get('ok')} mem_available_mb={h.get('mem_available_mb')} temp_c={h.get('temp_c')} models: {models}")
    except Exception as e:
        print("health: FAILED", type(e).__name__)
        return 1

    run = uuid.uuid4().hex[:6]                        # defeats the 30 s answer cache so timings are real work
    bad = 0
    for img_path in a.image:
        with open(img_path, "rb") as f:
            img = f.read()
        print(f"\nimage {img_path} ({len(img) // 1024} KiB)")
        schema = {"type": "object", "properties": {"person": {"type": "boolean"}, "note": {"type": "string", "maxLength": 40}},
                  "required": ["person"]}
        steps = [
            ("yesno", "/v1/vlm/yesno", {"question": f"{a.question} ({run})"}),
            ("short", "/v1/vlm/short", {"question": f"Describe the scene in one short sentence. ({run})", "max_tokens": 40}),
            ("json", "/v1/vlm/json", {"question": f"Is a person visible? ({run})", "schema": schema, "max_tokens": 64,
                                        "timeout_ms": 25000}),
        ]
        for name, path, params in steps:
            code, data, wall = call(url, secret, path, params, img, a.timeout)
            if code == 200:
                shown = data.get("answer") if name == "yesno" else data.get("text") if name == "short" else data.get("data", data.get("result"))
                note = "" if wall <= 3 * EXPECTED_MS[name] else "  SLOW (>3x expected)"
                print(f"  {name:5} 200  wall {wall:6d} ms  node {data.get('latency_ms')} ms (queue {data.get('queue_ms')})"
                      f"  expected ~{EXPECTED_MS[name]} ms{'[est]' if name == 'short' else ''}  cached={data.get('cached')}{note}")
                print(f"        -> {shown!r}")
            else:
                bad += 1
                reason = data.get("error", "?")
                print(f"  {name:5} {code or 'ERR'}  wall {wall} ms  error={reason}")
    print("\nSMOKE", "PASSED" if bad == 0 else f"FAILED ({bad} call(s))")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
