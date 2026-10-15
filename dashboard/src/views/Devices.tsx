import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ConfirmationRequired } from "../api/events";
import type { Capability, Endpoint, SetResult } from "../api/types";
import { explain } from "../errors";
import { settleKey, useLive, type Settle } from "../live";

type Value = boolean | number | string;

const PHASE_LABEL = { accepted: "accepted", settling: "settling", settled: "settled", settle_timeout: "settle timeout" } as const;

// While settling, the indicator runs for at most the capability's announced settle_s. If the result message never
// arrives (lost socket), it stops claiming motion once that time is up.
export function SettleTrail({ s }: { s?: Settle }) {
  const last = s?.phases[s.phases.length - 1];
  const deadline = last === "settling" ? s?.deadline : undefined;
  const [lapsed, setLapsed] = useState(false);
  useEffect(() => {
    setLapsed(false);
    if (deadline === undefined) return;
    const left = deadline - Date.now();
    if (left <= 0) { setLapsed(true); return; }
    const t = setTimeout(() => setLapsed(true), left);
    return () => clearTimeout(t);
  }, [deadline]);
  if (!s || !last) return null;
  return (
    <span className={`settle settle-${last}`} role="status" aria-label="motion progress">
      {s.phases.map((p) => PHASE_LABEL[p]).join(" → ")}
      {last === "settling" && s.settle_s != null ? (lapsed ? ` (no result after ${s.settle_s}s)` : ` (up to ${s.settle_s}s)`) : ""}
      {last === "settled" && s.elapsed_ms != null ? ` (${(s.elapsed_ms / 1000).toFixed(1)}s)` : ""}
      {last === "settle_timeout" ? ` (did not reach target in ${s.settle_s ?? "?"}s, observed ${s.observed == null ? "none" : String(s.observed)})` : ""}
    </span>
  );
}

function CapControl({ ep, cap }: { ep: Endpoint; cap: Capability }) {
  const { noteSettle, settle } = useLive();
  const cur = ep.state?.[cap.id];
  const [draft, setDraft] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The server's own prompt (summary) and the exact action to mint for; the client writes no confirm text of its own.
  const [pending, setPending] = useState<{ value: Value; summary: string; action: ConfirmationRequired["action"] } | null>(null);
  const [busy, setBusy] = useState(false);
  const disabled = !ep.online || !ep.enabled || busy;

  const send = async (value: Value, token?: string) => {
    setErr(null);
    setBusy(true);
    try {
      const r = await api<SetResult>("POST", `/devices/${ep.endpoint_id}/set`, { capability: cap.id, value, ...(token ? { confirmation_token: token } : {}) });
      if (!r.ok) { setErr(explain(r.error ?? "failed")); return; }
      noteSettle(ep.endpoint_id, cap.id, "accepted");
      if (r.status === "moving") noteSettle(ep.endpoint_id, cap.id, "settling", cap.settle_s);
    } catch (e) {
      const code = (e as { code?: string }).code;
      const body = (e as { body?: Partial<ConfirmationRequired> }).body;
      if (code === "confirmation_required" && body?.summary && body.action) setPending({ value, summary: body.summary, action: body.action });
      else setErr(explain(e));
    } finally { setBusy(false); setDraft(null); }
  };

  // Direct flow for risk:confirm: the gateway mints the token on POST /confirmations {action}; we only pass it along.
  const confirmDirect = async () => {
    if (pending === null) return;
    setBusy(true); setErr(null);
    try {
      const m = await api<{ confirmation_token?: string }>("POST", "/confirmations", { action: pending.action });
      const v = pending.value;
      setPending(null);
      setBusy(false);
      await send(v, m.confirmation_token);
    } catch (e) { setErr(explain(e)); setPending(null); } finally { setBusy(false); }
  };

  const unit = cap.unit ? ` ${cap.unit}` : "";
  let control: React.ReactNode;
  if (cap.kind === "sensor") {
    control = <span className="readout">{cur == null ? "—" : `${String(cur)}${unit}`}</span>;
  } else if (cap.type === "bool") {
    control = (
      <label className="switch">
        <input type="checkbox" role="switch" aria-label={cap.id} checked={cur === true} disabled={disabled} onChange={(e) => send(e.target.checked)} />
        <span>{cur === true ? "on" : "off"}</span>
      </label>
    );
  } else if (cap.type === "enum") {
    control = (
      <select aria-label={cap.id} value={typeof cur === "string" ? cur : ""} disabled={disabled} onChange={(e) => send(e.target.value)}>
        {typeof cur !== "string" && <option value="" disabled>—</option>}
        {(cap.values ?? []).map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    );
  } else if (cap.min != null && cap.max != null) {
    const shown = draft ?? (typeof cur === "number" ? cur : cap.min);
    control = (
      <span className="slider">
        <input type="range" aria-label={cap.id} min={cap.min} max={cap.max} step={cap.type === "int" ? 1 : "any"} value={shown} disabled={disabled}
          onChange={(e) => setDraft(Number(e.target.value))}
          onPointerUp={() => draft !== null && send(draft)} onKeyUp={() => draft !== null && send(draft)} />
        <output>{shown}{unit}</output>
      </span>
    );
  } else {
    control = <input type="number" aria-label={cap.id} defaultValue={typeof cur === "number" ? cur : undefined} disabled={disabled}
      onKeyDown={(e) => { if (e.key === "Enter") void send(Number((e.target as HTMLInputElement).value)); }} />;
  }

  return (
    <div className="cap">
      <span className="cap-name">{cap.id}</span>
      {control}
      <SettleTrail s={settle[settleKey(ep.endpoint_id, cap.id)]} />
      {pending !== null && (
        <div className="conf" role="group" aria-label="Confirmation required">
          <p>{pending.summary}</p>
          <div className="row"><button className="primary" onClick={confirmDirect} disabled={busy}>Confirm</button><button onClick={() => setPending(null)}>Cancel</button></div>
        </div>
      )}
      {err && <span className="err" role="alert">{err}</span>}
    </div>
  );
}

export function EndpointCard({ ep }: { ep: Endpoint }) {
  return (
    <article className={`card ${ep.online ? "" : "offline"}`} aria-label={ep.name}>
      <header>
        <h3>{ep.name}</h3>
        <span className={`badge ${ep.online ? "ok" : "off"}`}>{ep.online ? "online" : "offline"}</span>
        {!ep.enabled && <span className="badge off">disabled</span>}
        {ep.risk === "confirm" && <span className="badge warn">confirm</span>}
      </header>
      <div className="muted small">{ep.type}{ep.tags?.length ? ` · ${ep.tags.join(", ")}` : ""}</div>
      {ep.capabilities.map((c) => <CapControl key={c.id} ep={ep} cap={c} />)}
    </article>
  );
}

export function Devices() {
  const { endpoints, loadError, ws } = useLive();
  const rooms = useMemo(() => {
    const m = new Map<string, Endpoint[]>();
    for (const e of endpoints ?? []) m.set(e.room, [...(m.get(e.room) ?? []), e]);
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [endpoints]);

  return (
    <section>
      <h2>Devices <span className={`ws ws-${ws}`} aria-label={`live connection ${ws}`}>{ws === "open" ? "live" : ws}</span></h2>
      {loadError && <p className="err" role="alert">{loadError}</p>}
      {endpoints === null && !loadError && <p className="muted">Loading…</p>}
      {endpoints?.length === 0 && <p className="muted">No devices visible to you.</p>}
      {rooms.map(([room, eps]) => (
        <div key={room} className="room">
          <h3 className="room-name">{room}</h3>
          <div className="grid">{eps.map((e) => <EndpointCard key={e.endpoint_id} ep={e} />)}</div>
        </div>
      ))}
    </section>
  );
}
