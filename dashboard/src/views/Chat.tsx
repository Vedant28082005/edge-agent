import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { session } from "../api/client";
import { streamChat } from "../api/sse";
import type { SseEvent, WsMessage } from "../api/events";
import { chatStore } from "../chatStore";
import type { Msg, Tool } from "../chatStore";
import { explain } from "../errors";
import { useLive } from "../live";

type ChipStatus = "done" | "running" | "failed" | "confirm";
const STATUS_WORD: Record<ChipStatus, string> = { done: "done", running: "running", failed: "failed", confirm: "needs confirmation" };
const STATUS_ICON: Record<ChipStatus, string> = { done: "✓", running: "…", failed: "✗", confirm: "…" };

// Plain-word results only: no raw tool args, ids or payloads are ever rendered, only the tool name (a fixed,
// known enum, not a secret) and a status word.
function toolStatus(t: Tool): ChipStatus {
  if (t.ok === undefined) return "running";
  if (t.ok === false) return "failed";
  const r = t.result as { error?: string } | undefined;
  if (r && typeof r === "object" && r.error === "confirmation_required") return "confirm";
  return "done";
}

function ToolChips({ tools }: { tools: Tool[] }) {
  if (!tools.length) return null;
  const shown = tools.slice(0, 4);
  const extra = tools.length - shown.length;
  return (
    <ul className="chips" aria-label="Tool calls">
      {shown.map((t) => {
        const status = toolStatus(t);
        return (
          <li key={t.call_id} className={`chip chip-${status}`}>
            <code>{t.tool}</code>
            <span>{STATUS_ICON[status]} {STATUS_WORD[status]}</span>
          </li>
        );
      })}
      {extra > 0 && <li className="chip chip-more">+{extra} more</li>}
    </ul>
  );
}

// No explicit "clarify" event exists: a tool-less reply that ends in a question is styled as a clarifying one.
const isClarify = (m: Extract<Msg, { kind: "assistant" }>) =>
  m.final && !m.error && !m.stopped && m.tools.length === 0 && m.text.trim().endsWith("?");

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

  const scrollToEnd = () => { const el = logRef.current; if (el) el.scrollTop = el.scrollHeight; };
  const onScroll = () => {
    const el = logRef.current; if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    stick.current = near; setAtBottom(near);
  };
  // history restore (mount) and every new/streamed message: follow the newest unless the user scrolled up
  useLayoutEffect(() => { if (stick.current) scrollToEnd(); }, [msgs]);
  useEffect(() => {
    if (!busy) { inputRef.current?.focus(); return; }
    const started = Date.now();
    setElapsed(0);
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(id);
  }, [busy]);

  const patchLast = (fn: (m: Extract<Msg, { kind: "assistant" }>) => void) =>
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
      case "status": { const d = e.data; patchLast((m) => { m.status = d.text; }); break; }
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

  const send = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setText(""); setBusy(true);
    stick.current = true; setAtBottom(true);
    const now = Date.now();
    setMsgs((m) => [...m, { kind: "user", text: t, ts: now }, { kind: "assistant", text: "", final: false, tools: [], ts: now }]);
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
    abort.current?.abort(); abort.current = null;
    setBusy(false); setText("");
    stick.current = true; setAtBottom(true);
    chatStore.clear();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); }
  };

  return (
    <section className="chat">
      <div className="chat-head">
        <h2>Chat</h2>
        <button type="button" className="ghost sm" onClick={clearChat} disabled={!msgs.length && !busy}>Clear chat</button>
      </div>
      <div className="log-wrap">
        <div className="log" ref={logRef} onScroll={onScroll} aria-live="polite">
          {msgs.map((m, i) => m.kind === "user" ? (
            <div key={i} className="bubble user">{m.text}</div>
          ) : (
            <div key={i} className={`bubble assistant${m.final ? "" : " streaming"}${m.error ? " is-error" : ""}${isClarify(m) ? " is-clarify" : ""}`}>
              <ToolChips tools={m.tools} />
              {m.status && !m.final && <div className="muted small">{m.status}</div>}
              {m.text && <div className="reply">{m.text}{!m.final && <span className="caret" aria-hidden="true" />}</div>}
              {!m.final && (
                <div className="typing muted small" role="status">
                  <span className="dots" aria-hidden="true"><i /><i /><i /></span>
                  {!m.text && !m.status && <span>thinking…</span>}
                  <span>{elapsed}s</span>
                </div>
              )}
              {m.final && !m.text && !m.tools.length && !m.error && !m.stopped && <div className="muted small">No reply</div>}
              {m.error && <div className="err" role="alert">{m.error}</div>}
              {m.stopped && <div className="muted small">Stopped. Actions already done are kept.</div>}
              {m.final && (m.route || m.ts) && (
                <div className="meta">
                  {m.route && <span className={`route route-${m.route}`}>{m.route}{m.latency_ms != null ? ` · ${m.latency_ms} ms` : ""}</span>}
                  {m.ts && <time dateTime={new Date(m.ts).toISOString()}>{new Date(m.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>}
                </div>
              )}
            </div>
          ))}
        </div>
        {!atBottom && <button type="button" className="jump" onClick={() => { stick.current = true; setAtBottom(true); scrollToEnd(); }}>Jump to latest</button>}
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); if (!busy) void send(); }}>
        <span className="reserved-slot" aria-hidden="true" />
        <textarea ref={inputRef} aria-label="Message" rows={1} value={text} maxLength={4000} autoFocus
          onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown}
          placeholder={session.role === "guest" ? "Ask a question…" : "Ask or command…"} />
        {busy
          ? <button type="button" className="danger" onClick={stop}>Stop</button>
          : <button className="primary" disabled={!text.trim()}>Send</button>}
      </form>
    </section>
  );
}
