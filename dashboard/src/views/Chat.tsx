import { useRef, useState } from "react";
import { streamChat } from "../api/sse";
import type { SseEvent, WsMessage } from "../api/events";
import { explain } from "../errors";
import { useLive } from "../live";

type Tool = { call_id: string; tool: string; args: unknown; result?: unknown; ok?: boolean };
type Msg =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; final: boolean; route?: string; latency_ms?: number; status?: string; tools: Tool[]; error?: string };

const short = (v: unknown, n = 80) => {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? `${s.slice(0, n)}…` : s;
};

const FORWARD = new Set(["confirmation_request", "confirmation_declined", "confirmation_expired", "action_settled", "action_settle_timeout"]);

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
    const d = e.data as Record<string, any>;
    if (FORWARD.has(e.event)) live.dispatch({ type: e.event, ...d } as unknown as WsMessage);
    switch (e.event) {
      case "status": patchLast((m) => { m.status = String(d.text ?? ""); }); break;
      case "token": patchLast((m) => { m.text += String(d.text ?? ""); }); break;
      case "tool_call": patchLast((m) => { m.tools.push({ call_id: String(d.call_id), tool: String(d.tool), args: d.args }); }); break;
      case "tool_result": patchLast((m) => {
        const t = m.tools.find((x) => x.call_id === d.call_id);
        if (t) { t.result = d.result; t.ok = Boolean(d.ok); } else m.tools.push({ call_id: String(d.call_id), tool: String(d.tool), args: undefined, result: d.result, ok: Boolean(d.ok) });
      }); break;
      case "final": patchLast((m) => { m.text = String(d.text ?? m.text); m.final = true; m.status = undefined; m.route = d.route; m.latency_ms = d.latency_ms; }); break;
      case "async_result": patchLast((m) => { m.text += `${m.text ? "\n" : ""}${String(d.text ?? "")}`; }); break;
      case "error": patchLast((m) => { m.error = explain(String(d.code ?? "error")); m.final = true; }); break;
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
          <div key={i} className="bubble bot">
            {m.tools.length > 0 && (
              <ul className="tools">
                {m.tools.map((t) => (
                  <li key={t.call_id} className={t.ok === false ? "bad" : ""}>
                    <details>
                      <summary>
                        <code>{t.tool}</code> {t.args !== undefined && <span className="muted">{short(t.args, 60)}</span>}
                        {" "}{t.ok === undefined ? "…" : t.ok ? "✓" : "✗"}
                      </summary>
                      <pre>{JSON.stringify({ args: t.args, result: t.result }, null, 2)}</pre>
                    </details>
                  </li>
                ))}
              </ul>
            )}
            {m.status && !m.final && <div className="muted small">{m.status}</div>}
            {m.text && <div className="reply">{m.text}</div>}
            {!m.final && !m.text && !m.status && <div className="muted small">thinking…</div>}
            {m.error && <div className="err" role="alert">{m.error}</div>}
            {m.final && m.route && <div className="muted small">{m.route}{m.latency_ms != null ? ` · ${m.latency_ms} ms` : ""}</div>}
          </div>
        ))}
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); void send(); }}>
        <input aria-label="Message" value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} placeholder="Ask or command…" />
        <button className="primary" disabled={busy || !text.trim()}>Send</button>
      </form>
    </section>
  );
}
