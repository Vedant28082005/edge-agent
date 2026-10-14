import { screen, waitFor, within } from "@testing-library/react";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes } from "../test/fixtures";
import type { Node, User } from "../api/types";
import { openApp } from "./helpers";

const pending: Node = {
  node_id: "esp32-a1b2c3d4e5f6", hw: "esp32-devkit", fw: "0.1.0", status: "pending",
  endpoints: [{ endpoint_id: "ep_ffffffff", local_id: "ch1", name: "", room: "", type: "fan", online: true, enabled: true, capabilities: [] }],
};
const users: User[] = [
  { user_id: "u1", username: "alice", role: "member", rooms: ["bedroom"] },
  { user_id: "u2", username: "guest1", role: "guest", rooms: ["hall"] },
];

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

  it("approves a pending node with name, room, tags and risk", async () => {
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
    await user.type(screen.getByLabelText("Name"), "Bedroom fan");
    await user.type(screen.getByLabelText("Room"), "bedroom");
    await user.type(screen.getByLabelText("Tags"), "night, cool");
    await user.selectOptions(screen.getByLabelText("Risk"), "confirm");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(body).toEqual({ endpoints: [{ local_id: "ch1", name: "Bedroom fan", room: "bedroom", aliases: [], tags: ["night", "cool"], risk: "confirm" }] }));
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
    expect(screen.getByLabelText("rooms of guest1")).toHaveValue("hall");
    await user.type(screen.getByLabelText("New username"), "visitor");
    await user.type(screen.getByLabelText("New password"), "long-enough-pw");
    await user.selectOptions(screen.getByLabelText("New role"), "guest");
    await user.type(screen.getByLabelText("New rooms"), "living, hall");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(body).toEqual({ username: "visitor", password: "long-enough-pw", role: "guest", rooms: ["living", "hall"] }));
  });
});
