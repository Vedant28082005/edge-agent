# dashboard (S5)

React + Vite + TypeScript web UI. Talks only to the gateway (`contracts/openapi/gateway.yaml`), same-origin.

```
npm ci
npm run gen:api     # regenerate src/api/schema.gen.ts from the contract (never edit it)
npm run check:api   # fails if schema.gen.ts is stale (also runs first in `npm test`)
npm test            # vitest + testing-library + MSW (handlers are checked against the contract)
npm run dev         # Vite dev server, proxies gateway paths and /ws to http://127.0.0.1:8000
npm run build       # -> dist/
```

Serving: `deploy/config/gateway.json` sets `static_dir: /srv/dashboard`; `deploy/compose.yml` mounts
`${DASHBOARD_DIR:-../dashboard/dist}` there read-only. Build first, then `docker compose up`.

Notes: TypeScript is pinned to 5.x (openapi-typescript 7 does not run on TS 7). Auth is the contract's JWT pair
(access in memory, refresh in sessionStorage). The WebSocket uses `POST /ws/ticket` and the
`edge-ticket.<ticket>` subprotocol, never a token in the URL. The first admin is created with the gateway CLI
(the contract has no HTTP bootstrap).
