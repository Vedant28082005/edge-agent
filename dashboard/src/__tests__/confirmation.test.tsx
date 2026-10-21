import { screen, waitFor, within } from "@testing-library/react";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes } from "../test/fixtures";
import { openApp, push } from "./helpers";

const req = {
  type: "confirmation_request", request_id: "req-1", summary: "Unlock front door?",
  action: { type: "set_state", endpoint_id: "ep_00000003", capability: "lock", value: "unlocked" },
  action_hash: "f".repeat(64), expires_in_s: 60,
};

async function withRequest() {
  const o = await openApp();
  push(o.ws, req);
  const card = await screen.findByRole("group", { name: "Confirmation required" });
  return { ...o, card };
}

describe("confirmation flow (agent flow)", () => {
  it("shows the server summary and action verbatim, never a hash, and approves by request_id only", async () => {
    let body: unknown;
    server.use(route("post", "/confirmations", async ({ request }) => { body = await request.json(); return { json: { status: "delivered_to_agent", action_hash: "h", expires_at: 1 } }; }), ...baseRoutes());
    const { user, card } = await withRequest();
    expect(within(card).getByText("Unlock front door?")).toBeInTheDocument();
    expect(card).toHaveTextContent("ep_00000003");
    expect(document.body.textContent).not.toContain("f".repeat(64));
    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(body).toEqual({ request_id: "req-1" }));
    expect(await within(card).findByText(/Approved/)).toBeInTheDocument();
  });

  it("cancel calls decline and says nothing was executed", async () => {
    let hit = "";
    server.use(route("post", "/confirmations/{request_id}/decline", ({ params }) => { hit = String(params.request_id); return { status: 204 }; }), ...baseRoutes());
    const { user, card } = await withRequest();
    await user.click(within(card).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(hit).toBe("req-1"));
    expect(await within(card).findByText(/Declined. Nothing was executed/)).toBeInTheDocument();
  });

  it.each([
    ["request_expired", /expired/i],
    ["request_already_handled", /already handled/i],
    ["action_changed", /action changed/i],
  ])("409 %s shows a clear message", async (code, re) => {
    server.use(route("post", "/confirmations", () => ({ status: 409, json: { error: code } })), ...baseRoutes());
    const { user, card } = await withRequest();
    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent(re);
  });

  it("403 (not your request / guest) is reported", async () => {
    server.use(route("post", "/confirmations", () => ({ status: 403, json: { error: "forbidden" } })), ...baseRoutes());
    const { user, card } = await withRequest();
    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent("not allowed");
  });

  it("token burned / hash mismatch outcomes from consume are explained, not shown raw", async () => {
    server.use(route("post", "/devices/{endpoint_id}/set", () => ({ json: { ok: false, error: "hash_mismatch" } })), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "hall" }));
    await user.click(screen.getByRole("button", { name: "Unlock…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/burned/i);
  });

  it("expired via WebSocket offers Retry, which creates a new request", async () => {
    let retried = "";
    server.use(route("post", "/confirmations/{request_id}/retry", ({ params }) => { retried = String(params.request_id); return { status: 202, json: { request_id: "req-2" } }; }), ...baseRoutes());
    const { user, ws, card } = await withRequest();
    push(ws, { type: "confirmation_expired", request_id: "req-1", summary: "Unlock front door?", reason: "no_response" });
    expect(await within(card).findByText(/Expired. Nothing was executed/)).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(retried).toBe("req-1"));
    push(ws, { ...req, request_id: "req-2" });
    expect(await screen.findAllByRole("group", { name: "Confirmation required" })).toHaveLength(1); // old card is replaced by the new one
    expect(screen.getByRole("button", { name: "Confirm" })).toBeEnabled();
  });

  it("retry refused with 409 request_not_retryable is explained", async () => {
    server.use(route("post", "/confirmations/{request_id}/retry", () => ({ status: 409, json: { error: "request_not_retryable" } })), ...baseRoutes());
    const { user, ws, card } = await withRequest();
    push(ws, { type: "confirmation_declined", request_id: "req-1", summary: "Unlock front door?" });
    await user.click(await within(card).findByRole("button", { name: "Retry" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent(/cannot be retried/i);
  });
});
