import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { session } from "../api/client";
import { streamChat } from "../api/sse";
import type { SseEvent, WsMessage } from "../api/events";
import type { Endpoint } from "../api/types";
import { chatStore } from "../chatStore";
import type { Msg, Tool } from "../chatStore";
import { explain } from "../errors";
import { IconChevron, IconCheck, IconCopy, IconDown, IconMore, IconSend, IconStop } from "../icons";
import { useLive } from "../live";

type Asst = Extract<Msg, { kind: "assistant" }>;
type ToolStatus = "done" | "running" | "failed" | "confirm";

function toolStatus(t: Tool): ToolStatus {
  if (t.ok === undefined) return "running";
  if (t.ok === false) return "failed";
  const r = t.result as { error?: string } | undefined;
  if (r && typeof r === "object" && r.error === "confirmation_required") return "confirm";
  return "done";
}

// Plain-language steps. Only the fixed tool names, admin-set device names and boolean/number values are used:
// endpoint ids, scene ids, raw args/results and tokens are never rendered.
const CAP_WORD: Record<string, string> = { brightness: "brightness", position: "position", target: "target temperature", mode: "mode", speed: "speed" };

export function describeTool(t: Tool, nameOf: (id: unknown) => string | null): string {
  const st = toolStatus(t);
  const a = (t.args && typeof t.args === "object" ? t.args : {}) as Record<string, unknown>;
  const name = nameOf(a.endpoint_id) ?? "a device";
  const pick = (running: string, done: string, failed: string, confirm: string) =>
    st === "running" ? running : st === "failed" ? failed : st === "confirm" ? confirm : done;
  switch (t.tool) {
    case "set_state": {
      const v = a.value;
      if (a.capability === "power" && typeof v === "boolean") {
        const w = v ? "on" : "off";
        return pick(`Turning ${w} ${name}`, `Turned ${w} ${name}`, `Couldn't turn ${w} ${name}`, `${name} needs your confirmation`);
      }
      if (a.capability === "lock") {
        const w = v === "locked" || v === true ? "Lock" : "Unlock";
        return pick(`${w}ing ${name}`, `${w}ed ${name}`, `Couldn't ${w.toLowerCase()} ${name}`, `${w}ing ${name} needs your confirmation`);
      }
      const cap = typeof a.capability === "string" ? CAP_WORD[a.capability] ?? "a setting" : "a setting";
      const val = typeof v === "number" ? ` to ${v}` : "";
      return pick(`Changing ${cap} of ${name}`, `Changed ${cap} of ${name}${val}`, `Couldn't change ${cap} of ${name}`, `${name} needs your confirmation`);
    }
    case "get_state": return pick(`Checking ${name}`, `Checked ${name}`, `Couldn't check ${name}`, `Checked ${name}`);
    case "read_sensor": return pick(`Reading ${name}`, `Read ${name}`, `Couldn't read ${name}`, `Read ${name}`);
    case "list_devices": return pick("Looking up your devices", "Looked up your devices", "Couldn't look up your devices", "Looked up your devices");
    case "run_scene": return pick("Running a scene", "Ran a scene", "Couldn't run the scene", "The scene needs your confirmation");
    default: return pick("Working on it", "Did an action", "An action didn't work", "An action needs your confirmation");
  }
}

function ToolSteps({ tools, nameOf }: { tools: Tool[]; nameOf: (id: unknown) => string | null }) {
  if (!tools.length) return null;
  const running = tools.some((t) => toolStatus(t) === "running");
  const label = running ? "Working…" : `Used ${tools.length} ${tools.length === 1 ? "tool" : "tools"}`;
  return (
    <details className="tools">
      <summary><IconChevron /><span>{label}</span></summary>
      <ul aria-label="Steps">
        {tools.map((t) => <li key={t.call_id} className={`step step-${toolStatus(t)}`}>{describeTool(t, nameOf)}</li>)}
      </ul>
    </details>
  );
}

// No explicit "clarify" event exists: a tool-less reply that ends in a question is styled as a clarifying one.
const isClarify = (m: Asst) => m.final && !m.error && !m.stopped && m.tools.length === 0 && m.text.trim().endsWith("?");

const secs = (ms: number) => (ms < 950 ? "<1 s" : ms < 10_000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 1000)} s`);
export function metaLabel(m: Asst): string | null {
  if (!m.final || m.error || m.stopped) return null;
  const t = m.latency_ms != null ? ` · ${secs(m.latency_ms)}` : "";
  if (m.route === "fast") return `Quick action${t}`;
  if (m.route === "llm") return m.latency_ms != null ? `Thought for ${secs(m.latency_ms)}` : "Thought";
  if (m.route) return `Answered${t}`;
  return null;
}

function Reply({ m }: { m: Asst }) {
  // when the final event replaces what streamed in, fade the swap instead of jumping
  const prev = useRef({ final: m.final, text: m.text });
  const [fade, setFade] = useState(false);
  useEffect(() => {
    const p = prev.current;
    prev.current = { final: m.final, text: m.text };
    if (m.final && !p.final && p.text && p.text !== m.text) {
      setFade(true);
      const id = setTimeout(() => setFade(false), 400);
      return () => clearTimeout(id);
    }
  }, [m.final, m.text]);
  return <div className={`reply${fade ? " swap" : ""}`}>{m.text}{!m.final && <span className="caret" aria-hidden="true" />}</div>;
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* clipboard unavailable */ }
  };
  return (
    <button type="button" className="ghost icon-btn copy" onClick={() => void copy()} aria-label={done ? "Copied" : "Copy reply"} title="Copy reply">
      {done ? <IconCheck /> : <IconCopy />}
    </button>
  );
}

const hour = () => new Date().getHours();
const greeting = () => (hour() < 5 ? "Hello" : hour() < 12 ? "Good morning" : hour() < 18 ? "Good afternoon" : "Good evening");

// Suggestions come from the user's real devices (admin-set names) or are generic; never hard-coded device names.
export function suggestions(eps: Endpoint[] | null, guest: boolean): string[] {
  const out: string[] = [];
  const list = eps ?? [];
  const light = list.find((e) => e.type === "light" && e.online !== false);
  const lock = list.find((e) => e.type === "lock");
  const climate = list.find((e) => e.capabilities?.some((c) => c.id === "temperature"));
  if (light && !guest) out.push(`Turn on ${light.name}`);
  if (lock) out.push(`Is ${lock.name} locked?`);
  if (climate) out.push(`What's the temperature in ${climate.room}?`);
  if (light && guest) out.push(`Is ${light.name} on?`);
  out.push("What's on right now?");
  if (out.length < 3) out.push("What can you do?");
  return out.slice(0, 4);
}

export function Chat() {
  const live = useLive();
  const msgs = useSyncExternalStore(chatStore.subscribe, chatStore.getMsgs);
  const setMsgs = chatStore.setMsgs;
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const stick = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const [elapsed, setElapsed] = useState(0);
  const [menu, setMenu] = useState(false);
  const guest = session.role === "guest";

  const nameOf = useMemo(() => {
    const byId = new Map((live.endpoints ?? []).map((e) => [e.endpoint_id, e.name]));
    return (id: unknown) => (typeof id === "string" ? byId.get(id) ?? null : null);
  }, [live.endpoints]);
  const chips = useMemo(() => suggestions(live.endpoints, guest), [live.endpoints, guest]);

  const scrollToEnd = (smooth = false) => {
    const el = logRef.current; if (!el) return;
    if (smooth && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    else el.scrollTop = el.scrollHeight;
  };
  const onScroll = () => {
    const el = logRef.current; if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    stick.current = near; setAtBottom(near);
  };
  // history restore (mount) and every new/streamed message: follow the newest unless the user scrolled up
  useLayoutEffect(() => { if (stick.current) scrollToEnd(); }, [msgs]);
  // grow with the content (1 to 6 lines)
  useLayoutEffect(() => {
    const el = inputRef.current; if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 6 * 24 + 4)}px`;
  }, [text]);
  useEffect(() => {
    if (!busy) { inputRef.current?.focus(); return; }
    const started = Date.now();
    setElapsed(0);
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(id);
  }, [busy]);
  useEffect(() => {
    if (!menu) return;
    const close = (e: Event) => { if (!(e.target as Element).closest?.(".chat-menu")) setMenu(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(false); };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", esc); };
  }, [menu]);

  const patchLast = (fn: (m: Asst) => void) =>
    setMsgs((all) => {
      const copy = all.slice();
      const last = copy[copy.length - 1];
      if (last?.kind === "assistant") { const n = { ...last, tools: last.tools.map((t) => ({ ...t })) }; fn(n); copy[copy.length - 1] = n; }
      return copy;
    });

  const onEvent = (e: SseEvent) => {
    switch (e.event) {
      case "confirmation_request": case "confirmation_declined": case "confirmation_expired":
      case "action_settled": case "action_settle_timeout": {
        const { seq: _seq, req_id: _req, ...rest } = e.data; // the SSE envelope is not part of the WsMessage shape
        void _seq; void _req;
        live.dispatch({ type: e.event, ...rest } as WsMessage);
        break;
      }
      case "status": { const d = e.data; patchLast((m) => { m.status = d.text; }); break; } // also the keepalive
      case "token": { const d = e.data; patchLast((m) => { m.text += d.text; }); break; }
      case "tool_call": { const d = e.data; patchLast((m) => { m.tools.push({ call_id: d.call_id, tool: d.tool, args: d.args }); }); break; }
      case "tool_result": { const d = e.data; patchLast((m) => {
        const t = m.tools.find((x) => x.call_id === d.call_id);
        if (t) { t.result = d.result; t.ok = d.ok; } else m.tools.push({ call_id: d.call_id, tool: d.tool, args: undefined, result: d.result, ok: d.ok });
      }); break; }
      case "final": { const d = e.data; patchLast((m) => { m.text = d.text; m.final = true; m.status = undefined; m.route = d.route; m.latency_ms = d.latency_ms; }); break; }
      case "async_result": { const d = e.data; patchLast((m) => { m.text += (m.text ? "\n" : "") + d.text; }); break; }
      case "error": { const d = e.data; patchLast((m) => { m.error = explain(d.code); m.final = true; }); break; }
    }
  };

  const send = async (override?: string) => {
    const t = (override ?? text).trim();
    if (!t || busy) return;
    setText(""); setBusy(true);
    stick.current = true; setAtBottom(true);
    const now = Date.now();
    setMsgs((m) => [...m, { kind: "user", text: t, ts: now }, { kind: "assistant", text: "", final: false, tools: [], ts: now }]);
    requestAnimationFrame(() => { if (stick.current) scrollToEnd(true); }); // smooth scroll on send, unless the user already scrolled away
    const ctl = new AbortController();
    abort.current = ctl;
    try {
      await streamChat({ session_id: chatStore.sessionId, text: t }, onEvent, ctl.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") patchLast((m) => { m.error = explain(e); });
    } finally {
      if (abort.current === ctl) { // not already stopped/cleared
        patchLast((m) => { m.final = true; });
        abort.current = null;
        setBusy(false);
      }
    }
  };

  // Aborts only our fetch. Pending confirmation cards live in the live store and need an explicit decision.
  const stop = () => {
    abort.current?.abort();
    abort.current = null;
    patchLast((m) => { if (!m.final) { m.stopped = true; m.final = true; m.status = undefined; } });
    setBusy(false);
  };

  const clearChat = () => {
    setMenu(false);
    abort.current?.abort(); abort.current = null;
    setBusy(false); setText("");
    stick.current = true; setAtBottom(true);
    chatStore.clear();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!busy) void send(); }
  };

  return (
    <section className="chat">
      <div className="chat-scroll" ref={logRef} onScroll={onScroll}>
        <div className="chat-col">
          <div className="chat-head">
            <h2>Chat</h2>
            <div className="chat-menu">
              <button type="button" className="ghost icon-btn" aria-label="Chat options" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((o) => !o)}><IconMore /></button>
              {menu && (
                <div className="menu" role="menu">
                  <button type="button" role="menuitem" className="ghost" onClick={clearChat} disabled={!msgs.length && !busy}>Clear chat</button>
                </div>
              )}
            </div>
          </div>
          {msgs.length === 0 && (
            <div className="chat-empty">
              <p className="greeting">{greeting()}</p>
              <p className="muted">{guest ? "Ask a question about your home." : "Ask a question or tell me what to do."}</p>
              <ul className="suggest plain" aria-label="Suggestions">
                {chips.map((c) => <li key={c}><button type="button" className="secondary" onClick={() => void send(c)} disabled={busy}>{c}</button></li>)}
              </ul>
            </div>
          )}
          <div className="msgs" aria-live="polite">
            {msgs.map((m, i) => m.kind === "user" ? (
              <div key={i} className="msg msg-user"><div className="user-bubble">{m.text}</div></div>
            ) : (
              <div key={i} className={`msg msg-asst${m.final ? "" : " streaming"}${m.error ? " is-error" : ""}${isClarify(m) ? " is-clarify" : ""}`}>
                <ToolSteps tools={m.tools} nameOf={nameOf} />
                {m.status && !m.final && <div className="status-line">{m.status}</div>}
                {m.text && <Reply m={m} />}
                {!m.final && !m.text && (
                  <div className="typing" role="status">
                    <span className="dots" aria-hidden="true"><i /><i /><i /></span>
                    {!m.status && <span>Thinking…</span>}
                    <span className="elapsed">{elapsed}s</span>
                  </div>
                )}
                {m.final && !m.text && !m.tools.length && !m.error && !m.stopped && <div className="muted small">No reply</div>}
                {m.error && <div className="err" role="alert">{m.error}</div>}
                {m.stopped && <div className="muted small">Stopped. Actions already done are kept.</div>}
                {m.final && (
                  <div className="meta">
                    {(() => {
                      const label = metaLabel(m);
                      return m.ts && label ? <time dateTime={new Date(m.ts).toISOString()} title={new Date(m.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}>{label}</time> : null;
                    })()}
                    {m.text && <CopyButton text={m.text} />}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
      {!atBottom && <button type="button" className="jump" onClick={() => { stick.current = true; setAtBottom(true); scrollToEnd(true); }}><IconDown /> Jump to latest</button>}
      <div className="composer-dock">
        <form className="composer" onSubmit={(e) => { e.preventDefault(); if (!busy) void send(); }}>
          <textarea ref={inputRef} aria-label="Message" rows={1} value={text} maxLength={4000} autoFocus
            onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown}
            placeholder={guest ? "Ask a question…" : "Ask or command…"} />
          {busy
            ? <button type="button" className="send stop" aria-label="Stop" title="Stop" onClick={stop}><IconStop /></button>
            : <button type="submit" className="send" aria-label="Send" title="Send" disabled={!text.trim()}><IconSend /></button>}
        </form>
        <p className="hint">Enter to send, Shift+Enter for a new line</p>
      </div>
    </section>
  );
}
