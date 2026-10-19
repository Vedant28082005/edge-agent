import { useRef, useState } from "react";
import { session } from "../api/client";
import { streamChat } from "../api/sse";
import type { SseEvent, WsMessage } from "../api/events";
import { explain } from "../errors";
import { useLive } from "../live";

type Tool = { call_id: string; tool: string; args: unknown; result?: unknown; ok?: boolean };
type Msg =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; final: boolean; route?: string; latency_ms?: number; status?: string; tools: Tool[]; error?: string };

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

export function Chat() {
  const live = useLive();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const sessionId = useRef(`web-${Math.random().toString(36).slice(2, 10)}`);
  const abort = useRef<AbortController | null>(null);

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
    setMsgs((m) => [...m, { kind: "user", text: t }, { kind: "assistant", text: "", final: false, tools: [] }]);
    abort.current = new AbortController();
    try {
      await streamChat({ session_id: sessionId.current, text: t }, onEvent, abort.current.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") patchLast((m) => { m.error = explain(e); });
    } finally {
      patchLast((m) => { m.final = true; });
      setBusy(false);
    }
  };

  return (
    <section className="chat">
      <h2>Chat</h2>
      <div className="log" aria-live="polite">
        {msgs.map((m, i) => m.kind === "user" ? (
          <div key={i} className="bubble user">{m.text}</div>
        ) : (
          <div key={i} className={`bubble assistant${m.final ? "" : " streaming"}`}>
            <ToolChips tools={m.tools} />
            {m.status && !m.final && <div className="muted small">{m.status}</div>}
            {m.text && <div className="reply">{m.text}{!m.final && <span className="caret" aria-hidden="true" />}</div>}
            {!m.final && !m.text && !m.status && <div className="muted small">thinking…</div>}
            {m.error && <div className="err" role="alert">{m.error}</div>}
            {m.final && m.route && <div className="muted small">{m.route}{m.latency_ms != null ? ` · ${m.latency_ms} ms` : ""}</div>}
          </div>
        ))}
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); void send(); }}>
        <span className="reserved-slot" aria-hidden="true" />
        <input aria-label="Message" value={text} maxLength={4000} onChange={(e) => setText(e.target.value)}
          placeholder={session.role === "guest" ? "Ask a question…" : "Ask or command…"} />
        <button className="primary" disabled={busy || !text.trim()}>Send</button>
      </form>
    </section>
  );
}
