import { useLive } from "../live";

// Live feed of what the WebSocket delivered to this browser (state, settle, confirmations, notifications), in memory only.
// The persistent history is the admin Action log.
// Devices are named by their admin-set label, never by endpoint id (handoff rule 4).
function describe(m: ReturnType<typeof useLive>["feed"][number]["msg"], nameOf: (id: string) => string): string {
  switch (m.type) {
    case "state": return `${nameOf(m.endpoint_id)} state ${JSON.stringify(m.values)}`;
    case "action_settled": return `${nameOf(m.endpoint_id)} ${m.capability} settled at ${String(m.observed)}`;
    case "action_settle_timeout": return `${nameOf(m.endpoint_id)} ${m.capability} did not settle (observed ${String(m.observed)})`;
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
  const { feed, ws, endpoints } = useLive();
  const nameOf = (id: string) => (endpoints ?? []).find((e) => e.endpoint_id === id)?.name ?? "A device";
  return (
    <section>
      <h2>Activity <span className={`ws ws-${ws}`}>{ws === "open" ? "live" : ws}</span></h2>
      {feed.length === 0 && <div className="empty"><p><strong>Nothing yet</strong></p><p className="small">Events appear here as they arrive.</p></div>}
      <ul className="plain feed">
        {feed.map((f) => (
          <li key={f.id}><time>{new Date(f.at).toLocaleTimeString()}</time> <span>{describe(f.msg, nameOf)}</span></li>
        ))}
      </ul>
    </section>
  );
}
