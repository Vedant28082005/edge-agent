import { useEffect, useState, useSyncExternalStore } from "react";
import { logout, refreshSession, session } from "./api/client";
import { PendingConfirmations } from "./Confirmation";
import { LiveProvider } from "./live";
import { NotificationBell } from "./Notifications";
import { Activity } from "./views/Activity";
import { Admin } from "./views/Admin";
import { Automations } from "./views/Automations";
import { Login, Setup } from "./views/Auth";
import { IconActivity, IconAdmin, IconAuto, IconAutomations, IconChat, IconDevices, IconLogout, IconMoon, IconSun } from "./icons";
import { ToastProvider } from "./toast";
import { Chat } from "./views/Chat";
import { Devices } from "./views/Devices";

type Tab = "devices" | "chat" | "automations" | "activity" | "admin";

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

const NAV: Record<Tab, { label: string; icon: () => React.JSX.Element }> = {
  devices: { label: "Devices", icon: IconDevices },
  chat: { label: "Chat", icon: IconChat },
  automations: { label: "Automations", icon: IconAutomations },
  activity: { label: "Activity", icon: IconActivity },
  admin: { label: "Admin", icon: IconAdmin },
};
const TITLES: Record<Tab, string> = { devices: "Devices", chat: "Chat", automations: "Automations", activity: "Activity", admin: "Admin" };

function Shell() {
  const [tab, setTab] = useState<Tab>("devices");
  const [theme, cycle] = useTheme();
  const canAutomate = session.role !== "guest"; // guests get 403 on every automation route: no tab, no bell
  const isAdmin = session.role === "admin"; // cosmetic only: the API enforces roles and we render only what it returns
  useEffect(() => { document.title = `${TITLES[tab]} · Edge Home`; }, [tab]);
  const ThemeIcon = theme === "dark" ? IconMoon : theme === "light" ? IconSun : IconAuto;
  return (
    <div className="shell">
      <aside className="side">
        <strong className="brand">Home</strong>
        <nav aria-label="Main">
          {(["devices", "chat", ...(canAutomate ? ["automations"] : []), "activity", ...(isAdmin ? ["admin"] : [])] as Tab[]).map((t) => {
            const Icon = NAV[t].icon;
            return (
              <button key={t} aria-current={tab === t} onClick={() => setTab(t)}><Icon /><span>{NAV[t].label}</span></button>
            );
          })}
        </nav>
        <div className="side-actions">
          {canAutomate && <NotificationBell />}
          <button className="ghost icon-btn" onClick={cycle} aria-label="Toggle theme" title={`Theme: ${theme}`}><ThemeIcon /></button>
          <button className="ghost icon-btn" onClick={logout} aria-label="Log out" title="Log out"><IconLogout /></button>
        </div>
      </aside>
      <PendingConfirmations />
      <main className={`content${tab === "chat" ? " is-chat" : ""}`}>
        {tab === "devices" && <Devices />}
        {tab === "chat" && <Chat />}
        {tab === "automations" && canAutomate && <Automations />}
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
  return <LiveProvider onClosed={() => { /* socket gave up: session is gone, state flips via session.subscribe */ }}><ToastProvider><Shell /></ToastProvider></LiveProvider>;
}
