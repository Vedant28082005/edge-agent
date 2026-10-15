import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { ActionLogEntry, ActionLogPage, ActionStatus, Endpoint, ForgetUsage, Node, Role, User } from "../api/types";
import { explain } from "../errors";

const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

function useLoad<T>(path: string) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try { setData(await api<T>("GET", path)); setErr(null); } catch (e) { setErr(explain(e)); }
  }, [path]);
  useEffect(() => { void reload(); }, [reload]);
  return { data, err, reload };
}

function ApproveForm({ node, onDone }: { node: Node; onDone: () => void }) {
  const eps = node.endpoints ?? [];
  const [rows, setRows] = useState(() => eps.map((e) => ({ local_id: e.local_id ?? "", name: "", room: "", aliases: "", tags: "", risk: "normal" as "normal" | "confirm" })));
  const [err, setErr] = useState<string | null>(null);
  const set = (i: number, k: string, v: string) => setRows((r) => r.map((x, j) => (j === i ? { ...x, [k]: v } : x)));
  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    try {
      await api("POST", `/admin/nodes/${node.node_id}/approve`, {
        endpoints: rows.map((r) => ({ local_id: r.local_id, name: r.name, room: r.room, aliases: csv(r.aliases), tags: csv(r.tags), risk: r.risk })),
      });
      onDone();
    } catch (e) { setErr(explain(e)); }
  };
  return (
    <form onSubmit={submit} className="stack">
      {rows.map((r, i) => (
        <fieldset key={r.local_id || i}>
          <legend>{eps[i]?.type ?? "endpoint"} · {r.local_id}</legend>
          <label>Name <input required maxLength={48} value={r.name} onChange={(e) => set(i, "name", e.target.value)} /></label>
          <label>Room <input required maxLength={48} value={r.room} onChange={(e) => set(i, "room", e.target.value)} /></label>
          <label>Aliases <input value={r.aliases} onChange={(e) => set(i, "aliases", e.target.value)} placeholder="comma separated" /></label>
          <label>Tags <input value={r.tags} pattern="[a-z0-9_,\- ]*" onChange={(e) => set(i, "tags", e.target.value)} placeholder="a-z 0-9 _ - ; comma separated" /></label>
          <label>Risk <select value={r.risk} onChange={(e) => set(i, "risk", e.target.value)}><option value="normal">normal</option><option value="confirm">confirm</option></select></label>
        </fieldset>
      ))}
      {err && <p className="err" role="alert">{err}</p>}
      <button className="primary">Approve</button>
    </form>
  );
}

function NodeRow({ node, reload }: { node: Node; reload: () => void }) {
  const [open, setOpen] = useState(false);
  const [usage, setUsage] = useState<ForgetUsage | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const forget = async (confirm: boolean) => {
    setErr(null);
    try {
      await api("DELETE", `/admin/nodes/${node.node_id}${confirm ? "?confirm=true" : ""}`);
      setUsage(null); reload();
    } catch (e) {
      if ((e as { status?: number }).status === 409 && !confirm) {
        // The 409 body is the usage list ("2 scenes, 1 automation").
        setUsage(((e as { body?: ForgetUsage }).body) ?? {});
      } else setErr(explain(e));
    }
  };

  return (
    <li className="card">
      <header>
        <h3><code>{node.node_id}</code></h3>
        <span className={`badge ${node.status === "online" ? "ok" : node.status === "pending" ? "warn" : "off"}`}>{node.status}</span>
        <span className="muted small">{node.hw} {node.fw}</span>
      </header>
      {(node.endpoints ?? []).map((e: Endpoint) => <EndpointEdit key={e.endpoint_id ?? e.local_id} ep={e} reload={reload} pending={node.status === "pending"} />)}
      <div className="row">
        {node.status === "pending" && <button className="primary" onClick={() => setOpen(!open)}>{open ? "Hide" : "Approve…"}</button>}
        <button onClick={() => api("POST", `/admin/nodes/${node.node_id}/identify`, {}).catch((e) => setErr(explain(e)))}>Identify</button>
        <button className="danger" onClick={() => forget(false)}>Forget</button>
      </div>
      {open && node.status === "pending" && <ApproveForm node={node} onDone={() => { setOpen(false); reload(); }} />}
      {usage && (
        <div className="conf" role="alertdialog" aria-label="Forget node">
          <p>{usage.message ?? `Used by ${usage.scenes ?? 0} scenes and ${usage.automations ?? 0} automations.`} Forgetting revokes the node&apos;s MQTT access.</p>
          <div className="row"><button className="danger" onClick={() => forget(true)}>Forget anyway</button><button onClick={() => setUsage(null)}>Cancel</button></div>
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
      <strong>{ep.name}</strong> <span className="muted small">{ep.room} · {ep.endpoint_id}</span>
      <button onClick={() => setEdit(!edit)}>{edit ? "Close" : "Edit"}</button>
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

function Nodes() {
  const { data, err, reload } = useLoad<Node[]>("/admin/nodes");
  const sorted = [...(data ?? [])].sort((a, b) => Number(b.status === "pending") - Number(a.status === "pending"));
  return (
    <div>
      <h3>Nodes</h3>
      {err && <p className="err" role="alert">{err}</p>}
      <ul className="plain">{sorted.map((n) => <NodeRow key={n.node_id} node={n} reload={reload} />)}</ul>
      {data?.length === 0 && <p className="muted">No nodes.</p>}
    </div>
  );
}

function UserRow({ u, reload }: { u: User; reload: () => void }) {
  const [role, setRole] = useState<Role>(u.role ?? "member");
  const [rooms, setRooms] = useState((u.rooms ?? []).join(","));
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    try { await api("PATCH", `/admin/users/${u.user_id}`, { role, rooms: csv(rooms) }); reload(); } catch (e) { setErr(explain(e)); }
  };
  return (
    <tr>
      <td>{u.username}{u.disabled ? " (disabled)" : ""}</td>
      <td><select aria-label={`role of ${u.username}`} value={role} onChange={(e) => setRole(e.target.value as Role)}><option>admin</option><option>member</option><option>guest</option></select></td>
      <td><input aria-label={`rooms of ${u.username}`} value={rooms} disabled={role === "admin"} onChange={(e) => setRooms(e.target.value)} placeholder="rooms, comma separated" /></td>
      <td className="row">
        <button onClick={save}>Save</button>
        <button onClick={() => api("PATCH", `/admin/users/${u.user_id}`, { disabled: !u.disabled }).then(reload).catch((e) => setErr(explain(e)))}>{u.disabled ? "Enable" : "Disable"}</button>
        <button className="danger" onClick={() => api("DELETE", `/admin/users/${u.user_id}`).then(reload).catch((e) => setErr(explain(e)))}>Delete</button>
        {err && <span className="err" role="alert">{err}</span>}
      </td>
    </tr>
  );
}

function Users() {
  const { data, err, reload } = useLoad<User[]>("/admin/users");
  const [f, setF] = useState({ username: "", password: "", role: "member" as Role, rooms: "" });
  const [cerr, setCerr] = useState<string | null>(null);
  const create = async (ev: React.FormEvent) => {
    ev.preventDefault();
    try {
      await api("POST", "/admin/users", { username: f.username, password: f.password, role: f.role, rooms: csv(f.rooms) });
      setF({ username: "", password: "", role: "member", rooms: "" }); setCerr(null); reload();
    } catch (e) { setCerr(explain(e)); }
  };
  return (
    <div>
      <h3>Users</h3>
      {err && <p className="err" role="alert">{err}</p>}
      <table className="table">
        <thead><tr><th>User</th><th>Role</th><th>Rooms (guest/member scope)</th><th /></tr></thead>
        <tbody>{(data ?? []).map((u) => <UserRow key={u.user_id} u={u} reload={reload} />)}</tbody>
      </table>
      <form onSubmit={create} className="row wrap" aria-label="Create user">
        <input aria-label="New username" required maxLength={64} placeholder="username" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} />
        <input aria-label="New password" required minLength={8} maxLength={256} type="password" autoComplete="new-password" placeholder="password (min 8)" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
        <select aria-label="New role" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as Role })}><option>admin</option><option>member</option><option>guest</option></select>
        <input aria-label="New rooms" placeholder="rooms" value={f.rooms} onChange={(e) => setF({ ...f, rooms: e.target.value })} />
        <button className="primary">Create</button>
      </form>
      {cerr && <p className="err" role="alert">{cerr}</p>}
    </div>
  );
}

const STATUSES: ActionStatus[] = ["done", "moving", "settled", "settle_timeout", "failed", "blocked", "confirmation_required", "declined", "expired", "denied"];
const EMPTY_FILTERS = { node_id: "", endpoint_id: "", user_id: "", status: "", from: "", to: "" };

// datetime-local value (browser local time) -> unix seconds
const toUnix = (v: string) => String(Math.floor(new Date(v).getTime() / 1000));

function logQuery(f: typeof EMPTY_FILTERS, cursor?: string): string {
  const q = new URLSearchParams();
  if (f.node_id.trim()) q.set("node_id", f.node_id.trim());
  if (f.endpoint_id.trim()) q.set("endpoint_id", f.endpoint_id.trim());
  if (f.user_id.trim()) q.set("user_id", f.user_id.trim());
  if (f.status) q.set("status", f.status);
  if (f.from) q.set("from", toUnix(f.from));
  if (f.to) q.set("to", toUnix(f.to));
  if (cursor) q.set("cursor", cursor);
  const qs = q.toString();
  return `/admin/action-log${qs ? `?${qs}` : ""}`;
}

function ActionLog() {
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
    <div>
      <h3>Action log</h3>
      <form onSubmit={apply} className="row wrap" aria-label="Action log filters">
        <input aria-label="Filter node" placeholder="node id" value={draft.node_id} onChange={(e) => set("node_id", e.target.value)} />
        <input aria-label="Filter endpoint" placeholder="endpoint id" value={draft.endpoint_id} onChange={(e) => set("endpoint_id", e.target.value)} />
        <input aria-label="Filter user" placeholder="user id" maxLength={64} value={draft.user_id} onChange={(e) => set("user_id", e.target.value)} />
        <select aria-label="Filter status" value={draft.status} onChange={(e) => set("status", e.target.value)}>
          <option value="">any status</option>
          {STATUSES.map((st) => <option key={st} value={st}>{st}</option>)}
        </select>
        <label>From <input type="datetime-local" aria-label="Filter from" value={draft.from} onChange={(e) => set("from", e.target.value)} /></label>
        <label>To <input type="datetime-local" aria-label="Filter to" value={draft.to} onChange={(e) => set("to", e.target.value)} /></label>
        <button className="primary">Apply</button>
        <button type="button" onClick={() => { setDraft(EMPTY_FILTERS); setApplied(EMPTY_FILTERS); }}>Clear</button>
      </form>
      {err && <p className="err" role="alert">{err}</p>}
      <table className="table" aria-label="Action log">
        <thead><tr><th>Time</th><th>User</th><th>Source</th><th>Action</th><th>Status</th><th>Error</th><th>Took</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td><time>{new Date(r.ts * 1000).toLocaleString()}</time></td>
              <td>{r.user_id}</td>
              <td>{r.source}</td>
              <td>{r.kind === "run_scene" ? `run_scene ${r.scene_id ?? ""}` : `set ${r.endpoint_id ?? ""} ${r.capability ?? ""}=${r.value == null ? "" : String(r.value)}`}</td>
              <td><span className="badge">{r.status}</span></td>
              <td>{r.error ?? ""}</td>
              <td>{r.elapsed_ms == null ? "" : `${(r.elapsed_ms / 1000).toFixed(1)}s`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {loaded && !err && rows.length === 0 && <p className="muted">No matching actions.</p>}
      {next && <button disabled={busy} onClick={() => load(applied, next)}>Load more</button>}
    </div>
  );
}

export function Admin() {
  return (
    <section>
      <h2>Admin</h2>
      <Nodes />
      <Users />
      <ActionLog />
    </section>
  );
}
