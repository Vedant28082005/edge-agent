import { useEffect, useMemo, useRef, useState } from "react";
import { api, session } from "../api/client";
import type { ConfirmationRequired } from "../api/events";
import type { Capability, Endpoint, SetResult } from "../api/types";
import { explain } from "../errors";
import { settleKey, useLive, type Settle } from "../live";

type Value = boolean | number | string;

const CAP_NAMES: Record<string, string> = { power: "Power", brightness: "Brightness", speed: "Fan speed", position: "Position", target: "Target temperature", mode: "Mode", temperature: "Temperature", humidity: "Humidity", lock: "Lock", color_temp: "Colour temperature" };
export const capLabel = (id: string) => CAP_NAMES[id] ?? (id.charAt(0).toUpperCase() + id.slice(1).replace(/_/g, " "));
const TYPE_ICON: Record<string, string> = { light: "💡", fan: "🌀", outlet: "🔌", cover: "▤", thermostat: "🌡", sensor: "◉", lock: "🔒" };
const TOGGLE_DEBOUNCE_MS = 250;

const PHASE_LABEL = { accepted: "accepted", settling: "settling", settled: "settled", settle_timeout: "settle timeout" } as const;

// While settling, the indicator runs for at most the capability's announced settle_s. If the result message never
// arrives (lost socket), it stops claiming motion once that time is up.
// Driven strictly by server events (action_settled / action_settle_timeout) — never shows the target as reached
// before "settled".
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
    <span className={`motion settle-${last}`} role="status" aria-label="motion progress">
      {s.phases.map((p) => PHASE_LABEL[p]).join(" → ")}
      {last === "settling" && s.settle_s != null ? (lapsed ? ` (no result after ${s.settle_s}s)` : ` (up to ${s.settle_s}s)`) : ""}
      {last === "settled" && s.elapsed_ms != null ? ` (${(s.elapsed_ms / 1000).toFixed(1)}s)` : ""}
      {last === "settle_timeout" ? ` (did not reach target in ${s.settle_s ?? "?"}s, observed ${s.observed == null ? "none" : String(s.observed)})` : ""}
    </span>
  );
}

type Pending = { value: Value; summary: string; action: ConfirmationRequired["action"] };

// Shared send/confirm machinery for any capability set, used by the generic control and by LockCard.
// The server's own prompt (summary) and the exact action to mint for are the only confirm text shown;
// the client never writes its own wording and never sees or stores the confirmation handle.
function useCapabilityAction(ep: Endpoint, cap: Capability) {
  const { noteSettle } = useLive();
  const [err, setErr] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);

  const send = async (value: Value, token?: string): Promise<boolean> => {
    setErr(null);
    setBusy(true);
    try {
      const r = await api<SetResult>("POST", `/devices/${ep.endpoint_id}/set`, { capability: cap.id, value, ...(token ? { confirmation_token: token } : {}) });
      if (!r.ok) { setErr(explain(r.error ?? "failed")); return false; }
      noteSettle(ep.endpoint_id, cap.id, "accepted");
      if (r.status === "moving") noteSettle(ep.endpoint_id, cap.id, "settling", cap.settle_s);
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code;
      const body = (e as { body?: Partial<ConfirmationRequired> }).body;
      if (code === "confirmation_required" && body?.summary && body.action) setPending({ value, summary: body.summary, action: body.action });
      else setErr(explain(e));
      return false;
    } finally { setBusy(false); }
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

  return { send, confirmDirect, cancelPending: () => setPending(null), pending, busy, err, setErr };
}

function ConfirmInline({ pending, busy, onConfirm, onCancel }: { pending: Pending; busy: boolean; onConfirm: () => void; onCancel: () => void }) {
  return (
    <div className="conf" role="group" aria-label="Confirmation required">
      <p>{pending.summary}</p>
      <div className="row">
        <button className="primary" onClick={onConfirm} disabled={busy}>Confirm</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function CapControl({ ep, cap, hubDown }: { ep: Endpoint; cap: Capability; hubDown: boolean }) {
  const { settle } = useLive();
  const { send, confirmDirect, cancelPending, pending, busy, err } = useCapabilityAction(ep, cap);
  const cur = ep.state?.[cap.id];
  const [draft, setDraft] = useState<number | null>(null);
  const [want, setWant] = useState<boolean | null>(null); // switch value being sent (shown until the server state arrives)
  const [done, setDone] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const doneTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const guest = session.role === "guest";
  // When the hub itself is unreachable, every device is treated as offline with its last known state.
  const offline = !ep.online || !ep.enabled || hubDown;
  const disabled = offline || busy;
  const label = capLabel(cap.id);

  useEffect(() => () => { clearTimeout(timer.current); clearTimeout(doneTimer.current); }, []);
  // The server state arrived: drop the pending values.
  useEffect(() => { setDraft(null); setWant(null); }, [cur]);

  // A failed send reverts the pending value (rule 8); a successful one flashes "Done" briefly.
  const commit = async (value: Value) => {
    setDone(false);
    const ok = await send(value);
    if (!ok) { setDraft(null); setWant(null); return; }
    setDone(true);
    clearTimeout(doneTimer.current);
    doneTimer.current = setTimeout(() => setDone(false), 1800);
  };
  // Rapid toggles collapse into one command carrying the last value.
  const toggle = (v: boolean) => {
    setWant(v);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => { void commit(v); }, TOGGLE_DEBOUNCE_MS);
  };

  const unit = cap.unit ? ` ${cap.unit}` : "";
  let control: React.ReactNode;
  // Guests never see a control that won't work for them: read-only state text only, no switch/select/slider/input.
  if (guest) {
    control = <span className="readout">{cur == null ? "—" : `${String(cur)}${cap.kind === "sensor" ? unit : ""}`}</span>;
  } else if (cap.kind === "sensor") {
    control = <span className="readout">{cur == null ? "—" : `${String(cur)}${unit}`}</span>;
  } else if (cap.type === "bool") {
    const on = want ?? cur === true;
    control = (
      <label className="switch">
        <input type="checkbox" role="switch" aria-label={label} checked={on} disabled={offline} tabIndex={offline ? -1 : undefined} onChange={(e) => toggle(e.target.checked)} />
        <span className="switch-label">{on ? "On" : "Off"}</span>
      </label>
    );
  } else if (cap.type === "enum") {
    control = (
      <select aria-label={label} value={typeof cur === "string" ? cur : ""} disabled={disabled} tabIndex={offline ? -1 : undefined} onChange={(e) => void commit(e.target.value)}>
        {typeof cur !== "string" && <option value="" disabled>—</option>}
        {(cap.values ?? []).map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    );
  } else if (cap.min != null && cap.max != null) {
    const shown = draft ?? (typeof cur === "number" ? cur : cap.min);
    // The command goes out on release (pointer up / key up), never on every drag tick.
    const release = () => { if (draft !== null && draft !== cur) void commit(draft); };
    control = (
      <span className="slider">
        <input type="range" aria-label={label} min={cap.min} max={cap.max} step={cap.type === "int" ? 1 : "any"} value={shown} disabled={disabled} tabIndex={offline ? -1 : undefined}
          onChange={(e) => setDraft(Number(e.target.value))} onPointerUp={release} onKeyUp={release} />
        <output aria-label={`${label} value`}>{shown}{unit}</output>
      </span>
    );
  } else {
    control = <input type="number" aria-label={label} defaultValue={typeof cur === "number" ? cur : undefined} disabled={disabled} tabIndex={offline ? -1 : undefined}
      onKeyDown={(e) => { if (e.key === "Enter") void commit(Number((e.target as HTMLInputElement).value)); }} />;
  }

  const sending = busy || want !== null || (draft !== null && draft !== cur);
  const failed = !guest && !!err;
  return (
    <div className="cap">
      <span className="cap-name">{label}</span>
      {control}
      {!guest && !offline && (
        <span className={`cap-state ${failed ? "failed" : sending ? "sending" : done ? "done" : ""}`} aria-live="polite">
          {failed ? "Failed" : sending ? "Sending…" : done ? "Done" : ""}
        </span>
      )}
      <SettleTrail s={settle[settleKey(ep.endpoint_id, cap.id)]} />
      {!guest && pending !== null && <ConfirmInline pending={pending} busy={busy} onConfirm={confirmDirect} onCancel={cancelPending} />}
      {!guest && err && <span className="err" role="alert">{err}</span>}
    </div>
  );
}

export function EndpointCard({ ep, hubDown = false }: { ep: Endpoint; hubDown?: boolean }) {
  const guest = session.role === "guest";
  const online = ep.online && !hubDown;
  return (
    <article className={`card ${online ? "" : "offline"}`} aria-label={ep.name}>
      <header>
        <h3>{ep.name}</h3>
        <span className={`badge ${online ? "ok" : "off"}`}>{online ? "online" : "offline"}</span>
        {!ep.enabled && <span className="badge off">disabled</span>}
        {guest && <span className="badge view-only">View only</span>}
      </header>
      <div className="muted small"><span className="type-badge"><span aria-hidden="true">{TYPE_ICON[ep.type] ?? "•"}</span> {ep.type}</span>{ep.tags?.length ? ` · ${ep.tags.join(", ")}` : ""}</div>
      {ep.capabilities.map((c) => <CapControl key={c.id} ep={ep} cap={c} hubDown={hubDown} />)}
    </article>
  );
}

// Risky devices look different: never a switch/toggle. Unlock always goes through the server's confirmation.
// An unlocked lock washes the whole card risk-soft.
export function LockCard({ ep, hubDown = false }: { ep: Endpoint; hubDown?: boolean }) {
  const cap = ep.capabilities.find((c) => c.kind === "actuator") ?? ep.capabilities[0];
  const { send, confirmDirect, cancelPending, pending, busy, err } = useCapabilityAction(ep, cap);
  const cur = ep.state?.[cap.id];
  const locked = cur !== "unlocked";
  const guest = session.role === "guest";
  const online = ep.online && !hubDown;
  const offline = !online || !ep.enabled;
  const disabled = offline || busy;

  return (
    <article className={`lock-card ${locked ? "" : "unlocked"} ${offline ? "offline" : ""}`} aria-label={ep.name}>
      <header>
        <h3>{ep.name}</h3>
        <span className={`badge ${online ? "ok" : "off"}`}>{online ? "online" : "offline"}</span>
        {guest && <span className="badge view-only">View only</span>}
      </header>
      <span className="secured-label">Secured device</span>
      <p className="lock-state">{locked ? "Locked" : "Unlocked"}</p>
      {!guest && (
        <button
          className="primary unlock-action"
          disabled={disabled}
          tabIndex={offline ? -1 : undefined}
          onClick={() => send(locked ? "unlocked" : "locked")}
        >
          {locked ? "Unlock…" : "Lock…"}
        </button>
      )}
      {!guest && pending !== null && <ConfirmInline pending={pending} busy={busy} onConfirm={confirmDirect} onCancel={cancelPending} />}
      {!guest && err && <span className="err" role="alert">{err}</span>}
    </article>
  );
}

type Reading = { label: string; value: string };

// Room readings strip: sensors, AC target, blind position — 1 to 4 values, generic across endpoint types.
function roomReadings(eps: Endpoint[]): Reading[] {
  const out: Reading[] = [];
  for (const e of eps) {
    for (const c of e.capabilities) {
      if (out.length >= 4) break;
      const v = e.state?.[c.id];
      if (v == null) continue;
      if (c.kind === "sensor") out.push({ label: e.name, value: `${String(v)}${c.unit ? ` ${c.unit}` : ""}` });
      else if (e.type === "cover" && c.id === "position") out.push({ label: e.name, value: `${String(v)}${c.unit ?? "%"}` });
      else if (e.type === "thermostat" && c.id === "target") out.push({ label: e.name, value: `${String(v)}${c.unit ? ` ${c.unit}` : ""}` });
    }
  }
  return out.slice(0, 4);
}

function ReadingsStrip({ eps }: { eps: Endpoint[] }) {
  const readings = roomReadings(eps);
  if (!readings.length) return null;
  return (
    <div className="readings-strip" aria-label="Room readings">
      {readings.map((r, i) => (
        <div key={i} className="reading">
          <div className="reading-label">{r.label}</div>
          <div className="reading-value">{r.value}</div>
        </div>
      ))}
    </div>
  );
}

// "2 on · 1 off" over devices that report a power state; otherwise a plain device count.
function roomSummary(eps: Endpoint[]): string {
  const powered = eps.filter((e) => typeof e.state?.power === "boolean");
  if (!powered.length) return `${eps.length} ${eps.length === 1 ? "device" : "devices"}`;
  const on = powered.filter((e) => e.state?.power === true).length;
  return `${on} on · ${powered.length - on} off`;
}

export function Devices() {
  const { endpoints, loadError, ws } = useLive();
  const rooms = useMemo(() => {
    const set = new Set<string>();
    for (const e of endpoints ?? []) set.add(e.room);
    return [...set].sort((a, b) => a.localeCompare(b));
  }, [endpoints]);
  const [room, setRoom] = useState<string | null>(null);
  const activeRoom = room && rooms.includes(room) ? room : rooms[0] ?? null;
  const roomEps = useMemo(() => (endpoints ?? []).filter((e) => e.room === activeRoom), [endpoints, activeRoom]);
  const others = rooms.filter((r) => r !== activeRoom);
  // When the hub itself is unreachable, show one banner and treat every device as offline with its last known state.
  const hubDown = ws === "reconnecting" || ws === "closed";

  return (
    <section>
      <h2>Devices <span className={`ws ws-${ws}`} aria-label={`live connection ${ws}`}>{ws === "open" ? "live" : ws}</span></h2>
      {loadError && <p className="err" role="alert">{loadError}</p>}
      {endpoints === null && !loadError && (
        <div className="grid" aria-busy="true" aria-label="Loading devices">
          {[0, 1, 2].map((i) => <div key={i} className="card skeleton" aria-hidden="true"><span className="sk sk-title" /><span className="sk sk-line" /><span className="sk sk-line" /></div>)}
        </div>
      )}
      {endpoints?.length === 0 && (
        <div className="empty">
          <p>{session.role === "guest" ? "No devices visible to you." : "No devices yet. Add one in Admin."}</p>
        </div>
      )}
      {hubDown && (
        <div className={`banner ${ws === "closed" ? "warn" : "info"}`} role="status">
          {ws === "closed" ? "Hub unreachable. Showing the last known state for every device." : "Reconnecting to the hub. Showing the last known state for every device."}
        </div>
      )}
      {rooms.length > 0 && (
        <div className="devices-layout">
          <nav className="room-tabs segmented" aria-label="Rooms">
            {rooms.map((r) => (
              <button key={r} aria-pressed={r === activeRoom} onClick={() => setRoom(r)}>{r}</button>
            ))}
          </nav>
          <div className="devices-main">
            {activeRoom && (
              <div>
                <h3 className="room-name">{activeRoom}</h3>
                <ReadingsStrip eps={roomEps} />
                <div className="grid">
                  {roomEps.map((e) => (e.type === "lock" ? <LockCard key={e.endpoint_id} ep={e} hubDown={hubDown} /> : <EndpointCard key={e.endpoint_id} ep={e} hubDown={hubDown} />))}
                </div>
              </div>
            )}
          </div>
          {others.length > 0 && (
            <aside className="rail" aria-label="Around the house">
              <h3>Around the house</h3>
              {others.map((r) => (
                <button key={r} type="button" className="rail-room" onClick={() => setRoom(r)}>
                  <span className="room-label">{r}</span>
                  <span className="muted small">{roomSummary((endpoints ?? []).filter((e) => e.room === r))}</span>
                </button>
              ))}
            </aside>
          )}
        </div>
      )}
    </section>
  );
}
