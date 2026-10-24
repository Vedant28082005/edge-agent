import { useCallback, useEffect, useState } from "react";
import { api, ApiError, session } from "../api/client";
import type {
  AutomationAction, AutomationClause, AutomationKillSwitch, AutomationOutcome, AutomationRule, AutomationRuleInput, AutomationRun,
  AutomationRunPage, AutomationTrigger, Capability, Endpoint, Weekday,
} from "../api/types";
import { explain } from "../errors";
import { useLive } from "../live";
import { capLabel } from "./Devices";

type Scalar = boolean | number | string;
const DAYS: Weekday[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const OPS = { eq: "is", ne: "is not", gt: "is above", ge: "is at least", lt: "is below", le: "is at most" } as const;
type Op = keyof typeof OPS;

// The server's own message (e.g. which rule field is wrong) wins; otherwise the generic explanation.
function explainRule(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { message?: string } | undefined;
    if (body?.message) return body.message;
  }
  return explain(e);
}

const dur = (s: number) => (s % 3600 === 0 ? `${s / 3600} h` : s % 60 === 0 ? `${s / 60} min` : `${s} s`);
const val = (v: Scalar) => (typeof v === "boolean" ? (v ? "on" : "off") : String(v));

type Names = (endpointId: string) => string;
// Endpoints are shown by name only. One the caller can no longer see is described in words, never by its id.
const makeNames = (eps: Endpoint[] | null): Names => (id) => eps?.find((x) => x.endpoint_id === id)?.name ?? "a device you can't see";

function describeTrigger(t: AutomationTrigger, n: Names): string {
  switch (t.type) {
    case "device_state_change":
      return `${n(t.endpoint_id)} ${capLabel(t.capability).toLowerCase()} changes${t.from !== undefined ? ` from ${val(t.from)}` : ""}${t.to !== undefined ? ` to ${val(t.to)}` : ""}`;
    case "device_offline": return `${n(t.endpoint_id)} is offline for ${dur(t.for_s ?? 60)}`;
    case "duration_in_state": return `${n(t.endpoint_id)} ${capLabel(t.capability).toLowerCase()} stays ${val(t.value)} for ${dur(t.for_s)}`;
    case "time_of_day": return `At ${t.at}${t.days ? ` on ${t.days.join(", ")}` : " every day"}`;
    case "schedule":
      return `Every ${dur(t.every_s)}${t.between ? ` between ${t.between.from} and ${t.between.to}` : ""}${t.days ? ` on ${t.days.join(", ")}` : ""}`;
  }
}
function describeClause(c: AutomationClause, n: Names): string {
  return c.type === "device_state"
    ? `${n(c.endpoint_id)} ${capLabel(c.capability).toLowerCase()} ${OPS[c.op]} ${val(c.value)}`
    : `the time is between ${c.after} and ${c.before}${c.days ? ` on ${c.days.join(", ")}` : ""}`;
}
function describeAction(a: AutomationAction, n: Names): string {
  return a.type === "notify"
    ? `Send a ${a.level ?? "info"} notification: "${a.text}"`
    : `Set ${n(a.endpoint_id)} ${capLabel(a.capability).toLowerCase()} to ${val(a.value)}`;
}

const OUTCOME: Record<AutomationOutcome, string> = {
  fired: "Ran", skipped_condition: "Skipped: condition not met", skipped_cooldown: "Skipped: cooling down", skipped_daily_limit: "Skipped: daily limit reached",
  skipped_offline: "Skipped: device offline", blocked_confirm: "Blocked: needs your confirmation", blocked_scope: "Blocked: no access to the room",
  failed: "Failed", suppressed_kill_switch: "Not run: automations paused",
};
const OUTCOME_TONE: Partial<Record<AutomationOutcome, string>> = { fired: "ok", failed: "warn", blocked_confirm: "warn", blocked_scope: "warn" };

function Runs({ ruleId }: { ruleId: string }) {
  const [items, setItems] = useState<AutomationRun[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async (cursor?: string) => {
    try {
      const q = new URLSearchParams({ limit: "20" });
      if (cursor) q.set("cursor", cursor);
      const page = await api<AutomationRunPage>("GET", `/automations/${ruleId}/runs?${q}`);
      setItems((cur) => (cursor && cur ? [...cur, ...page.items] : page.items));
      setNext(page.next_cursor);
      setErr(null);
    } catch (e) { setErr(explain(e)); }
  }, [ruleId]);
  useEffect(() => { void load(); }, [load]);
  if (err) return <p className="err" role="alert">{err}</p>;
  if (!items) return <p className="muted small">Loading history…</p>;
  if (items.length === 0) return <p className="muted small">No runs in the last 30 days.</p>;
  return (
    <div>
      <ul className="plain runs" aria-label="Run history">
        {items.map((r) => (
          <li key={r.run_id}>
            <span className={`badge ${OUTCOME_TONE[r.outcome] ?? "off"}`}>{OUTCOME[r.outcome]}</span>
            {r.action_status && <span className="muted small">result: {r.action_status.replace(/_/g, " ")}</span>}
            {r.error && <span className="muted small">{explain(r.error)}</span>}
            <time className="muted small">{new Date(r.ts * 1000).toLocaleString()}</time>
          </li>
        ))}
      </ul>
      {next && <button className="sm" onClick={() => void load(next)}>Load more</button>}
    </div>
  );
}

function RuleCard({ rule, names, onChanged }: { rule: AutomationRule; names: Names; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [history, setHistory] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const act = async (method: string, path: string) => {
    setBusy(true);
    try { await api(method, path); setErr(null); setConfirmDelete(false); onChanged(); } catch (e) { setErr(explainRule(e)); } finally { setBusy(false); }
  };
  const base = `/automations/${rule.rule_id}`;
  return (
    <li className="card rule">
      <header>
        <h3>{rule.name}</h3>
        <span className={`badge ${rule.status === "approved" ? "ok" : rule.status === "proposed" ? "info" : "off"}`}>
          {rule.status === "approved" ? "On" : rule.status === "proposed" ? "Needs approval" : "Off"}
        </span>
        {rule.created_by === "llm" && <span className="badge view-only">Suggested by the assistant</span>}
        {rule.broken && <span className="badge warn">Needs fixing</span>}
      </header>
      <p className="rule-line"><strong>When</strong> {describeTrigger(rule.trigger, names)}</p>
      {rule.condition && <p className="rule-line"><strong>Only if</strong> {rule.condition.all.map((c) => describeClause(c, names)).join(" and ")}</p>}
      <p className="rule-line"><strong>Then</strong> {describeAction(rule.action, names)}</p>
      <p className="muted small">
        At most once every {dur(rule.cooldown_s)}, {rule.max_runs_per_day} a day
        {rule.rooms && rule.rooms.length > 0 ? ` · ${rule.rooms.join(", ")}` : ""}
      </p>
      {rule.broken && <p className="muted small">A device this rule uses was removed or changed, so it will not run. Delete it and create a new one.</p>}
      <div className="row">
        {rule.status !== "approved" && !rule.broken && <button className="primary sm" disabled={busy} onClick={() => void act("POST", `${base}/approve`)}>Approve</button>}
        {rule.status === "approved" && <button className="sm" disabled={busy} onClick={() => void act("POST", `${base}/disable`)}>Disable</button>}
        {rule.status === "proposed" && <button className="sm" disabled={busy} onClick={() => void act("POST", `${base}/disable`)}>Keep off</button>}
        <button className="sm" aria-expanded={history} onClick={() => setHistory((h) => !h)}>{history ? "Hide history" : "History"}</button>
        {!confirmDelete && <button className="danger sm" disabled={busy} onClick={() => setConfirmDelete(true)}>Delete…</button>}
      </div>
      {confirmDelete && (
        <div className="row" role="group" aria-label={`Delete ${rule.name}`}>
          <span>Delete this rule?</span>
          <button className="sm" onClick={() => setConfirmDelete(false)}>Cancel</button>
          <button className="danger sm" disabled={busy} onClick={() => void act("DELETE", base)}>Delete rule</button>
        </div>
      )}
      {err && <p className="err" role="alert">{err}</p>}
      {history && <Runs ruleId={rule.rule_id} />}
    </li>
  );
}

// ---------------------------------------------------------------- create form
const epLabel = (e: Endpoint) => `${e.name} (${e.room})`;

function ValueInput({ cap, value, onChange, label, optional }: { cap?: Capability; value: string; onChange: (v: string) => void; label: string; optional?: boolean }) {
  const blank = optional ? <option value="">any</option> : null;
  if (cap?.type === "bool") return <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>{blank}<option value="true">on</option><option value="false">off</option></select>;
  if (cap?.type === "enum") return <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>{blank}{(cap.values ?? []).map((v) => <option key={v} value={v}>{v}</option>)}</select>;
  if (cap?.type === "int" || cap?.type === "float") return <input aria-label={label} type="number" min={cap.min} max={cap.max} step={cap.type === "int" ? 1 : "any"} value={value} onChange={(e) => onChange(e.target.value)} />;
  return <input aria-label={label} maxLength={64} value={value} onChange={(e) => onChange(e.target.value)} />;
}
function toScalar(cap: Capability | undefined, raw: string): Scalar {
  if (cap?.type === "bool") return raw === "true";
  if (cap?.type === "int" || cap?.type === "float") return Number(raw);
  return raw;
}
// Default for a required value input so it is never empty.
const defaultValue = (cap?: Capability) => (cap?.type === "bool" ? "true" : cap?.type === "enum" ? cap.values?.[0] ?? "" : "");

type Pick = { ep: string; cap: string };
function EndpointCapPick({ eps, value, onChange, label, actuatorsOnly }: { eps: Endpoint[]; value: Pick; onChange: (p: Pick) => void; label: string; actuatorsOnly?: boolean }) {
  const e = eps.find((x) => x.endpoint_id === value.ep);
  const caps = (e?.capabilities ?? []).filter((c) => !actuatorsOnly || c.kind === "actuator");
  return (
    <>
      <select aria-label={`${label} device`} value={value.ep} onChange={(ev) => {
        const next = eps.find((x) => x.endpoint_id === ev.target.value);
        const first = (next?.capabilities ?? []).find((c) => !actuatorsOnly || c.kind === "actuator");
        onChange({ ep: ev.target.value, cap: first?.id ?? "" });
      }}>
        <option value="">Choose a device</option>
        {eps.map((x) => <option key={x.endpoint_id} value={x.endpoint_id}>{epLabel(x)}</option>)}
      </select>
      <select aria-label={`${label} property`} value={value.cap} onChange={(ev) => onChange({ ...value, cap: ev.target.value })}>
        {caps.map((c) => <option key={c.id} value={c.id}>{capLabel(c.id)}</option>)}
      </select>
    </>
  );
}

function DayChecks({ value, onChange, label }: { value: Weekday[]; onChange: (d: Weekday[]) => void; label: string }) {
  return (
    <div className="checklist" role="group" aria-label={label}>
      {DAYS.map((d) => (
        <label key={d}><input type="checkbox" aria-label={d} checked={value.includes(d)} onChange={(e) => onChange(e.target.checked ? [...value, d] : value.filter((x) => x !== d))} />{d}</label>
      ))}
    </div>
  );
}

type TType = AutomationTrigger["type"];
const TRIGGER_LABEL: Record<TType, string> = {
  device_state_change: "A device changes", device_offline: "A device goes offline", duration_in_state: "A device stays in a state",
  time_of_day: "At a time of day", schedule: "On a repeating schedule",
};
type ClauseDraft = { kind: "device_state" | "time_window"; pick: Pick; op: Op; value: string; after: string; before: string };
const newClause = (): ClauseDraft => ({ kind: "device_state", pick: { ep: "", cap: "" }, op: "eq", value: "", after: "22:00", before: "06:00" });

function CreateForm({ eps, onDone }: { eps: Endpoint[]; onDone: () => void }) {
  const capOf = (p: Pick) => eps.find((e) => e.endpoint_id === p.ep)?.capabilities.find((c) => c.id === p.cap);
  const [name, setName] = useState("");
  const [tType, setTType] = useState<TType>("device_state_change");
  const [tPick, setTPick] = useState<Pick>({ ep: "", cap: "" });
  const [tTo, setTTo] = useState("");
  const [tFrom, setTFrom] = useState("");
  const [tValue, setTValue] = useState("");
  const [tFor, setTFor] = useState("60");
  const [tAt, setTAt] = useState("07:00");
  const [tDays, setTDays] = useState<Weekday[]>([]);
  const [tEvery, setTEvery] = useState("15"); // minutes
  const [tBetween, setTBetween] = useState(false);
  const [bFrom, setBFrom] = useState("08:00");
  const [bTo, setBTo] = useState("20:00");
  const [clauses, setClauses] = useState<ClauseDraft[]>([]);
  const [aType, setAType] = useState<AutomationAction["type"]>("notify");
  const [aLevel, setALevel] = useState<"info" | "warning">("info");
  const [aText, setAText] = useState("");
  const [aPick, setAPick] = useState<Pick>({ ep: "", cap: "" });
  const [aValue, setAValue] = useState("");
  const [cooldown, setCooldown] = useState("60");
  const [maxRuns, setMaxRuns] = useState("20");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // An automation can never drive a device that needs confirmation (locks), so those are not offered as targets.
  const actionEps = eps.filter((e) => e.risk !== "confirm" && e.capabilities.some((c) => c.kind === "actuator"));
  const setClause = (i: number, patch: Partial<ClauseDraft>) => setClauses((cs) => cs.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const needsDevice = tType === "device_state_change" || tType === "device_offline" || tType === "duration_in_state";
  const days = (d: Weekday[]) => (d.length > 0 ? { days: DAYS.filter((x) => d.includes(x)) } : {});

  const buildTrigger = (): AutomationTrigger | string => {
    const cap = capOf(tPick);
    if (needsDevice && !tPick.ep) return "Choose the device for the trigger.";
    switch (tType) {
      case "device_state_change":
        return { type: tType, endpoint_id: tPick.ep, capability: tPick.cap, ...(tTo !== "" ? { to: toScalar(cap, tTo) } : {}), ...(tFrom !== "" ? { from: toScalar(cap, tFrom) } : {}) };
      case "device_offline": return { type: tType, endpoint_id: tPick.ep, for_s: Number(tFor) };
      case "duration_in_state":
        if (tValue === "") return "Enter the state the device must stay in.";
        return { type: tType, endpoint_id: tPick.ep, capability: tPick.cap, value: toScalar(cap, tValue), for_s: Number(tFor) };
      case "time_of_day": return { type: tType, at: tAt, ...days(tDays) };
      case "schedule": return { type: tType, every_s: Number(tEvery) * 60, ...(tBetween ? { between: { from: bFrom, to: bTo } } : {}), ...days(tDays) };
    }
  };
  const buildClause = (c: ClauseDraft): AutomationClause | string => {
    if (c.kind === "time_window") return { type: "time_window", after: c.after, before: c.before };
    if (!c.pick.ep || c.value === "") return "Finish each condition or remove it.";
    return { type: "device_state", endpoint_id: c.pick.ep, capability: c.pick.cap, op: c.op, value: toScalar(capOf(c.pick), c.value) };
  };
  const buildAction = (): AutomationAction | string => {
    if (aType === "notify") return aText.trim() ? { type: "notify", level: aLevel, text: aText.trim() } : "Write the notification text.";
    if (!aPick.ep || aValue === "") return "Choose the device and value to set.";
    return { type: "set_state", endpoint_id: aPick.ep, capability: aPick.cap, value: toScalar(capOf(aPick), aValue) };
  };

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    const trigger = buildTrigger();
    const action = buildAction();
    const built = clauses.map(buildClause);
    const bad = [trigger, action, ...built].find((x): x is string => typeof x === "string");
    if (bad) { setErr(bad); return; }
    const body: AutomationRuleInput = {
      name: name.trim(), source: "user", trigger: trigger as AutomationTrigger, action: action as AutomationAction,
      cooldown_s: Number(cooldown), max_runs_per_day: Number(maxRuns),
      ...(built.length > 0 ? { condition: { all: built as AutomationClause[] } } : {}),
    };
    setBusy(true);
    try { await api("POST", "/automations", body); setErr(null); onDone(); } catch (e) { setErr(explainRule(e)); } finally { setBusy(false); }
  };

  return (
    <form className="stack card" onSubmit={submit} aria-label="New rule">
      <h3>New rule</h3>
      <label>Name<input required maxLength={48} value={name} onChange={(e) => setName(e.target.value)} /></label>

      <fieldset>
        <legend>When</legend>
        <div className="row wrap">
          <select aria-label="Trigger type" value={tType} onChange={(e) => setTType(e.target.value as TType)}>
            {(Object.keys(TRIGGER_LABEL) as TType[]).map((t) => <option key={t} value={t}>{TRIGGER_LABEL[t]}</option>)}
          </select>
          {needsDevice && <EndpointCapPick eps={eps} value={tPick} onChange={(p) => { setTPick(p); setTTo(""); setTFrom(""); setTValue(defaultValue(capOf(p))); }} label="Trigger" />}
          {tType === "device_state_change" && (
            <>
              <ValueInput cap={capOf(tPick)} value={tFrom} onChange={setTFrom} label="From value" optional />
              <ValueInput cap={capOf(tPick)} value={tTo} onChange={setTTo} label="To value" optional />
            </>
          )}
          {tType === "duration_in_state" && <ValueInput cap={capOf(tPick)} value={tValue} onChange={setTValue} label="Stays at value" />}
          {(tType === "device_offline" || tType === "duration_in_state") && (
            <label className="inline">For (seconds)<input type="number" min={tType === "duration_in_state" ? 10 : 0} max={86400} value={tFor} onChange={(e) => setTFor(e.target.value)} /></label>
          )}
          {tType === "time_of_day" && <label className="inline">At<input type="time" value={tAt} onChange={(e) => setTAt(e.target.value)} /></label>}
          {tType === "schedule" && (
            <>
              <label className="inline">Every (minutes)<input type="number" min={1} max={1440} value={tEvery} onChange={(e) => setTEvery(e.target.value)} /></label>
              <label className="inline"><input type="checkbox" checked={tBetween} onChange={(e) => setTBetween(e.target.checked)} />Only between</label>
              {tBetween && (
                <>
                  <input aria-label="Between from" type="time" value={bFrom} onChange={(e) => setBFrom(e.target.value)} />
                  <input aria-label="Between to" type="time" value={bTo} onChange={(e) => setBTo(e.target.value)} />
                </>
              )}
            </>
          )}
        </div>
        {(tType === "time_of_day" || tType === "schedule") && <DayChecks value={tDays} onChange={setTDays} label="Trigger days (none means every day)" />}
      </fieldset>

      <fieldset>
        <legend>Only if (optional, up to 4, all must hold)</legend>
        {clauses.map((c, i) => (
          <div className="row wrap" key={i}>
            <select aria-label={`Condition ${i + 1} type`} value={c.kind} onChange={(e) => setClause(i, { kind: e.target.value as ClauseDraft["kind"] })}>
              <option value="device_state">A device state</option>
              <option value="time_window">The time of day</option>
            </select>
            {c.kind === "device_state" ? (
              <>
                <EndpointCapPick eps={eps} value={c.pick} label={`Condition ${i + 1}`} onChange={(p) => setClause(i, { pick: p, value: defaultValue(capOf(p)) })} />
                <select aria-label={`Condition ${i + 1} comparison`} value={c.op} onChange={(e) => setClause(i, { op: e.target.value as Op })}>
                  {(Object.keys(OPS) as Op[]).map((o) => <option key={o} value={o}>{OPS[o]}</option>)}
                </select>
                <ValueInput cap={capOf(c.pick)} value={c.value} onChange={(v) => setClause(i, { value: v })} label={`Condition ${i + 1} value`} />
              </>
            ) : (
              <>
                <input aria-label={`Condition ${i + 1} after`} type="time" value={c.after} onChange={(e) => setClause(i, { after: e.target.value })} />
                <input aria-label={`Condition ${i + 1} before`} type="time" value={c.before} onChange={(e) => setClause(i, { before: e.target.value })} />
              </>
            )}
            <button type="button" className="sm" aria-label={`Remove condition ${i + 1}`} onClick={() => setClauses((cs) => cs.filter((_, j) => j !== i))}>Remove</button>
          </div>
        ))}
        {clauses.length < 4 && <button type="button" className="sm" onClick={() => setClauses((cs) => [...cs, newClause()])}>Add condition</button>}
      </fieldset>

      <fieldset>
        <legend>Then</legend>
        <div className="row wrap">
          <select aria-label="Action type" value={aType} onChange={(e) => setAType(e.target.value as AutomationAction["type"])}>
            <option value="notify">Send a notification</option>
            <option value="set_state">Set a device</option>
          </select>
          {aType === "notify" ? (
            <>
              <select aria-label="Notification level" value={aLevel} onChange={(e) => setALevel(e.target.value as "info" | "warning")}>
                <option value="info">Info</option><option value="warning">Warning</option>
              </select>
              <input aria-label="Notification text" required maxLength={120} placeholder="Text, up to 120 characters" value={aText} onChange={(e) => setAText(e.target.value)} />
            </>
          ) : (
            <>
              <EndpointCapPick eps={actionEps} value={aPick} label="Action" actuatorsOnly onChange={(p) => { setAPick(p); setAValue(defaultValue(capOf(p))); }} />
              <ValueInput cap={capOf(aPick)} value={aValue} onChange={setAValue} label="Action value" />
            </>
          )}
        </div>
      </fieldset>

      <div className="row wrap">
        <label className="inline">Cooldown (seconds)<input type="number" min={10} max={86400} required value={cooldown} onChange={(e) => setCooldown(e.target.value)} /></label>
        <label className="inline">Max runs per day<input type="number" min={1} max={1000} required value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} /></label>
      </div>
      <p className="muted small">Saving turns the rule on. Editing it later switches it off until you approve it again.</p>
      {err && <p className="err" role="alert">{err}</p>}
      <div className="row"><button className="primary" disabled={busy}>Save rule</button></div>
    </form>
  );
}

// ---------------------------------------------------------------- kill switch (admin only)
function KillSwitch() {
  const { autoEnabled, setAutoEnabled } = useLive();
  const [confirming, setConfirming] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api<AutomationKillSwitch>("GET", "/admin/automations/kill-switch").then((k) => setAutoEnabled(k.automations_enabled)).catch((e) => setErr(explain(e)));
  }, [setAutoEnabled]);
  const set = async (enabled: boolean) => {
    try {
      const k = await api<AutomationKillSwitch>("PUT", "/admin/automations/kill-switch", { automations_enabled: enabled });
      setAutoEnabled(k.automations_enabled); setConfirming(false); setErr(null);
    } catch (e) { setErr(explain(e)); }
  };
  if (autoEnabled === null) return err ? <p className="err" role="alert">{err}</p> : null;
  return (
    <section className="card kill" aria-label="Automation kill switch">
      <div className="row">
        <h3>All automations</h3>
        <span className={`badge ${autoEnabled ? "ok" : "warn"}`}>{autoEnabled ? "Running" : "Paused"}</span>
        <span className="spacer" />
        {autoEnabled && !confirming && <button className="risk-outline" onClick={() => setConfirming(true)}>Pause all…</button>}
        {!autoEnabled && <button className="primary" onClick={() => void set(true)}>Resume all</button>}
      </div>
      {autoEnabled && confirming && (
        <div className="row" role="group" aria-label="Confirm pause">
          <span>Stop every rule from running, including notifications?</span>
          <button onClick={() => setConfirming(false)}>Cancel</button>
          <button className="danger" onClick={() => void set(false)}>Pause all</button>
        </div>
      )}
      {err && <p className="err" role="alert">{err}</p>}
    </section>
  );
}

// ---------------------------------------------------------------- view
export function Automations() {
  const { endpoints, autoRev, autoEnabled } = useLive();
  const isAdmin = session.role === "admin"; // cosmetic: the API enforces roles
  const [rules, setRules] = useState<AutomationRule[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const names = makeNames(endpoints);

  const load = useCallback(async () => {
    try { setRules(await api<AutomationRule[]>("GET", "/automations")); setErr(null); } catch (e) { setErr(explain(e)); }
  }, []);
  useEffect(() => { void load(); }, [load, autoRev]); // autoRev: refetch on `automations_changed`

  const proposed = (rules ?? []).filter((r) => r.status === "proposed");
  const others = (rules ?? []).filter((r) => r.status !== "proposed");
  return (
    <section>
      <h2>Automations</h2>
      {isAdmin && <KillSwitch />}
      {autoEnabled === false && <div className="banner warn" role="status"><span>Automations are paused. No rule will run until they are resumed.</span></div>}
      {err && <p className="err" role="alert">{err}</p>}

      {proposed.length > 0 && (
        <div className="auto-block" role="region" aria-label="Proposed rules">
          <h3 className="section-title">Proposed</h3>
          <p className="muted small">These rules do nothing until you approve them. Read each one first.</p>
          <ul className="plain stack">{proposed.map((r) => <RuleCard key={r.rule_id} rule={r} names={names} onChanged={load} />)}</ul>
        </div>
      )}

      <div className="auto-block">
        <div className="row">
          <h3 className="section-title">Rules</h3>
          <span className="spacer" />
          {!creating && <button className="primary sm" onClick={() => setCreating(true)}>New rule</button>}
        </div>
        {creating && endpoints && <CreateForm eps={endpoints} onDone={() => { setCreating(false); void load(); }} />}
        {creating && <div className="row"><button className="sm" onClick={() => setCreating(false)}>Cancel</button></div>}
        {rules && others.length === 0 && !creating && (
          <div className="empty"><p>No rules yet.</p><p className="small">A rule does one thing when something happens, like a notification when a door has been open too long.</p></div>
        )}
        <ul className="plain stack">{others.map((r) => <RuleCard key={r.rule_id} rule={r} names={names} onChanged={load} />)}</ul>
      </div>
    </section>
  );
}
