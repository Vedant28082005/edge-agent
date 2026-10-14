"""Container HEALTHCHECK helper (stdlib only). Usage: healthcheck.py PORT PATH [200|alive]

200    -> the path must answer HTTP 200 (device-hub /health is open).
alive  -> any HTTP answer below 500 counts, including 401: the gateway and the agent authenticate every route, and
          a container holds no secret to present, so a 401 from the app's own auth layer proves it is serving."""
import http.client
import sys

port, path = int(sys.argv[1]), sys.argv[2]
mode = sys.argv[3] if len(sys.argv) > 3 else "200"
try:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    c.request("GET", path)
    status = c.getresponse().status
except Exception as e:  # noqa: BLE001
    print(f"unhealthy: {type(e).__name__}")
    sys.exit(1)
ok = status == 200 if mode == "200" else status < 500
print(f"HTTP {status}")
sys.exit(0 if ok else 1)
