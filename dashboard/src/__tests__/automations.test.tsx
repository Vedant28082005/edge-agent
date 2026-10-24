import { screen, waitFor, within } from "@testing-library/react";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes } from "../test/fixtures";
import type { AutomationRule, Notification } from "../api/types";
import { openApp, push } from "./helpers";

const base = { owner: "u1", cooldown_s: 60, max_runs_per_day: 20, created_at: 1_700_000_000, updated_at: 1_700_000_000, rooms: ["bedroom"] };
const running: AutomationRule = {
  ...base, rule_id: "ru_00000001", name: "Lamp on at dusk", enabled: true, status: "approved", created_by: "user", approved_by: "u1", approved_at: 1_700_000_001,
  trigger: { type: "time_of_day", at: "18:30" }, action: { type: "set_state", endpoint_id: "ep_00000001", capability: "power", value: true },
};
const proposed: AutomationRule = {
  ...base, rule_id: "ru_00000002", name: "Close blind when hot", enabled: false, status: "proposed", created_by: "llm",
  trigger: { type: "device_state_change", endpoint_id: "ep_00000004", capability: "temperature", to: 30 },
  condition: { all: [{ type: "device_state", endpoint_id: "ep_00000002", capability: "position", op: "gt", value: 50 }] },
  action: { type: "set_state", endpoint_id: "ep_00000002", capability: "position", value: 0 },
};
const rulesRoutes = (list: AutomationRule[]) => [
  route("get", "/automations", () => ({ json: list })),
  route("get", "/admin/automations/kill-switch", () => ({ json: { automations_enabled: true } })),
];
const note = (over: Partial<Notification> = {}): Notification => ({
  notification_id: "nt_000000000001", ts: 1_700_000_100, level: "action_blocked", source: "system", text: "Front door was not unlocked: it needs your confirmation.", read: false, ...over,
});

describe("automations", () => {
  it("guests get no Automations tab, no bell, and never call an automation route", async () => {
    let calls = 0;
    server.use(
      route("get", "/automations", () => { calls++; return { status: 403, json: { error: "forbidden" } }; }),
      route("get", "/notifications", () => { calls++; return { status: 403, json: { error: "forbidden" } }; }),
      ...baseRoutes("guest"),
    );
    await openApp("guest");
    expect(screen.queryByRole("button", { name: "automations" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Notifications/ })).not.toBeInTheDocument();
    expect(calls).toBe(0);
  });

  it("lists rules in plain words, never showing ids, owners or tokens", async () => {
    server.use(...rulesRoutes([running, proposed]), ...baseRoutes("admin"));
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "automations" }));
    expect(await screen.findByText("Lamp on at dusk")).toBeInTheDocument();
    expect(screen.getByText(/Set Ceiling light power to on/)).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    for (const forbidden of ["ru_0000", "ep_0000", "u1", "approved_by"]) expect(text).not.toContain(forbidden);
  });

  it("shows LLM proposals in a Proposed section that only a user approval can move", async () => {
    let list = [running, proposed];
    server.use(
      route("get", "/automations", () => ({ json: list })),
      route("post", "/automations/{rule_id}/approve", ({ params }) => {
        expect(params.rule_id).toBe("ru_00000002");
        list = [running, { ...proposed, status: "approved", enabled: true, approved_by: "u1", approved_at: 1_700_000_500 }];
        return { json: list[1] };
      }),
      route("get", "/admin/automations/kill-switch", () => ({ json: { automations_enabled: true } })),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "automations" }));
    const section = await screen.findByRole("region", { name: "Proposed rules" });
    expect(within(section).getByText("Suggested by the assistant")).toBeInTheDocument();
    expect(within(section).getByText(/Close blind when hot/)).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Proposed rules" })).not.toBeInTheDocument());
  });

  it("disables an approved rule", async () => {
    let disabled = false;
    server.use(
      route("get", "/automations", () => ({ json: [disabled ? { ...running, status: "disabled", enabled: false } : running] })),
      route("post", "/automations/{rule_id}/disable", () => { disabled = true; return { json: { ...running, status: "disabled", enabled: false } }; }),
      ...baseRoutes("member"),
    );
    const { user } = await openApp("member");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("surfaces the server's refusal when approval is blocked", async () => {
    server.use(
      route("get", "/automations", () => ({ json: [proposed] })),
      route("post", "/automations/{rule_id}/approve", () => ({ status: 409, json: { error: "confirm_endpoint_not_allowed" } })),
      ...baseRoutes("member"),
    );
    const { user } = await openApp("member");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("need your confirmation");
  });

  it("creates a rule: builds the contract body, with source user and no server-owned fields", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      route("get", "/automations", () => ({ json: [] })),
      route("post", "/automations", async ({ request }) => { body = (await request.json()) as Record<string, unknown>; return { status: 201, json: running }; }),
      ...baseRoutes("member"),
    );
    const { user } = await openApp("member");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await user.click(await screen.findByRole("button", { name: "New rule" }));
    await user.type(screen.getByLabelText("Name"), "Hall warm");
    await user.selectOptions(screen.getByLabelText("Trigger device"), "Hall sensor (hall)");
    await user.type(screen.getByLabelText("To value"), "30");
    await user.type(screen.getByLabelText("Notification text"), "Hall is hot");
    await user.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(body.name).toBe("Hall warm"));
    expect(body).toEqual({
      name: "Hall warm", source: "user", cooldown_s: 60, max_runs_per_day: 20,
      trigger: { type: "device_state_change", endpoint_id: "ep_00000004", capability: "temperature", to: 30 },
      action: { type: "notify", level: "info", text: "Hall is hot" },
    });
  });

  it("never offers a risk:confirm device (a lock) as an action target", async () => {
    server.use(route("get", "/automations", () => ({ json: [] })), ...baseRoutes("admin"));
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await user.click(await screen.findByRole("button", { name: "New rule" }));
    await user.selectOptions(screen.getByLabelText("Action type"), "set_state");
    const target = screen.getByLabelText("Action device");
    expect(within(target).queryByText(/Front door/)).not.toBeInTheDocument();
    expect(within(target).getByText(/Ceiling light/)).toBeInTheDocument();
    // the lock can still be read by a trigger
    expect(within(screen.getByLabelText("Trigger device")).getByText(/Front door/)).toBeInTheDocument();
  });

  it("shows run history with outcomes in words and no internal ids", async () => {
    server.use(
      ...rulesRoutes([running]),
      route("get", "/automations/{rule_id}/runs", () => ({
        json: {
          items: [
            { run_id: 7, rule_id: "ru_00000001", ts: 1_700_000_200, outcome: "blocked_confirm", action_log_id: 99, call_id: "call-secret-1" },
            { run_id: 6, rule_id: "ru_00000001", ts: 1_700_000_100, outcome: "fired", action_status: "done", action_log_id: 98 },
          ],
          next_cursor: null,
        },
      })),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await user.click(await screen.findByRole("button", { name: "History" }));
    const list = await screen.findByRole("list", { name: "Run history" });
    expect(within(list).getByText("Blocked: needs your confirmation")).toBeInTheDocument();
    expect(within(list).getByText("Ran")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("call-secret-1");
  });

  it("refetches rules when the socket reports automations_changed", async () => {
    let list = [running];
    server.use(
      route("get", "/automations", () => ({ json: list })),
      route("get", "/admin/automations/kill-switch", () => ({ json: { automations_enabled: true } })),
      ...baseRoutes("admin"),
    );
    const { user, ws } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await screen.findByText("Lamp on at dusk");
    list = [running, proposed];
    push(ws, { type: "automations_changed", rule_id: "ru_00000002", change: "created" });
    expect(await screen.findByRole("region", { name: "Proposed rules" })).toBeInTheDocument();
  });

  it("hides the kill switch from members and never calls it", async () => {
    let calls = 0;
    server.use(
      route("get", "/automations", () => ({ json: [running] })),
      route("get", "/admin/automations/kill-switch", () => { calls++; return { status: 403, json: { error: "forbidden" } }; }),
      ...baseRoutes("member"),
    );
    const { user } = await openApp("member");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await screen.findByText("Lamp on at dusk");
    expect(screen.queryByRole("region", { name: "Automation kill switch" })).not.toBeInTheDocument();
    expect(calls).toBe(0);
  });

  it("admin kill switch: pausing asks first, then PUTs; a kill_switch event updates the page", async () => {
    let put: unknown;
    server.use(
      ...rulesRoutes([running]),
      route("put", "/admin/automations/kill-switch", async ({ request }) => { put = await request.json(); return { json: { automations_enabled: false } }; }),
      ...baseRoutes("admin"),
    );
    const { user, ws } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "automations" }));
    await user.click(await screen.findByRole("button", { name: "Pause all…" }));
    expect(put).toBeUndefined(); // nothing is sent until confirmed
    await user.click(within(screen.getByRole("group", { name: "Confirm pause" })).getByRole("button", { name: "Pause all" }));
    expect(await screen.findByText(/Automations are paused/)).toBeInTheDocument();
    expect(put).toEqual({ automations_enabled: false });
    push(ws, { type: "automations_changed", rule_id: null, change: "kill_switch", automations_enabled: true });
    await waitFor(() => expect(screen.queryByText(/Automations are paused/)).not.toBeInTheDocument());
  });
});

describe("notification bell", () => {
  it("counts live automation_notification events, shows server text as plain text, and marks read", async () => {
    let marked: unknown;
    server.use(
      route("get", "/notifications", () => ({
        json: { items: [note({ notification_id: "nt_000000000009", ts: 1_700_000_001, level: "info", text: "Earlier one", read: true })], next_cursor: null },
      })),
      route("post", "/notifications/read", async ({ request }) => { marked = await request.json(); return { status: 204 }; }),
      ...baseRoutes("member"),
    );
    const { user, ws } = await openApp("member");
    expect(await screen.findByRole("button", { name: "Notifications, 0 unread" })).toBeInTheDocument();
    const { read, ...live } = note({ text: "<b>Front door</b> blocked" });
    void read;
    push(ws, { type: "automation_notification", ...live });
    await user.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    const panel = screen.getByRole("region", { name: "Notifications" });
    expect(within(panel).getByText("<b>Front door</b> blocked")).toBeInTheDocument(); // text, never markup
    expect(within(panel).getByText("Blocked")).toBeInTheDocument(); // level shown as a word
    await user.click(within(panel).getByRole("button", { name: "Mark all read" }));
    await waitFor(() => expect(marked).toEqual({ notification_ids: ["nt_000000000001"] }));
    expect(await screen.findByRole("button", { name: "Notifications, 0 unread" })).toBeInTheDocument();
  });

  it("treats an unknown level as info", async () => {
    server.use(...baseRoutes("member"));
    const { user, ws } = await openApp("member");
    const { read, ...live } = note({ level: "brand_new_level" as Notification["level"], text: "Something new" });
    void read;
    push(ws, { type: "automation_notification", ...live });
    await user.click(await screen.findByRole("button", { name: /Notifications/ }));
    expect(within(screen.getByRole("region", { name: "Notifications" })).getByText("Info")).toBeInTheDocument();
  });
});
