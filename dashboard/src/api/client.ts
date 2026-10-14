import type { Role, TokenPair } from "./types";

// Contract scheme: JWT access + refresh in JSON bodies, `Authorization: Bearer` on requests,
// `POST /auth/refresh {refresh_token}` (rotating). The access token lives in memory only;
// the refresh token is kept in sessionStorage so a reload keeps the session for the tab.
const RT_KEY = "edge.refresh";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message?: string, public retryAfter?: number, public body?: unknown) {
    super(message || code);
  }
}

let access: string | null = null;
let role: Role | null = null;
let refreshing: Promise<boolean> | null = null;
const listeners = new Set<() => void>();

const store = {
  get: () => { try { return sessionStorage.getItem(RT_KEY); } catch { return null; } },
  set: (v: string | null) => {
    try { if (v) sessionStorage.setItem(RT_KEY, v); else sessionStorage.removeItem(RT_KEY); } catch { /* storage unavailable */ }
  },
};

export const session = {
  get role() { return role; },
  get authed() { return access !== null; },
  hasRefresh: () => store.get() !== null,
  subscribe(fn: () => void) { listeners.add(fn); return () => { listeners.delete(fn); }; },
  set(p: TokenPair) { access = p.access_token; role = p.role; store.set(p.refresh_token); listeners.forEach((f) => f()); },
  clear() { access = null; role = null; store.set(null); listeners.forEach((f) => f()); },
};

async function parseError(res: Response): Promise<ApiError> {
  let body: { error?: string; message?: string } = {};
  try { body = await res.json(); } catch { /* not JSON */ }
  const ra = Number(res.headers.get("Retry-After"));
  return new ApiError(res.status, body.error ?? `http_${res.status}`, body.message, ra > 0 ? ra : undefined, body);
}

export function refreshSession(): Promise<boolean> {
  const rt = store.get();
  if (!rt) return Promise.resolve(false);
  refreshing ??= (async () => {
    try {
      const res = await fetch("/auth/refresh", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: rt }),
      });
      if (!res.ok) { session.clear(); return false; }
      session.set((await res.json()) as TokenPair);
      return true;
    } catch { return false; } finally { refreshing = null; }
  })();
  return refreshing;
}

export async function rawFetch(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
  const headers = new Headers(init.headers);
  if (access) headers.set("Authorization", `Bearer ${access}`);
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401 && retry && !path.startsWith("/auth/") && (await refreshSession())) {
    return rawFetch(path, init, false);
  }
  return res;
}

export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) { init.body = JSON.stringify(body); init.headers = { "Content-Type": "application/json" }; }
  const res = await rawFetch(path, init);
  if (!res.ok) throw await parseError(res);
  if (res.status === 204 || !res.headers.get("content-type")?.includes("json")) return undefined as T;
  return (await res.json()) as T;
}

export async function login(username: string, password: string): Promise<void> {
  const res = await fetch("/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw await parseError(res);
  session.set((await res.json()) as TokenPair);
}

export function logout() { session.clear(); }
