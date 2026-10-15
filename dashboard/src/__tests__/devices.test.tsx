import { fireEvent, screen, within, waitFor } from "@testing-library/react";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes, blind } from "../test/fixtures";
import { openApp, push } from "./helpers";

// jsdom does not step range inputs from the keyboard: change the value, then release (the control commits on release).
function moveSlider(name: string, value: number) {
  const el = screen.getByRole("slider", { name });
  fireEvent.change(el, { target: { value: String(value) } });
  fireEvent.keyUp(el);
}

describe("devices", () => {
  it("groups endpoints by room and renders controls from capability types", async () => {
    server.use(...baseRoutes());
    await openApp();
    const bedroom = screen.getByRole("heading", { name: "bedroom" }).parentElement!;
    expect(within(bedroom).getByRole("article", { name: "Ceiling light" })).toBeInTheDocument();
    expect(within(bedroom).getByRole("switch", { name: "power" })).not.toBeChecked(); // bool -> switch
    expect(within(bedroom).getByRole("slider", { name: "brightness" })).toHaveValue("30"); // ranged int -> slider
    const hall = screen.getByRole("heading", { name: "hall" }).parentElement!;
    expect(within(hall).getByRole("combobox", { name: "lock" })).toHaveValue("locked"); // enum -> select
    expect(within(hall).getByText("21.5 C")).toBeInTheDocument(); // sensor -> readout
  });

  it("applies live state from the WebSocket", async () => {
    server.use(...baseRoutes());
    const { ws } = await openApp();
    push(ws, { type: "state", endpoint_id: "ep_00000001", values: { power: true } });
    await waitFor(() => expect(screen.getByRole("switch", { name: "power" })).toBeChecked());
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
    moveSlider("position", 1);
    await waitFor(() => expect(sent).toEqual({ capability: "position", value: 1 }));
    await waitFor(() => expect(screen.getByRole("status", { name: "motion progress" })).toHaveTextContent("accepted → settling"));
    expect(screen.getByRole("status", { name: "motion progress" })).toHaveTextContent("settling (up to 30s)"); // Capability.settle_s
    push(ws, { type: "action_settled", call_id: "c1", endpoint_id: "ep_00000002", capability: "position", value: 1, observed: 1, elapsed_ms: 4200 });
    expect(await screen.findByText(/accepted → settling → settled \(4\.2s\)/)).toBeInTheDocument();
  });

  it("shows settle_timeout", async () => {
    server.use(route("post", "/devices/{endpoint_id}/set", () => ({ json: { ok: true, status: "moving" } })), ...baseRoutes());
    const { ws } = await openApp();
    moveSlider("position", 1);
    await screen.findByText(/accepted → settling/);
    push(ws, { type: "action_settle_timeout", call_id: "c1", endpoint_id: "ep_00000002", capability: "position", value: 1, observed: null, settle_s: 30 });
    expect(await screen.findByText(/settle timeout \(did not reach target in 30s, observed none\)/)).toBeInTheDocument();
  });

  it("stops claiming motion once the capability's settle_s has passed without a result", async () => {
    const quick = { ...blind, capabilities: [{ ...blind.capabilities[0], settle_s: 1 }] };
    server.use(route("post", "/devices/{endpoint_id}/set", () => ({ json: { ok: true, status: "moving" } })), ...baseRoutes("admin", [quick]));
    await openApp();
    moveSlider("position", 1);
    expect(await screen.findByText(/settling \(up to 1s\)/)).toBeInTheDocument();
    expect(await screen.findByText(/settling \(no result after 1s\)/, undefined, { timeout: 2500 })).toBeInTheDocument();
  });

  it("a confirmation_required body without the server summary is an error, not a client-written prompt", async () => {
    server.use(route("post", "/devices/{endpoint_id}/set", () => ({ status: 409, json: { error: "confirmation_required" } })), ...baseRoutes());
    const { user } = await openApp();
    await user.selectOptions(screen.getByRole("combobox", { name: "lock" }), "unlocked");
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
    await user.selectOptions(screen.getByRole("combobox", { name: "lock" }), "unlocked");
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
});
