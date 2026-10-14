import { useState } from "react";
import { api } from "./api/client";
import { explain } from "./errors";
import { useLive, type Conf } from "./live";

// The summary and action are shown exactly as the server sent them. The UI never computes or shows hashes
// and never handles tokens: the gateway mints the token and hands it to the agent (agent flow).
export function ConfirmationCard({ id }: { id: string }) {
  const { confs, patchConf, addConf } = useLive();
  const c: Conf | undefined = confs[id];
  const [busy, setBusy] = useState(false);
  if (!c) return null;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); } finally { setBusy(false); }
  };

  const approve = () => run(async () => {
    patchConf(id, { status: "approving", error: undefined });
    try {
      await api("POST", "/confirmations", { request_id: id });
      patchConf(id, { status: "approved" });
    } catch (e) {
      patchConf(id, { status: "error", error: explain(e) });
    }
  });

  const decline = () => run(async () => {
    try {
      await api("POST", `/confirmations/${encodeURIComponent(id)}/decline`);
      patchConf(id, { status: "declined" });
    } catch (e) {
      patchConf(id, { status: "error", error: explain(e) });
    }
  });

  const retry = () => run(async () => {
    try {
      const r = await api<{ request_id: string }>("POST", `/confirmations/${encodeURIComponent(id)}/retry`);
      patchConf(id, { retriedAs: r.request_id });
      // The new request is delivered as a confirmation_request message; add it now in case it arrives later than the reply.
      addConf({ ...c.req, request_id: r.request_id });
    } catch (e) {
      patchConf(id, { error: explain(e) });
    }
  });

  const done = c.status === "approved" || c.status === "declined" || c.status === "expired";
  return (
    <div className={`conf conf-${c.status}`} role="group" aria-label="Confirmation required">
      <div className="conf-summary">{c.req.summary}</div>
      <dl className="conf-action">
        {Object.entries(c.req.action).map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd></div>
        ))}
      </dl>
      {c.status === "pending" || c.status === "approving" || (c.status === "error" && !done) ? (
        <div className="row">
          <button className="primary" disabled={busy || c.status === "approving"} onClick={approve}>Confirm</button>
          <button disabled={busy} onClick={decline}>Cancel</button>
        </div>
      ) : null}
      {c.status === "approved" && <p className="muted">Approved. Waiting for the result…</p>}
      {c.status === "declined" && <p className="muted">Declined. Nothing was executed.</p>}
      {c.status === "expired" && <p className="warn">Expired. Nothing was executed.</p>}
      {c.error && <p className="err" role="alert">{c.error}</p>}
      {(c.status === "declined" || c.status === "expired" || (c.status === "error" && c.error?.includes("Retry"))) && !c.retriedAs && (
        <button disabled={busy} onClick={retry}>Retry</button>
      )}
      {c.retriedAs && <p className="muted">A new request was created.</p>}
    </div>
  );
}

export function PendingConfirmations() {
  const { confs } = useLive();
  const ids = Object.keys(confs).filter((id) => !confs[id].retriedAs);
  if (!ids.length) return null;
  return <div className="conf-list">{ids.map((id) => <ConfirmationCard key={id} id={id} />)}</div>;
}
