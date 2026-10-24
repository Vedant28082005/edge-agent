import { fireEvent, screen, within, waitFor } from "@testing-library/react";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes, blind, lamp } from "../test/fixtures";
import { openApp, push } from "./helpers";

// jsdom does not step range inputs from the keyboard: change the value, then release (the control commits on release).
function moveSlider(name: string, value: number) {
  const el = screen.getByRole("slider", { name });
  fireEvent.change(el, { target: { value: String(value) } });
  fireEvent.keyUp(el);
}

describe("devices", () => {
  it("groups endpoints by room (room-at-a-time) and renders controls from capability types", async () => {
    server.use(...baseRoutes());
    const { user } = await openApp();
    // bedroom sorts first alphabetically and is shown by default.
    const bedroom = screen.getByRole("heading", { name: "bedroom" }).parentElement!;
    expect(within(bedroom).getByRole("article", { name: "Ceiling light" })).toBeInTheDocument();
    expect(within(bedroom).getByRole("switch", { name: "Power" })).not.toBeChecked(); // bool -> switch
    expect(within(bedroom).getByRole("slider", { name: "Brightness" })).toHaveValue("30"); // ranged int -> slider
    expect(screen.queryByRole("heading", { name: "hall" })).not.toBeInTheDocument(); // only one room shown at a time

    await user.click(screen.getByRole("button", { name: "hall" }));
    const hall = screen.getByRole("heading", { name: "hall" }).parentElement!;
    expect(within(hall).getByRole("button", { name: "Unlock…" })).toBeInTheDocument(); // lock -> LockCard, never a switch/select
    expect(within(hall).getByText("Secured device")).toBeInTheDocument();
    expect(within(hall).getAllByText("21.5 C").length).toBeGreaterThan(0); // sensor -> readout (and the readings strip)
    expect(screen.queryByRole("heading", { name: "bedroom" })).not.toBeInTheDocument();
  });

  it("applies live state from the WebSocket", async () => {
    server.use(...baseRoutes());
    const { ws } = await openApp();
    push(ws, { type: "state", endpoint_id: "ep_00000001", values: { power: true } });
    await waitFor(() => expect(screen.getByRole("switch", { name: "Power" })).toBeChecked());
  });

  it("walks a motion capability accepted, settling, settled", async () => {
    let sent: unknown;
    server.use(
            route("post", "/devices/{endpoint_id}/set", async ({ request }) => {
        sent = await request.json();
        return { json: { ok: true, status: "moving", verified: false } };
      }),
      ...baseRoutes(),
    );
    const { ws } = await openApp();
    moveSlider("Position", 1);
    await waitFor(() => expect(sent).toEqual({ capability: "position", value: 1 }));
    await waitFor(() => expect(screen.getByRole("status", { name: "motion progress" })).toHaveTextContent("accepted → settling"));
    expect(screen.getByRole("status", { name: "motion progress" })).toHaveTextContent("settling (up to 30s)"); // Capability.settle_s
    push(ws, { type: "action_settled", call_id: "c1", endpoint_id: "ep_00000002", capability: "position", value: 1, observed: 1, elapsed_ms: 4200 });
    expect(await screen.findByText(/accepted → settling → settled \(4\.2s\)/)).toBeInTheDocument();
  });

  it("shows settle_timeout", async () => {
    server.use(route("post", "/devices/{endpoint_id}/set", () => ({ json: { ok: true, status: "moving" } })), ...baseRoutes());
    const { ws } = await openApp();
    moveSlider("Position", 1);
    await screen.findByText(/accepted → settling/);
    push(ws, { type: "action_settle_timeout", call_id: "c1", endpoint_id: "ep_00000002", capability: "position", value: 1, observed: null, settle_s: 30 });
    expect(await screen.findByText(/settle timeout \(did not reach target in 30s, observed none\)/)).toBeInTheDocument();
  });

  it("stops claiming motion once the capability's settle_s has passed without a result", async () => {
    const quick = { ...blind, capabilities: [{ ...blind.capabilities[0], settle_s: 1 }] };
    server.use(route("post", "/devices/{endpoint_id}/set", () => ({ json: { ok: true, status: "moving" } })), ...baseRoutes("admin", [quick]));
    await openApp();
    moveSlider("Position", 1);
    expect(await screen.findByText(/settling \(up to 1s\)/)).toBeInTheDocument();
    expect(await screen.findByText(/settling \(no result after 1s\)/, undefined, { timeout: 2500 })).toBeInTheDocument();
  });

  it("a confirmation_required body without the server summary is an error, not a client-written prompt", async () => {
    server.use(route("post", "/devices/{endpoint_id}/set", () => ({ status: 409, json: { error: "confirmation_required" } })), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "hall" }));
    await user.click(screen.getByRole("button", { name: "Unlock…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("needs your confirmation");
    expect(screen.queryByRole("group", { name: "Confirmation required" })).not.toBeInTheDocument();
  });

  it("direct confirm flow: 409 confirmation_required, gateway mints token, the retried set carries it", async () => {
    const action = { type: "set_state", endpoint_id: "ep_00000003", capability: "lock", value: "unlocked" };
    const hash = "a".repeat(64);
    const sets: Array<Record<string, unknown>> = [];
    let minted: unknown;
    server.use(
            route("post", "/devices/{endpoint_id}/set", async ({ request }) => {
        const b = (await request.json()) as Record<string, unknown>;
        sets.push(b);
        return b.confirmation_token ? { json: { ok: true, status: "done" } } : { status: 409, json: { error: "confirmation_required", summary: "Unlock Front door?", action, action_hash: hash, expires_in_s: 60 } };
      }),
      route("post", "/confirmations", async ({ request }) => {
        minted = await request.json();
        return { json: { status: "minted", confirmation_token: "tok-from-gateway", action_hash: "h", expires_at: 1 } };
      }),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "hall" }));
    await user.click(screen.getByRole("button", { name: "Unlock…" }));
    const card = await screen.findByRole("group", { name: "Confirmation required" });
    expect(card.querySelector("p")?.textContent).toBe("Unlock Front door?"); // the server summary, no client wording around it
    expect(document.body.textContent).not.toContain(hash);
    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(sets).toHaveLength(2));
    expect(sets[1]).toEqual({ capability: "lock", value: "unlocked", confirmation_token: "tok-from-gateway" });
    expect(minted).toEqual({ action }); // exactly as the server sent it
    expect(document.body.textContent).not.toContain(hash);
    expect(document.body.textContent).not.toContain("tok-from-gateway");
  });

  it("power is a switch; rapid toggles are debounced into one command with the last value", async () => {
    const sent: unknown[] = [];
    server.use(
      route("post", "/devices/{endpoint_id}/set", async ({ request }) => { sent.push(await request.json()); return { json: { ok: true, status: "done" } }; }),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    const sw = screen.getByRole("switch", { name: "Power" });
    expect(sw).not.toBeChecked();
    await user.click(sw);
    await user.click(sw);
    await user.click(sw); // on, off, on within the debounce window
    expect(screen.getByRole("switch", { name: "Power" })).toBeChecked(); // pending value shown
    expect(screen.getByText("Sending…")).toBeInTheDocument();
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({ capability: "power", value: true });
  });

  it("slider sends only on release, shows a live value label and reverts with an inline error on failure", async () => {
    const sent: unknown[] = [];
    server.use(
      route("post", "/devices/{endpoint_id}/set", async ({ request }) => { sent.push(await request.json()); return { json: { ok: false, error: "device_offline" } }; }),
      ...baseRoutes(),
    );
    await openApp();
    const el = screen.getByRole("slider", { name: "Brightness" });
    fireEvent.change(el, { target: { value: "55" } });
    fireEvent.change(el, { target: { value: "60" } }); // drag ticks
    expect(screen.getByText("60 %")).toBeInTheDocument(); // live label with unit
    expect(sent).toHaveLength(0); // nothing sent while dragging
    fireEvent.keyUp(el); // release
    await waitFor(() => expect(sent).toEqual([{ capability: "brightness", value: 60 }]));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Brightness" })).toHaveValue("30"); // reverted to the last known value
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("an offline device is dimmed, dashed and non-interactive", async () => {
    server.use(...baseRoutes("admin", [{ ...lamp, online: false }]));
    await openApp();
    const card = screen.getByRole("article", { name: "Ceiling light" });
    expect(card).toHaveClass("offline");
    expect(within(card).getByText("offline")).toBeInTheDocument();
    const sw = within(card).getByRole("switch", { name: "Power" });
    expect(sw).toBeDisabled();
    expect(sw).toHaveAttribute("tabindex", "-1");
    expect(within(card).getByRole("slider", { name: "Brightness" })).toBeDisabled();
  });

  it("shows an empty state when there are no devices", async () => {
    server.use(...baseRoutes("admin", []));
    await openApp();
    expect(screen.getByText("No devices yet. Add one in Admin.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading devices")).not.toBeInTheDocument();
  });

  it("summarises other rooms in the Around the house rail", async () => {
    server.use(...baseRoutes());
    await openApp();
    const rail = screen.getByRole("complementary", { name: "Around the house" });
    expect(within(rail).getByText("hall")).toBeInTheDocument();
    expect(within(rail).getByText("2 devices")).toBeInTheDocument();
  });
});
