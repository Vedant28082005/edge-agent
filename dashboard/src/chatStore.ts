// Chat history: a module-level store (survives navigating between tabs) mirrored to sessionStorage
// (survives a reload, cleared on log out). Never holds access/refresh tokens or confirmation_token:
// tool args/results are stripped of those keys before they ever reach memory or storage.
import { session } from "./api/client";

export type Tool = { call_id: string; tool: string; args: unknown; result?: unknown; ok?: boolean };
export type Msg =
  | { kind: "user"; text: string; ts?: number }
  | { kind: "assistant"; text: string; final: boolean; route?: string; latency_ms?: number; status?: string; tools: Tool[]; error?: string; stopped?: boolean; ts?: number };

const STORAGE_KEY = "edge.chat";
const SECRET_KEYS = new Set(["access_token", "refresh_token", "confirmation_token"]);

function stripSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripSecrets);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (SECRET_KEYS.has(k)) continue;
      out[k] = stripSecrets(v);
    }
    return out;
  }
  return value;
}

function sanitize(msgs: Msg[]): Msg[] {
  return msgs.map((m) =>
    m.kind === "assistant"
      ? { ...m, tools: m.tools.map((t) => ({ ...t, args: stripSecrets(t.args), result: t.result === undefined ? undefined : stripSecrets(t.result) })) }
      : m,
  );
}

function randomSessionId(): string {
  return `web-${Math.random().toString(36).slice(2, 10)}`;
}

type Stored = { sessionId: string; msgs: Msg[] };

function load(): Stored {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Stored;
      if (parsed && typeof parsed.sessionId === "string" && Array.isArray(parsed.msgs)) return parsed;
    }
  } catch { /* storage unavailable or corrupt */ }
  return { sessionId: randomSessionId(), msgs: [] };
}

let state: Stored = load();
const listeners = new Set<() => void>();

function persist() {
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch { /* storage unavailable */ }
}

function notify() {
  listeners.forEach((fn) => fn());
}

export const chatStore = {
  get sessionId() { return state.sessionId; },
  getMsgs(): Msg[] { return state.msgs; },
  subscribe(fn: () => void): () => void {
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  },
  setMsgs(next: Msg[] | ((prev: Msg[]) => Msg[])) {
    const raw = typeof next === "function" ? (next as (p: Msg[]) => Msg[])(state.msgs) : next;
    state = { ...state, msgs: sanitize(raw) };
    persist();
    notify();
  },
  clear() {
    state = { sessionId: randomSessionId(), msgs: [] };
    try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* storage unavailable */ }
    notify();
  },
};

session.subscribe(() => {
  if (!session.authed) chatStore.clear();
});
