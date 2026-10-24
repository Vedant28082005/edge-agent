import { useState } from "react";
import type { NotificationLevel } from "./api/types";
import { useLive } from "./live";

// Closed vocabulary from the contract; an unknown level displays as info. Every level has a word, so colour is never the only signal.
const LEVEL_LABEL: Record<NotificationLevel, string> = {
  info: "Info", warning: "Warning", action_blocked: "Blocked", rule_proposed: "Needs approval",
  rule_broken: "Rule broken", rule_limit: "Limit reached", kill_switch: "Automations paused",
};
const LEVEL_TONE: Record<NotificationLevel, string> = {
  info: "info", warning: "warn", action_blocked: "warn", rule_proposed: "info", rule_broken: "warn", rule_limit: "warn", kill_switch: "warn",
};
const known = (l: string): NotificationLevel => (l in LEVEL_LABEL ? (l as NotificationLevel) : "info");

/** Bell for automation notifications. Not rendered for guests: they have no automation routes and receive none of these messages. */
export function NotificationBell() {
  const { notifs, markRead } = useLive();
  const [open, setOpen] = useState(false);
  const unread = notifs.filter((n) => !n.read);
  return (
    <div className="bell">
      <button className="ghost sm" aria-label={`Notifications, ${unread.length} unread`} aria-expanded={open} aria-controls="bell-panel" onClick={() => setOpen((o) => !o)}>
        <span aria-hidden="true">🔔</span>
        {unread.length > 0 && <span className="bell-count" aria-hidden="true">{unread.length > 9 ? "9+" : unread.length}</span>}
      </button>
      {open && (
        <section id="bell-panel" className="bell-panel" aria-label="Notifications">
          <header className="row">
            <h3>Notifications</h3>
            <span className="spacer" />
            {unread.length > 0 && <button className="sm" onClick={() => void markRead(unread.map((n) => n.notification_id))}>Mark all read</button>}
          </header>
          {notifs.length === 0 && <p className="muted small">Nothing yet. Alerts from your automations appear here.</p>}
          <ul className="plain">
            {notifs.map((n) => {
              const lvl = known(n.level);
              return (
                <li key={n.notification_id} className={n.read ? "" : "unread"}>
                  <span className={`badge ${LEVEL_TONE[lvl]}`}>{LEVEL_LABEL[lvl]}</span>
                  <span className="notif-text">{n.text}</span>
                  <time className="muted small">{new Date(n.ts * 1000).toLocaleString()}</time>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
