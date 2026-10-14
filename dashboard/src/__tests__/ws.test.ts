import { backoffMs, connectWs } from "../api/ws";
import { session } from "../api/client";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes, tokens } from "../test/fixtures";
import { FakeWebSocket, until } from "../test/fakes";

describe("websocket ticket flow and reconnect", () => {
  it("backoff doubles; 4429 waits at least 5 s up to 60 s", () => {
    expect([0, 1, 2, 3].map((n) => backoffMs(n, false))).toEqual([1000, 2000, 4000, 8000]);
    expect(backoffMs(0, true)).toBe(5000);
    expect(backoffMs(1, true)).toBe(10000);
    expect(backoffMs(9, true)).toBe(60000);
  });

  it("uses a fresh ticket in the subprotocol (never the JWT in the URL) and reconnects with backoff", async () => {
    let n = 0;
    server.use(
      route("post", "/ws/ticket", () => ({ json: { ticket: `T${++n}`.padEnd(43, "x"), expires_in_s: 30 } })),
      ...baseRoutes(),
    );
    session.set(tokens());
    const sleeps: number[] = [];
    const statuses: string[] = [];
    const stop = connectWs({
      onMessage: () => {}, onStatus: (s) => statuses.push(s),
      sleep: async (ms) => { sleeps.push(ms); },
    });
    await until(() => FakeWebSocket.instances.length === 1);
    const first = FakeWebSocket.instances[0];
    expect(first.url).not.toContain("test-access");
    expect(first.url).not.toContain("token");
    expect(first.protocols).toEqual([`edge-ticket.${"T1".padEnd(43, "x")}`]);
    first.open();
    first.serverClose(1006); // network drop
    await until(() => FakeWebSocket.instances.length === 2);
    expect(FakeWebSocket.instances[1].protocols).toEqual([`edge-ticket.${"T2".padEnd(43, "x")}`]); // single-use: new ticket each time
    expect(sleeps[0]).toBe(1000);
    FakeWebSocket.instances[1].serverClose(1006); // dropped before open: attempt grows
    await until(() => FakeWebSocket.instances.length === 3);
    expect(sleeps[1]).toBe(2000);
    FakeWebSocket.instances[2].open();
    expect(statuses).toContain("open");
    stop();
  });

  it("close 4429 backs off at least 5 s", async () => {
    server.use(...baseRoutes());
    session.set(tokens());
    const sleeps: number[] = [];
    const stop = connectWs({ onMessage: () => {}, onStatus: () => {}, sleep: async (ms) => { sleeps.push(ms); } });
    await until(() => FakeWebSocket.instances.length === 1);
    FakeWebSocket.instances[0].serverClose(4429);
    await until(() => FakeWebSocket.instances.length === 2);
    expect(sleeps[0]).toBeGreaterThanOrEqual(5000);
    stop();
  });

  it("close 4401 refreshes the token and reconnects without delay", async () => {
    let refreshed = 0;
    server.use(route("post", "/auth/refresh", () => { refreshed++; return { json: tokens() }; }), ...baseRoutes());
    session.set(tokens());
    const sleeps: number[] = [];
    const stop = connectWs({ onMessage: () => {}, onStatus: () => {}, sleep: async (ms) => { sleeps.push(ms); } });
    await until(() => FakeWebSocket.instances.length === 1);
    FakeWebSocket.instances[0].serverClose(4401);
    await until(() => FakeWebSocket.instances.length === 2);
    expect(refreshed).toBe(1);
    expect(sleeps[0]).toBe(0);
    stop();
  });
});
