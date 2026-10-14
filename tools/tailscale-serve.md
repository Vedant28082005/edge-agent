# Tailscale in front of the gateway (HTTPS) and ACL notes

The gateway publishes plain HTTP on `127.0.0.1:8000` only (compose). `tailscale serve` terminates TLS with the tailnet
certificate and forwards to it, so the gateway is reachable at `https://pi-brain.<tailnet>.ts.net` from tailnet devices
only, and never from the LAN or the internet. Run once on `pi-brain` (needs `tailscale up` done, MagicDNS and HTTPS
certificates enabled in the admin console under DNS):

```bash
# HTTPS on 443 -> gateway on loopback (background, survives reboots)
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000

tailscale serve status                         # shows https://pi-brain.<tailnet>.ts.net -> http://127.0.0.1:8000
curl -s https://pi-brain.<tailnet>.ts.net/system/health -o /dev/null -w '%{http_code}\n'   # 401 = up (auth required)

# stop serving
sudo tailscale serve --https=443 off           # or: sudo tailscale serve reset
```

Do **not** use `tailscale funnel` (it publishes to the internet). Do not serve 8100, 8200 or 8001: they are not published
by compose and must stay unreachable.

WebSockets (`/ws`) and SSE (`/chat`) pass through `tailscale serve`. `/ws` authenticates with a one-time ticket in the
`Sec-WebSocket-Protocol` header, never in the URL, so the access log of the proxy carries no credential.

## ACL notes (tailnet policy file, admin console -> Access controls)

* Tag the Pi `tag:edge-brain` and allow only your user devices to reach it on 443: `{"action": "accept", "src": ["autogroup:member"], "dst": ["tag:edge-brain:443"]}`
  (narrow `src` to the groups that may use the home UI, e.g. `group:household`).
* Admin work (`/admin/*`, provisioning) is role-gated by the gateway, not by Tailscale; the ACL only limits who can open the app at all.
* Give `tag:edge-brain` **no** `dst` rules for 8883, 8100, 8200 or 8000: boards reach mosquitto on 8883 over the **LAN** (their
  provisioning bundle holds the Pi's LAN IP), not over the tailnet.
* ssh to the Pi only from the admin PC: `{"action": "accept", "src": ["<your user>"], "dst": ["tag:edge-brain:22"]}`; or use Tailscale SSH with `check` mode.
* pi-vision and jetson-yolo stay reachable from the brain for the gateway's vision/yolo upstreams (`pi-vision:8300`, `jetson-yolo:8400`):
  `{"action": "accept", "src": ["tag:edge-brain"], "dst": ["tag:edge-vision:8300", "tag:edge-yolo:8400"]}`.
* The gateway sees every tailnet client as the container bridge address (the connection comes through the proxy), so its
  per-IP login limit (20 failures per 15 min) is shared by all users; the per-user+IP limit (5) is not. Fine for a household.

Status: written from the Tailscale CLI documentation; not run on a real tailnet.
