import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { api, session } from "./api/client";
import type { EventAction, WsMessage } from "./api/events";
import type { Endpoint, Notification, NotificationPage } from "./api/types";
import { connectWs, type WsStatus } from "./api/ws";

export type SettlePhase = "accepted" | "settling" | "settled" | "settle_timeout";
// `deadline` (ms epoch) is when the capability's announced settle_s runs out; absent for capabilities without one.
export type Settle = { phases: SettlePhase[]; observed?: unknown; elapsed_ms?: number; settle_s?: number; deadline?: number };

export type ConfStatus = "pending" | "approving" | "approved" | "declined" | "expired" | "error";
// Only what the card shows: the server summary and action. The action_hash is never kept or displayed.
export type ConfReq = { request_id: string; summary: string; action?: EventAction };
export type Conf = { req: ConfReq; status: ConfStatus; error?: string; retriedAs?: string };

export type FeedItem = { id: number; at: number; msg: WsMessage };

type S = {
  endpoints: Endpoint[] | null;
  loadError: string | null;
  ws: WsStatus;
  settle: Record<string, Settle>;
  confs: Record<string, Conf>;
  feed: FeedItem[];
  notifs: Notification[]; // newest first; fed by GET /notifications and live `automation_notification`
  autoRev: number; // bumped on every `automations_changed`: the Automations view refetches
  autoEnabled: boolean | null; // kill switch state once known (admins read it; others learn it from `automations_changed`)
};

type A =
  | { t: "endpoints"; v: Endpoint[] }
  | { t: "loadError"; v: string }
  | { t: "ws"; v: WsStatus }
  | { t: "msg"; m: WsMessage }
  | { t: "settle"; key: string; phase: SettlePhase; settle_s?: number; now: number }
  | { t: "conf"; id: string; patch: Partial<Conf> }
  | { t: "confAdd"; req: ConfReq }
  | { t: "notifs"; v: Notification[] }
  | { t: "notifsRead"; ids: string[] }
  | { t: "autoEnabled"; v: boolean };

const MAX_NOTIFS = 100;
function mergeNotifs(cur: Notification[], incoming: Notification[]): Notification[] {
  const byId = new Map(cur.map((n) => [n.notification_id, n]));
  for (const n of incoming) byId.set(n.notification_id, { ...byId.get(n.notification_id), ...n });
  return [...byId.values()].sort((a, b) => b.ts - a.ts).slice(0, MAX_NOTIFS);
}

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
      const deadline = a.phase === "settling" && a.settle_s ? a.now + a.settle_s * 1000 : a.phase === "accepted" ? undefined : s.settle[a.key]?.deadline;
      return { ...s, settle: { ...s.settle, [a.key]: { ...s.settle[a.key], phases, settle_s: a.settle_s ?? s.settle[a.key]?.settle_s, deadline } } };
    }
    case "confAdd":
      return { ...s, confs: { ...s.confs, [a.req.request_id]: { req: a.req, status: "pending" } } };
    case "conf": {
      const c = s.confs[a.id];
      return c ? { ...s, confs: { ...s.confs, [a.id]: { ...c, ...a.patch } } } : s;
    }
    case "notifs": return { ...s, notifs: mergeNotifs(s.notifs, a.v) };
    case "notifsRead": return { ...s, notifs: s.notifs.map((n) => (a.ids.includes(n.notification_id) ? { ...n, read: true } : n)) };
    case "autoEnabled": return { ...s, autoEnabled: a.v };
    case "msg": {
      const m = a.m;
      let next: S = { ...s, feed: [{ id: ++feedSeq, at: Date.now(), msg: m }, ...s.feed].slice(0, 200) };
      if (m.type === "automation_notification") {
        // Only the notification fields are kept; live ones are unread until the user marks them.
        const { notification_id, ts, level, source, rule_id, endpoint_id, text } = m;
        next = { ...next, notifs: mergeNotifs(next.notifs, [{ notification_id, ts, level, source, rule_id, endpoint_id, text, read: false }]) };
      } else if (m.type === "automations_changed") {
        next = { ...next, autoRev: next.autoRev + 1, autoEnabled: m.change === "kill_switch" && m.automations_enabled !== undefined ? m.automations_enabled : next.autoEnabled };
      }
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
        next = { ...next, confs: { ...next.confs, [m.request_id]: { req: { request_id: m.request_id, summary: m.summary, action: m.action }, status: "pending" } } };
      } else if (m.type === "confirmation_declined" || m.type === "confirmation_expired") {
        const c = next.confs[m.request_id];
        const status: ConfStatus = m.type === "confirmation_declined" ? "declined" : "expired";
        next = {
          ...next,
          confs: { ...next.confs, [m.request_id]: c ? { ...c, status } : { req: { request_id: m.request_id, summary: m.summary }, status } },
        };
      }
      return next;
    }
  }
}

type Ctx = S & {
  dispatch: (m: WsMessage) => void;
  reload: () => Promise<void>;
  loadNotifs: () => Promise<void>;
  markRead: (ids: string[]) => Promise<void>;
  setAutoEnabled: (v: boolean) => void;
  noteSettle: (ep: string, cap: string, phase: SettlePhase, settle_s?: number) => void;
  patchConf: (id: string, patch: Partial<Conf>) => void;
  addConf: (req: ConfReq) => void;
};

const LiveCtx = createContext<Ctx | null>(null);
export const useLive = () => {
  const c = useContext(LiveCtx);
  if (!c) throw new Error("LiveProvider missing");
  return c;
};

export function LiveProvider({ children, onClosed }: { children: ReactNode; onClosed?: () => void }) {
  const [s, d] = useReducer(reduce, { endpoints: null, loadError: null, ws: "connecting", settle: {}, confs: {}, feed: [], notifs: [], autoRev: 0, autoEnabled: null });
  const onClosedRef = useRef(onClosed);
  onClosedRef.current = onClosed;

  const reload = useCallback(async () => {
    try { d({ t: "endpoints", v: await api<Endpoint[]>("GET", "/devices") }); }
    catch (e) { d({ t: "loadError", v: (e as Error).message }); }
  }, []);

  // Guests have no automation routes (403) and never receive these messages, so they do not ask.
  const loadNotifs = useCallback(async () => {
    if (session.role === "guest") return;
    try { d({ t: "notifs", v: (await api<NotificationPage>("GET", "/notifications?limit=50")).items.map((n) => ({ ...n, read: n.read ?? false })) }); }
    catch { /* the bell just stays empty; live messages still arrive */ }
  }, []);
  const markRead = useCallback(async (ids: string[]) => {
    if (ids.length === 0) return;
    try { await api("POST", "/notifications/read", { notification_ids: ids.slice(0, 200) }); d({ t: "notifsRead", ids }); }
    catch { /* stays unread; the user can try again */ }
  }, []);

  const setAutoEnabled = useCallback((v: boolean) => d({ t: "autoEnabled", v }), []); // stable: views use it as an effect dependency

  useEffect(() => {
    void reload();
    void loadNotifs();
    let wasOpen = false;
    const stop = connectWs({
      onStatus: (v) => {
        d({ t: "ws", v });
        if (v === "open") { if (wasOpen) { void reload(); void loadNotifs(); } wasOpen = true; } // resync after a reconnect: state may have changed meanwhile
        if (v === "closed") onClosedRef.current?.();
      },
      onMessage: (m) => {
        d({ t: "msg", m });
        if (m.type === "registry_changed") void reload();
      },
    });
    return stop;
  }, [reload, loadNotifs]);

  const value = useMemo<Ctx>(() => ({
    ...s,
    dispatch: (m) => d({ t: "msg", m }),
    reload, loadNotifs, markRead,
    setAutoEnabled,
    noteSettle: (ep, cap, phase, settle_s) => d({ t: "settle", key: settleKey(ep, cap), phase, settle_s, now: Date.now() }),
    patchConf: (id, patch) => d({ t: "conf", id, patch }),
    addConf: (req) => d({ t: "confAdd", req }),
  }), [s, reload, loadNotifs, markRead, setAutoEnabled]);

  return <LiveCtx.Provider value={value}>{children}</LiveCtx.Provider>;
}
