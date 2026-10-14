import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { api } from "./api/client";
import type { ConfirmationRequest, WsMessage } from "./api/events";
import type { Endpoint } from "./api/types";
import { connectWs, type WsStatus } from "./api/ws";

export type SettlePhase = "accepted" | "settling" | "settled" | "settle_timeout";
export type Settle = { phases: SettlePhase[]; observed?: unknown; elapsed_ms?: number; settle_s?: number };

export type ConfStatus = "pending" | "approving" | "approved" | "declined" | "expired" | "error";
export type Conf = { req: ConfirmationRequest; status: ConfStatus; error?: string; retriedAs?: string };

export type FeedItem = { id: number; at: number; msg: WsMessage };

type S = {
  endpoints: Endpoint[] | null;
  loadError: string | null;
  ws: WsStatus;
  settle: Record<string, Settle>;
  confs: Record<string, Conf>;
  feed: FeedItem[];
};

type A =
  | { t: "endpoints"; v: Endpoint[] }
  | { t: "loadError"; v: string }
  | { t: "ws"; v: WsStatus }
  | { t: "msg"; m: WsMessage }
  | { t: "settle"; key: string; phase: SettlePhase }
  | { t: "conf"; id: string; patch: Partial<Conf> }
  | { t: "confAdd"; req: ConfirmationRequest };

export const settleKey = (ep: string, cap: string) => `${ep}:${cap}`;
let feedSeq = 0;

function reduce(s: S, a: A): S {
  switch (a.t) {
    case "endpoints": return { ...s, endpoints: a.v, loadError: null };
    case "loadError": return { ...s, loadError: a.v };
    case "ws": return { ...s, ws: a.v };
    case "settle": {
      const cur = s.settle[a.key]?.phases ?? [];
      const phases = a.phase === "accepted" ? ["accepted" as const] : [...cur, a.phase];
      return { ...s, settle: { ...s.settle, [a.key]: { ...s.settle[a.key], phases } } };
    }
    case "confAdd":
      return { ...s, confs: { ...s.confs, [a.req.request_id]: { req: a.req, status: "pending" } } };
    case "conf": {
      const c = s.confs[a.id];
      return c ? { ...s, confs: { ...s.confs, [a.id]: { ...c, ...a.patch } } } : s;
    }
    case "msg": {
      const m = a.m;
      let next: S = { ...s, feed: [{ id: ++feedSeq, at: Date.now(), msg: m }, ...s.feed].slice(0, 200) };
      if (m.type === "state" && next.endpoints) {
        next = {
          ...next,
          endpoints: next.endpoints.map((e) => (e.endpoint_id === m.endpoint_id ? { ...e, state: { ...e.state, ...m.values } } : e)),
        };
      } else if (m.type === "action_settled" || m.type === "action_settle_timeout") {
        const key = settleKey(m.endpoint_id, m.capability);
        const done: SettlePhase = m.type === "action_settled" ? "settled" : "settle_timeout";
        const prev = next.settle[key]?.phases ?? ["accepted", "settling"];
        next = {
          ...next,
          settle: {
            ...next.settle,
            [key]: {
              phases: [...prev, done], observed: m.observed,
              elapsed_ms: m.type === "action_settled" ? m.elapsed_ms : undefined,
              settle_s: m.type === "action_settle_timeout" ? m.settle_s : undefined,
            },
          },
        };
      } else if (m.type === "confirmation_request") {
        next = { ...next, confs: { ...next.confs, [m.request_id]: { req: m, status: "pending" } } };
      } else if (m.type === "confirmation_declined" || m.type === "confirmation_expired") {
        const c = next.confs[m.request_id];
        const status: ConfStatus = m.type === "confirmation_declined" ? "declined" : "expired";
        next = {
          ...next,
          confs: { ...next.confs, [m.request_id]: c ? { ...c, status } : { req: { request_id: m.request_id, summary: m.summary, action: { type: "unknown" }, expires_in_s: 0 }, status } },
        };
      }
      return next;
    }
  }
}

type Ctx = S & {
  dispatch: (m: WsMessage) => void;
  reload: () => Promise<void>;
  noteSettle: (ep: string, cap: string, phase: SettlePhase) => void;
  patchConf: (id: string, patch: Partial<Conf>) => void;
  addConf: (req: ConfirmationRequest) => void;
};

const LiveCtx = createContext<Ctx | null>(null);
export const useLive = () => {
  const c = useContext(LiveCtx);
  if (!c) throw new Error("LiveProvider missing");
  return c;
};

export function LiveProvider({ children, onClosed }: { children: ReactNode; onClosed?: () => void }) {
  const [s, d] = useReducer(reduce, { endpoints: null, loadError: null, ws: "connecting", settle: {}, confs: {}, feed: [] });
  const onClosedRef = useRef(onClosed);
  onClosedRef.current = onClosed;

  const reload = useCallback(async () => {
    try { d({ t: "endpoints", v: await api<Endpoint[]>("GET", "/devices") }); }
    catch (e) { d({ t: "loadError", v: (e as Error).message }); }
  }, []);

  useEffect(() => {
    void reload();
    let wasOpen = false;
    const stop = connectWs({
      onStatus: (v) => {
        d({ t: "ws", v });
        if (v === "open") { if (wasOpen) void reload(); wasOpen = true; } // resync after a reconnect: state may have changed meanwhile
        if (v === "closed") onClosedRef.current?.();
      },
      onMessage: (m) => {
        d({ t: "msg", m });
        if (m.type === "registry_changed") void reload();
      },
    });
    return stop;
  }, [reload]);

  const value = useMemo<Ctx>(() => ({
    ...s,
    dispatch: (m) => d({ t: "msg", m }),
    reload,
    noteSettle: (ep, cap, phase) => d({ t: "settle", key: settleKey(ep, cap), phase }),
    patchConf: (id, patch) => d({ t: "conf", id, patch }),
    addConf: (req) => d({ t: "confAdd", req }),
  }), [s, reload]);

  return <LiveCtx.Provider value={value}>{children}</LiveCtx.Provider>;
}
