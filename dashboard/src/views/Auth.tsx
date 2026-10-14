import { useState } from "react";
import { login } from "../api/client";
import { explain } from "../errors";

export function Login({ onSetup }: { onSetup: () => void }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try { await login(u, p); } catch (x) { setErr(explain(x)); } finally { setBusy(false); setP(""); }
  };
  return (
    <main className="auth">
      <form onSubmit={submit} className="stack" aria-label="Sign in">
        <h1>Sign in</h1>
        <label>Username <input value={u} maxLength={64} autoComplete="username" onChange={(e) => setU(e.target.value)} required /></label>
        <label>Password <input type="password" value={p} maxLength={256} autoComplete="current-password" onChange={(e) => setP(e.target.value)} required /></label>
        {err && <p className="err" role="alert">{err}</p>}
        <button className="primary" disabled={busy}>Sign in</button>
        <button type="button" className="link" onClick={onSetup}>First-time setup</button>
      </form>
    </main>
  );
}

// The contract defines no HTTP bootstrap endpoint: the first admin is created with the gateway CLI
// (`gateway bootstrap-admin`, one-time, refused once any user exists). So this screen only instructs.
export function Setup({ onBack }: { onBack: () => void }) {
  return (
    <main className="auth">
      <div className="stack">
        <h1>First-time setup</h1>
        <p>The first administrator is created on the gateway host, not from the browser. Run this once on pi-brain:</p>
        <pre>docker compose run --rm -it gateway --config /etc/edge-agent/config/gateway.json bootstrap-admin --username NAME</pre>
        <p className="muted">It prompts for a password (at least 12 characters) and refuses to run if any user already exists. After that, sign in here and add other users under Admin.</p>
        <button onClick={onBack}>Back to sign in</button>
      </div>
    </main>
  );
}
