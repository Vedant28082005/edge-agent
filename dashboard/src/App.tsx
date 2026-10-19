import { useEffect, useState, useSyncExternalStore } from "react";
import { logout, refreshSession, session } from "./api/client";
import { PendingConfirmations } from "./Confirmation";
import { LiveProvider } from "./live";
import { Activity } from "./views/Activity";
import { Admin } from "./views/Admin";
import { Login, Setup } from "./views/Auth";
import { Chat } from "./views/Chat";
import { Devices } from "./views/Devices";

type Tab = "devices" | "chat" | "activity" | "admin";

function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark" | "auto">(() => {
    try { return (localStorage.getItem("edge.theme") as "light" | "dark") ?? "auto"; } catch { return "auto"; }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", theme);
    try { if (theme === "auto") localStorage.removeItem("edge.theme"); else localStorage.setItem("edge.theme", theme); } catch { /* storage unavailable */ }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "auto" ? "dark" : t === "dark" ? "light" : "auto"))] as const;
}

function Shell() {
  const [tab, setTab] = useState<Tab>("devices");
  const [theme, cycle] = useTheme();
  const isAdmin = session.role === "admin"; // cosmetic only: the API enforces roles and we render only what it returns
  return (
    <div className="shell">
      <header className="top">
        <strong className="brand">Home</strong>
        <nav aria-label="Main">
          {(["devices", "chat", "activity", ...(isAdmin ? ["admin"] : [])] as Tab[]).map((t) => (
            <button key={t} aria-current={tab === t} onClick={() => setTab(t)}>{t}</button>
          ))}
        </nav>
        <span className="spacer" />
        <button className="ghost sm" onClick={cycle} aria-label="Toggle theme">theme: {theme}</button>
        <button className="ghost sm" onClick={logout}>Log out</button>
      </header>
      <PendingConfirmations />
      <main className="content">
        {tab === "devices" && <Devices />}
        {tab === "chat" && <Chat />}
        {tab === "activity" && <Activity />}
        {tab === "admin" && isAdmin && <Admin />}
      </main>
    </div>
  );
}

export default function App() {
  const authed = useSyncExternalStore(session.subscribe, () => session.authed);
  const [booting, setBooting] = useState(session.hasRefresh());
  const [setup, setSetup] = useState(false);

  useEffect(() => {
    if (!booting) return;
    void refreshSession().finally(() => setBooting(false));
  }, [booting]);

  if (booting) return <p className="muted center">Loading…</p>;
  if (!authed) return setup ? <Setup onBack={() => setSetup(false)} /> : <Login onSetup={() => setSetup(true)} />;
  return <LiveProvider onClosed={() => { /* socket gave up: session is gone, state flips via session.subscribe */ }}><Shell /></LiveProvider>;
}
