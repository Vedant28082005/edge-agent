import { useLive } from "../live";

// Live feed of what the WebSocket delivered to this browser (state, settle, confirmations, notifications), in memory only.
// The persistent history is the admin Action log.
function describe(m: ReturnType<typeof useLive>["feed"][number]["msg"]): string {
  switch (m.type) {
    case "state": return `${m.endpoint_id} state ${JSON.stringify(m.values)}`;
    case "action_settled": return `${m.endpoint_id} ${m.capability} settled at ${String(m.observed)}`;
    case "action_settle_timeout": return `${m.endpoint_id} ${m.capability} did not settle (observed ${String(m.observed)})`;
    case "confirmation_request": return `confirmation requested: ${m.summary}`;
    case "confirmation_declined": return `declined: ${m.summary}`;
    case "confirmation_expired": return `expired: ${m.summary}`;
    case "notification": return `${m.level}: ${m.text}`;
    case "automation_notification": return `${m.level}: ${m.text}`;
    case "async_result": return `async ${m.kind}: ${m.text}`;
    case "registry_changed": return `registry changed (v${m.version})`;
    case "scenes_changed": return `scene ${m.scene_id} ${m.change}`;
    default: return m.type;
  }
}

export function Activity() {
  const { feed, ws } = useLive();
  return (
    <section>
      <h2>Activity <span className={`ws ws-${ws}`}>{ws === "open" ? "live" : ws}</span></h2>
      {feed.length === 0 && <p className="muted">Nothing yet. Events appear here as they arrive.</p>}
      <ul className="plain feed">
        {feed.map((f) => (
          <li key={f.id}><time>{new Date(f.at).toLocaleTimeString()}</time> <span>{describe(f.msg)}</span></li>
        ))}
      </ul>
    </section>
  );
}
