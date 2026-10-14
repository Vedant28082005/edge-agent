import { api, refreshSession, session } from "./client";
import type { WsMessage } from "./events";

export type WsStatus = "connecting" | "open" | "reconnecting" | "closed";

export function backoffMs(attempt: number, limited: boolean): number {
  if (limited) return Math.min(60_000, 5_000 * 2 ** attempt); // close 4429: at least 5 s, doubling to 60 s
  return Math.min(30_000, 1_000 * 2 ** attempt);
}

/** Seam for tests: the default factory is the browser WebSocket. */
export const wsFactory = { make: (url: string, protocols: string[]): WebSocket => new WebSocket(url, protocols) };

type Opts = {
  onMessage: (m: WsMessage) => void;
  onStatus: (s: WsStatus) => void;
  makeSocket?: (url: string, protocols: string[]) => WebSocket;
  sleep?: (ms: number) => Promise<void>;
};

/** Ticket flow: POST /ws/ticket, then connect with `Sec-WebSocket-Protocol: edge-ticket.<ticket>`. The JWT never goes in the URL. */
export function connectWs(o: Opts): () => void {
  const make = o.makeSocket ?? ((u, p) => wsFactory.make(u, p));
  const sleep = o.sleep ?? ((ms) => new Promise<void>((r) => setTimeout(r, ms)));
  let stopped = false;
  let sock: WebSocket | null = null;
  let attempt = 0;

  const url = () => `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;

  async function loop() {
    while (!stopped) {
      o.onStatus(attempt === 0 ? "connecting" : "reconnecting");
      let limited = false;
      let authLost = false;
      try {
        const t = await api<{ ticket: string }>("POST", "/ws/ticket");
        if (stopped) return;
        const code = await new Promise<number>((resolve) => {
          const s = make(url(), [`edge-ticket.${t.ticket}`]);
          sock = s;
          s.onopen = () => { attempt = 0; o.onStatus("open"); };
          s.onmessage = (e) => { try { o.onMessage(JSON.parse(String(e.data)) as WsMessage); } catch { /* ignore junk */ } };
          s.onerror = () => { /* close follows */ };
          s.onclose = (e) => resolve(e.code);
        });
        limited = code === 4429;
        authLost = code === 4401;
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 401) authLost = true;
        if (status === 429) limited = true;
      }
      if (stopped) return;
      if (authLost && !(await refreshSession())) { o.onStatus("closed"); return; } // session gone: UI shows login
      if (!session.authed) { o.onStatus("closed"); return; }
      attempt += 1;
      o.onStatus("reconnecting");
      await sleep(authLost ? 0 : backoffMs(attempt - 1, limited));
    }
  }
  void loop();
  return () => { stopped = true; sock?.close(); o.onStatus("closed"); };
}
