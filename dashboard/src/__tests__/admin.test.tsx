import { screen, waitFor, within } from "@testing-library/react";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes } from "../test/fixtures";
import type { ActionLogEntry, Node, User } from "../api/types";
import { openApp } from "./helpers";

const pending: Node = {
  node_id: "esp32-a1b2c3d4e5f6", hw: "esp32-devkit", fw: "0.1.0", status: "pending",
  endpoints: [{ endpoint_id: "ep_ffffffff", local_id: "ch1", name: "", room: "", type: "fan", online: true, enabled: true, capabilities: [] }],
};
const users: User[] = [
  { user_id: "u1", username: "alice", role: "member", rooms: ["bedroom"] },
  { user_id: "u2", username: "guest1", role: "guest", rooms: ["hall"] },
];

const entry = (id: number, over: Partial<ActionLogEntry> = {}): ActionLogEntry => ({
  id, ts: 1_700_000_000 + id, user_id: "u1", source: "direct", kind: "set_state", endpoint_id: "ep_00000001",
  capability: "power", value: true, status: "done", elapsed_ms: 120, ...over,
});

describe("admin", () => {
  it("hides the Admin tab for non-admins and never calls /admin/*", async () => {
    let adminCalls = 0;
    server.use(route("get", "/admin/nodes", () => { adminCalls++; return { status: 403, json: { error: "forbidden" } }; }), ...baseRoutes("guest"));
    await openApp("guest");
    expect(screen.queryByRole("button", { name: "admin" })).not.toBeInTheDocument();
    expect(adminCalls).toBe(0);
  });

  it("renders only what the API returns when the server refuses (403)", async () => {
    server.use(
            route("get", "/admin/nodes", () => ({ status: 403, json: { error: "forbidden" } })),
      route("get", "/admin/users", () => ({ status: 403, json: { error: "forbidden" } })),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "admin" }));
    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent("not allowed");
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
  });

  it("approves a pending node with a name pre-filled from the announced type, and a room", async () => {
    let body: unknown;
    server.use(
            route("get", "/admin/nodes", () => ({ json: [pending] })),
      route("get", "/admin/users", () => ({ json: users })),
      route("post", "/admin/nodes/{node_id}/approve", async ({ request }) => { body = await request.json(); return { json: [] }; }),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "admin" }));
    await user.click(await screen.findByRole("button", { name: "Approve…" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Fan"); // pre-filled from the announced endpoint type
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled(); // room still empty
    expect(screen.queryByText("ch1")).not.toBeInTheDocument(); // local_id is an internal id, never shown
    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Bedroom fan");
    await user.type(screen.getByLabelText("Room"), "bedroom");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(body).toEqual({ endpoints: [{ local_id: "ch1", name: "Bedroom fan", room: "bedroom" }] }));
  });

  it("approve stays disabled until every name and room is filled, and shows the server's error", async () => {
    server.use(
            route("get", "/admin/nodes", () => ({ json: [pending] })),
      route("get", "/admin/users", () => ({ json: users })),
      route("post", "/admin/nodes/{node_id}/approve", () => ({ status: 403, json: { error: "forbidden", message: "This node was already approved by someone else." } })),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "admin" }));
    await user.click(await screen.findByRole("button", { name: "Approve…" }));
    await user.clear(screen.getByLabelText("Name"));
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    await user.type(screen.getByLabelText("Name"), "Bedroom fan");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled(); // room still empty
    await user.type(screen.getByLabelText("Room"), "bedroom");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("This node was already approved by someone else.")).toBeInTheDocument();
  });

  it("forget: first call returns the usage list, only a second explicit confirm sends confirm=true", async () => {
    const urls: string[] = [];
    server.use(
            route("get", "/admin/nodes", () => ({ json: [{ ...pending, status: "online" }] })),
      route("get", "/admin/users", () => ({ json: users })),
      route("delete", "/admin/nodes/{node_id}", ({ request }) => {
        urls.push(new URL(request.url).search);
        return urls.length === 1 ? { status: 409, json: { scenes: 2, automations: 1, message: "Used by 2 scenes, 1 automation" } } : { status: 204 };
      }),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "admin" }));
    await user.click(await screen.findByRole("button", { name: "Forget" }));
    const dlg = await screen.findByRole("alertdialog");
    expect(dlg).toHaveTextContent("Used by 2 scenes, 1 automation");
    expect(urls).toEqual([""]);
    await user.click(within(dlg).getByRole("button", { name: "Forget anyway" }));
    await waitFor(() => expect(urls).toEqual(["", "?confirm=true"]));
  });

  it("lists users and creates a guest limited to rooms", async () => {
    let body: unknown;
    server.use(
            route("get", "/admin/nodes", () => ({ json: [] })),
      route("get", "/admin/users", () => ({ json: users })),
      route("post", "/admin/users", async ({ request }) => { body = await request.json(); return { status: 201, json: users[1] }; }),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "admin" }));
    expect(await screen.findByText("alice")).toBeInTheDocument();
    // Rooms are a checklist (role segmented + room checklist), populated from rooms currently in use.
    const guestRooms = screen.getByRole("group", { name: "rooms of guest1" });
    expect(within(guestRooms).getByRole("checkbox", { name: "hall" })).toBeChecked();
    expect(within(guestRooms).getByRole("checkbox", { name: "bedroom" })).not.toBeChecked();

    await user.type(screen.getByLabelText("New username"), "visitor");
    await user.type(screen.getByLabelText("New password"), "long-enough-pw");
    const newRole = screen.getByRole("group", { name: "New role" });
    await user.click(within(newRole).getByRole("button", { name: "guest" }));
    const newRooms = screen.getByRole("group", { name: "New rooms" });
    await user.click(within(newRooms).getByRole("checkbox", { name: "hall" }));
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(body).toEqual({ username: "visitor", password: "long-enough-pw", role: "guest", rooms: ["hall"] }));
  });

  it("action log: filters go to the query, cursor pages append, errors are explained", async () => {
    const urls: URL[] = [];
    server.use(
      route("get", "/admin/nodes", () => ({ json: [] })),
      route("get", "/admin/users", () => ({ json: users })),
      route("get", "/admin/action-log", ({ request }) => {
        const u = new URL(request.url);
        urls.push(u);
        if (u.searchParams.get("user_id") === "bad") return { status: 400, json: { error: "bad_request" } };
        if (u.searchParams.get("cursor") === "c1") return { json: { items: [entry(1, { status: "settled" })], next_cursor: null } };
        if (u.searchParams.get("status") === "denied") return { json: { items: [entry(9, { status: "denied", error: "forbidden" })], next_cursor: null } };
        return { json: { items: [entry(3), entry(2, { source: "agent", kind: "run_scene", scene_id: "movie", endpoint_id: null, capability: null, value: null, status: "blocked", error: "confirmation_required" })], next_cursor: "c1" } };
      }),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "admin" }));
    const table = await screen.findByRole("table", { name: "Action log" });
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(3)); // header + 2
    const sceneRow = within(table).getAllByRole("row")[2]; // plain name, not the raw `kind` enum
    expect(sceneRow.textContent).toContain("Run scene");
    expect(sceneRow.textContent).toContain("movie");
    expect(urls[0].search).toBe("");

    await user.click(screen.getByRole("button", { name: "Load 50 more" }));
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(4));
    expect(urls[1].searchParams.get("cursor")).toBe("c1");
    expect(screen.queryByRole("button", { name: /Load \d+ more/ })).not.toBeInTheDocument(); // next_cursor null
    expect(screen.getByText("Showing 3 (all matching)")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter status"), "denied");
    await user.type(screen.getByLabelText("Filter endpoint"), "ep_00000003");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(2)); // replaced, not appended
    const last = urls[urls.length - 1];
    expect(last.searchParams.get("status")).toBe("denied");
    expect(last.searchParams.get("endpoint_id")).toBe("ep_00000003");
    expect(last.searchParams.has("cursor")).toBe(false);

    await user.type(screen.getByLabelText("Filter user"), "bad");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText("The request was not valid.")).toBeInTheDocument();
  });

  it("action log: 403 is reported", async () => {
    server.use(
      route("get", "/admin/nodes", () => ({ json: [] })),
      route("get", "/admin/users", () => ({ json: users })),
      route("get", "/admin/action-log", () => ({ status: 403, json: { error: "forbidden" } })),
      ...baseRoutes("admin"),
    );
    const { user } = await openApp("admin");
    await user.click(screen.getByRole("button", { name: "admin" }));
    expect((await screen.findAllByRole("alert")).some((a) => a.textContent === "You are not allowed to do that.")).toBe(true);
  });
});
