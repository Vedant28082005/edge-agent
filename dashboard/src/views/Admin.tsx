import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ActionLogEntry, ActionLogPage, ActionStatus, Endpoint, ForgetUsage, Node, Role, User } from "../api/types";
import { explain } from "../errors";
import { useToast } from "../toast";

// The approve form shows the server's own error message verbatim when it sent one (e.g. a name
// collision), falling back to the generic explanation only when it didn't.
function explainApprove(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { message?: string } | undefined;
    if (body?.message) return body.message;
  }
  return explain(e);
}

const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const ROLES: Role[] = ["admin", "member", "guest"];
const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// Role segmented control: guest = rooms-only + view-only (gateway.yaml Role enum: admin, member, guest).
function RoleSegmented({ value, onChange, label }: { value: Role; onChange: (r: Role) => void; label: string }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {ROLES.map((r) => (
        <button key={r} type="button" aria-pressed={value === r} onClick={() => onChange(r)}>{r}</button>
      ))}
    </div>
  );
}

// Room checklist, populated from the rooms currently in use (the contract has no standalone "list of rooms"
// endpoint; rooms only exist as a free-form field on endpoints and users). A room with no devices yet cannot
// be offered here until an admin places a device in it.
function RoomChecklist({ rooms, selected, onChange, label }: { rooms: string[]; selected: string[]; onChange: (next: string[]) => void; label: string }) {
  if (rooms.length === 0) return <span className="muted small">No rooms yet</span>;
  return (
    <div className="checklist" role="group" aria-label={label}>
      {rooms.map((r) => (
        <label key={r}>
          <input
            type="checkbox"
            aria-label={r}
            checked={selected.includes(r)}
            onChange={(e) => onChange(e.target.checked ? [...selected, r] : selected.filter((x) => x !== r))}
          />
          {r}
        </label>
      ))}
    </div>
  );
}

function useLoad<T>(path: string) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const toast = useToast();
  const reload = useCallback(async () => {
    try { setData(await api<T>("GET", path)); setErr(null); } catch (e) { const m = explain(e); setErr(m); toast(m); }
  }, [path, toast]);
  useEffect(() => { void reload(); }, [reload]);
  return { data, err, reload };
}

function ApproveForm({ node, rooms, onDone }: { node: Node; rooms: string[]; onDone: () => void }) {
  // A pending node has no approved endpoints yet: what it announced is in pending_endpoints.
  const eps = node.pending_endpoints ?? [];
  const [rows, setRows] = useState(() => eps.map((e) => ({ local_id: e.local_id ?? "", name: e.type ? capitalize(e.type) : "", room: "" })));
  const [err, setErr] = useState<string | null>(null);
  const set = (i: number, k: "name" | "room", v: string) => setRows((r) => r.map((x, j) => (j === i ? { ...x, [k]: v } : x)));
  const valid = rows.every((r) => r.name.trim() && r.room.trim());
  const datalistId = `rooms-${node.node_id}`;
  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    try {
      await api("POST", `/admin/nodes/${node.node_id}/approve`, {
        endpoints: rows.map((r) => ({ local_id: r.local_id, name: r.name, room: r.room })),
      });
      onDone();
    } catch (e) { setErr(explainApprove(e)); }
  };
  return (
    <form onSubmit={submit} className="stack">
      <datalist id={datalistId}>{rooms.map((r) => <option key={r} value={r} />)}</datalist>
      {rows.map((r, i) => (
        <fieldset key={r.local_id || i}>
          <legend>{eps[i]?.type ? capitalize(eps[i].type) : "Endpoint"}{eps.length > 1 ? ` ${i + 1}` : ""}</legend>
          <label>Name <input required maxLength={48} value={r.name} onChange={(e) => set(i, "name", e.target.value)} /></label>
          <label>Room <input required maxLength={48} list={datalistId} value={r.room} onChange={(e) => set(i, "room", e.target.value)} /></label>
        </fieldset>
      ))}
      {err && <p className="err" role="alert">{err}</p>}
      <button className="primary" disabled={!valid}>Approve</button>
    </form>
  );
}

function NodeRow({ node, rooms, reload }: { node: Node; rooms: string[]; reload: () => void }) {
  const [open, setOpen] = useState(false);
  const [usage, setUsage] = useState<ForgetUsage | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const toast = useToast();
  const pending = node.status === "pending";
  // Approved nodes are named after what they do; the raw node id is only shown while a node is still pending.
  const names = (node.endpoints ?? []).map((e) => e.name).filter((n): n is string => !!n);
  const title = pending ? null : names.length ? names.slice(0, 2).join(", ") + (names.length > 2 ? ` +${names.length - 2}` : "") : "Unnamed node";

  const forget = async (confirm: boolean) => {
    setErr(null);
    try {
      await api("DELETE", `/admin/nodes/${node.node_id}${confirm ? "?confirm=true" : ""}`);
      setUsage(null); setConfirming(false); reload();
    } catch (e) {
      if ((e as { status?: number }).status === 409 && !confirm) {
        // The 409 body is the usage list ("2 scenes, 1 automation").
        setUsage(((e as { body?: ForgetUsage }).body) ?? {});
      } else { const m = explain(e); setErr(m); toast(m); }
    }
  };

  return (
    <li className="card">
      <header>
        <h3 className="node-title">{title ?? <code>{node.node_id}</code>}</h3>
        <span className={`badge ${node.status === "online" ? "ok" : node.status === "pending" ? "warn" : "off"}`}>{node.status}</span>
        <span className="muted small">{node.hw} {node.fw}</span>
      </header>
      {(node.endpoints ?? []).map((e: Endpoint) => <EndpointEdit key={e.endpoint_id ?? e.local_id} ep={e} reload={reload} pending={node.status === "pending"} />)}
      <div className="row">
        {pending && <button className="primary" onClick={() => setOpen(!open)}>{open ? "Hide" : "Approve…"}</button>}
        <button className="secondary" onClick={() => api("POST", `/admin/nodes/${node.node_id}/identify`, {}).catch((e) => { const m = explain(e); setErr(m); toast(m); })}>Identify</button>
        <button className="danger" onClick={() => setConfirming(true)} disabled={confirming}>Forget</button>
      </div>
      {open && node.status === "pending" && <ApproveForm node={node} rooms={rooms} onDone={() => { setOpen(false); reload(); }} />}
      {confirming && (
        <div className="conf" role="alertdialog" aria-label="Forget node">
          {usage ? (
            <>
              <p>{usage.message ?? `Used by ${usage.scenes ?? 0} scenes and ${usage.automations ?? 0} automations.`} Forgetting revokes the node&apos;s MQTT access.</p>
              <div className="row"><button className="danger" onClick={() => forget(true)}>Forget anyway</button><button onClick={() => { setUsage(null); setConfirming(false); }}>Cancel</button></div>
            </>
          ) : (
            <>
              <p>Forget {title ?? "this node"}? It is removed and loses its network access.</p>
              <div className="row"><button className="danger" onClick={() => forget(false)}>Yes, forget</button><button onClick={() => setConfirming(false)}>Cancel</button></div>
            </>
          )}
        </div>
      )}
      {err && <p className="err" role="alert">{err}</p>}
    </li>
  );
}

function EndpointEdit({ ep, reload, pending }: { ep: Endpoint; reload: () => void; pending: boolean }) {
  const [edit, setEdit] = useState(false);
  const [f, setF] = useState({ name: ep.name ?? "", room: ep.room ?? "", tags: (ep.tags ?? []).join(","), risk: ep.risk ?? "normal", enabled: ep.enabled ?? true });
  const [err, setErr] = useState<string | null>(null);
  if (pending || !ep.endpoint_id) return null;
  const save = async () => {
    try {
      await api("PATCH", `/admin/endpoints/${ep.endpoint_id}`, { name: f.name, room: f.room, tags: csv(f.tags), risk: f.risk, enabled: f.enabled });
      setEdit(false); reload();
    } catch (e) { setErr(explain(e)); }
  };
  return (
    <div className="ep-line">
      <strong>{ep.name}</strong> <span className="muted small">{ep.room}</span>
      <button className="secondary sm" onClick={() => setEdit(!edit)}>{edit ? "Close" : "Edit"}</button>
      {edit && (
        <div className="stack">
          <label>Name <input value={f.name} maxLength={48} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          <label>Room <input value={f.room} maxLength={48} onChange={(e) => setF({ ...f, room: e.target.value })} /></label>
          <label>Tags <input value={f.tags} onChange={(e) => setF({ ...f, tags: e.target.value })} /></label>
          <label>Risk <select value={f.risk} onChange={(e) => setF({ ...f, risk: e.target.value as "normal" | "confirm" })}><option value="normal">normal</option><option value="confirm">confirm</option></select></label>
          <label><input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> Enabled</label>
          {err && <p className="err" role="alert">{err}</p>}
          <button className="primary" onClick={save}>Save</button>
        </div>
      )}
    </div>
  );
}

function Nodes({ rooms }: { rooms: string[] }) {
  const { data, err, reload } = useLoad<Node[]>("/admin/nodes");
  const sorted = [...(data ?? [])].sort((a, b) => Number(b.status === "pending") - Number(a.status === "pending"));
  return (
    <div className="admin-block">
      <h3>Nodes</h3>
      {err && <p className="err" role="alert">{err}</p>}
      {data === null && !err && <div className="cards" aria-busy="true" aria-label="Loading nodes"><div className="card skeleton" aria-hidden="true"><span className="sk sk-title" /><span className="sk sk-line" /></div></div>}
      <ul className="plain cards">{sorted.map((n) => <NodeRow key={n.node_id} node={n} rooms={rooms} reload={reload} />)}</ul>
      {data?.length === 0 && <div className="empty"><p>No nodes yet. New devices show up here when they join.</p></div>}
    </div>
  );
}

function UserRow({ u, rooms, reload }: { u: User; rooms: string[]; reload: () => void }) {
  const [role, setRole] = useState<Role>(u.role ?? "member");
  const [userRooms, setUserRooms] = useState<string[]>(u.rooms ?? []);
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    try { await api("PATCH", `/admin/users/${u.user_id}`, { role, rooms: role === "admin" ? [] : userRooms }); reload(); } catch (e) { setErr(explain(e)); }
  };
  return (
    <tr>
      <td>{u.username}{u.disabled ? " (disabled)" : ""}</td>
      <td><RoleSegmented value={role} onChange={setRole} label={`role of ${u.username}`} /></td>
      <td>
        {role === "admin"
          ? <span className="muted small">All rooms</span>
          : <RoomChecklist rooms={rooms} selected={userRooms} onChange={setUserRooms} label={`rooms of ${u.username}`} />}
      </td>
      <td className="row">
        <button className="primary sm" onClick={save}>Save</button>
        <button className="secondary sm" onClick={() => api("PATCH", `/admin/users/${u.user_id}`, { disabled: !u.disabled }).then(reload).catch((e) => setErr(explain(e)))}>{u.disabled ? "Enable" : "Disable"}</button>
        <button className="danger sm" onClick={() => api("DELETE", `/admin/users/${u.user_id}`).then(reload).catch((e) => setErr(explain(e)))}>Delete</button>
        {err && <span className="err" role="alert">{err}</span>}
      </td>
    </tr>
  );
}

function Users({ rooms, users: { data, err, reload } }: { rooms: string[]; users: ReturnType<typeof useLoad<User[]>> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [selRooms, setSelRooms] = useState<string[]>([]);
  const [cerr, setCerr] = useState<string | null>(null);
  const create = async (ev: React.FormEvent) => {
    ev.preventDefault();
    try {
      await api("POST", "/admin/users", { username, password, role, rooms: role === "admin" ? [] : selRooms });
      setUsername(""); setPassword(""); setRole("member"); setSelRooms([]); setCerr(null); reload();
    } catch (e) { setCerr(explain(e)); }
  };
  return (
    <div className="admin-block">
      <h3>Users</h3>
      {err && <p className="err" role="alert">{err}</p>}
      {data === null && !err && <div className="card skeleton" aria-busy="true" aria-label="Loading users"><span className="sk sk-title" /><span className="sk sk-line" /></div>}
      <div className="table-wrap"><table className="table users-table">
        <thead><tr><th>User</th><th>Role</th><th>Rooms (guest/member scope)</th><th /></tr></thead>
        <tbody>{(data ?? []).map((u) => <UserRow key={u.user_id} u={u} rooms={rooms} reload={reload} />)}</tbody>
      </table></div>
      <form onSubmit={create} className="stack" aria-label="Create user">
        <div className="row wrap">
          <input aria-label="New username" required maxLength={64} placeholder="username" value={username} onChange={(e) => setUsername(e.target.value)} />
          <input aria-label="New password" required minLength={8} maxLength={256} type="password" autoComplete="new-password" placeholder="password (min 8)" value={password} onChange={(e) => setPassword(e.target.value)} />
          <RoleSegmented value={role} onChange={setRole} label="New role" />
          <button className="primary">Create</button>
        </div>
        {role !== "admin" && <RoomChecklist rooms={rooms} selected={selRooms} onChange={setSelRooms} label="New rooms" />}
      </form>
      {cerr && <p className="err" role="alert">{cerr}</p>}
    </div>
  );
}

const STATUSES: ActionStatus[] = ["done", "moving", "settled", "settle_timeout", "failed", "blocked", "confirmation_required", "declined", "expired", "denied"];
const EMPTY_FILTERS = { endpoint_id: "", user_id: "", status: "", range: "" };
const RANGES: Array<{ value: string; label: string; seconds: number }> = [
  { value: "3600", label: "Last hour", seconds: 3600 },
  { value: "86400", label: "Last 24 hours", seconds: 86400 },
  { value: "604800", label: "Last 7 days", seconds: 604800 },
  { value: "2592000", label: "Last 30 days", seconds: 2592000 },
];
const PAGE_LIMIT = 50;


function logQuery(f: typeof EMPTY_FILTERS, cursor?: string): string {
  const q = new URLSearchParams();
  if (f.endpoint_id) q.set("endpoint_id", f.endpoint_id);
  if (f.user_id) q.set("user_id", f.user_id);
  if (f.status) q.set("status", f.status);
  if (f.range) q.set("from", String(Math.floor(Date.now() / 1000) - Number(f.range)));
  if (cursor) q.set("cursor", cursor);
  const qs = q.toString();
  return `/admin/action-log${qs ? `?${qs}` : ""}`;
}

// Plain names for the log, per handoff rule 6. The contract's ActionLogEntry.kind is only ever set_state or
// run_scene (chat, confirmations, node and user events are not rows here — the endpoint explicitly holds no
// agent/chat traces). Endpoint ids are resolved to the admin-set device name; a node id is never a plain name
// (it is MAC-derived) and the contract gives no friendly node label, so it is left out rather than shown raw.
function actionLabel(r: ActionLogEntry, endpointName: (id: string | null) => string | null): { kind: string; detail: string; risky: boolean } {
  if (r.kind === "run_scene") return { kind: "Run scene", detail: r.scene_id ?? "", risky: false };
  const name = endpointName(r.endpoint_id ?? null) ?? "device";
  const risky = r.capability === "lock";
  return { kind: "Device command", detail: `${name} · ${r.capability ?? ""} → ${r.value == null ? "" : String(r.value)}`, risky };
}

function ActionLog({ endpointName, endpoints, users }: { endpointName: (id: string | null) => string | null; endpoints: Endpoint[]; users: User[] }) {
  const [draft, setDraft] = useState(EMPTY_FILTERS);
  const [applied, setApplied] = useState(EMPTY_FILTERS);
  const [rows, setRows] = useState<ActionLogEntry[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (f: typeof EMPTY_FILTERS, cursor?: string) => {
    setBusy(true);
    try {
      const page = await api<ActionLogPage>("GET", logQuery(f, cursor));
      setRows((r) => (cursor ? [...r, ...page.items] : page.items));
      setNext(page.next_cursor);
      setErr(null);
    } catch (e) { setErr(explain(e)); } finally { setBusy(false); setLoaded(true); }
  }, []);
  useEffect(() => { void load(applied); }, [applied, load]);

  const set = (k: keyof typeof EMPTY_FILTERS, v: string) => setDraft((d) => ({ ...d, [k]: v }));
  const apply = (ev: React.FormEvent) => { ev.preventDefault(); setApplied(draft); };

  return (
    <div className="admin-block">
      <h3>Action log</h3>
      <form onSubmit={apply} className="log-filters" aria-label="Action log filters">
        <label className="field">Device
          <select aria-label="Filter device" value={draft.endpoint_id} onChange={(e) => set("endpoint_id", e.target.value)}>
            <option value="">Any device</option>
            {endpoints.map((e) => <option key={e.endpoint_id} value={e.endpoint_id}>{e.name}{e.room ? ` (${e.room})` : ""}</option>)}
          </select>
        </label>
        <label className="field">User
          <select aria-label="Filter user" value={draft.user_id} onChange={(e) => set("user_id", e.target.value)}>
            <option value="">Anyone</option>
            {users.filter((u) => u.user_id).map((u) => <option key={u.user_id} value={u.user_id}>{u.username}</option>)}
          </select>
        </label>
        <label className="field">Status
          <select aria-label="Filter status" value={draft.status} onChange={(e) => set("status", e.target.value)}>
            <option value="">Any status</option>
            {STATUSES.map((st) => <option key={st} value={st}>{st}</option>)}
          </select>
        </label>
        <label className="field">When
          <select aria-label="Filter time range" value={draft.range} onChange={(e) => set("range", e.target.value)}>
            <option value="">Any time</option>
            {RANGES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </label>
        <div className="btn-row">
          <button className="primary">Apply</button>
          <button type="button" className="secondary" onClick={() => { setDraft(EMPTY_FILTERS); setApplied(EMPTY_FILTERS); }}>Clear</button>
        </div>
      </form>
      {err && <p className="err" role="alert">{err}</p>}
      {!loaded && !err && <div className="card skeleton" aria-busy="true" aria-label="Loading actions"><span className="sk sk-title" /><span className="sk sk-line" /></div>}
      <div className="table-wrap"><table className="table" aria-label="Action log">
        <thead><tr><th>Time</th><th>User</th><th>Source</th><th>Action</th><th>Status</th><th>Error</th><th>Took</th></tr></thead>
        <tbody>
          {rows.map((r) => {
            const { kind, detail, risky } = actionLabel(r, endpointName);
            return (
              <tr key={r.id} className={`log-row${risky ? " risk" : ""}`}>
                <td><time>{new Date(r.ts * 1000).toLocaleString()}</time></td>
                <td>{users.find((u) => u.user_id === r.user_id)?.username ?? "Unknown user"}</td>
                <td>{r.source}</td>
                <td><span className={`log-kind${risky ? " risk" : ""}`}>{kind}</span> {detail}</td>
                <td><span className="badge">{r.status}</span></td>
                <td>{r.error ?? ""}</td>
                <td>{r.elapsed_ms == null ? "" : `${(r.elapsed_ms / 1000).toFixed(1)}s`}</td>
              </tr>
            );
          })}
        </tbody>
      </table></div>
      {loaded && !err && rows.length === 0 && <p className="muted">No matching actions.</p>}
      <div className="pager">
        {next && <button disabled={busy} onClick={() => load(applied, next)}>Load {PAGE_LIMIT} more</button>}
        {loaded && rows.length > 0 && <span className="count">Showing {rows.length}{next ? "" : " (all matching)"}</span>}
      </div>
    </div>
  );
}

export function Admin() {
  const { data: endpoints } = useLoad<Endpoint[]>("/devices");
  const users = useLoad<User[]>("/admin/users");
  const rooms = useMemo(() => [...new Set((endpoints ?? []).map((e) => e.room))].sort((a, b) => a.localeCompare(b)), [endpoints]);
  const endpointName = useCallback(
    (id: string | null) => (id ? (endpoints ?? []).find((e) => e.endpoint_id === id)?.name ?? null : null),
    [endpoints],
  );
  return (
    <section>
      <h2>Admin</h2>
      <Nodes rooms={rooms} />
      <Users rooms={rooms} users={users} />
      <ActionLog endpointName={endpointName} endpoints={endpoints ?? []} users={users.data ?? []} />
    </section>
  );
}
