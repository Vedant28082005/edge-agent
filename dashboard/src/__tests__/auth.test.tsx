import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { session } from "../api/client";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes } from "../test/fixtures";
import { FakeWebSocket, until } from "../test/fakes";

describe("auth", () => {
  it("signs in with the contract token pair and shows devices", async () => {
    let body: unknown;
    server.use(
      route("post", "/auth/login", async ({ request }) => {
        body = await request.json();
        return { json: { access_token: "a", refresh_token: "r", role: "member" } };
      }),
      ...baseRoutes("member"),
    );
    render(<App />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Username"), "vedant");
    await user.type(screen.getByLabelText("Password"), "pw-for-test");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await until(() => FakeWebSocket.instances.length > 0);
    expect(await screen.findByRole("heading", { name: /Devices/ })).toBeInTheDocument();
    expect(body).toEqual({ username: "vedant", password: "pw-for-test" });
    expect(screen.queryByRole("button", { name: "admin" })).not.toBeInTheDocument(); // member sees no Admin tab
  });

  it("shows an error for bad credentials and stays signed out", async () => {
    server.use(route("post", "/auth/login", () => ({ status: 401, json: { error: "unauthorized" } })));
    render(<App />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Username"), "x");
    await user.type(screen.getByLabelText("Password"), "y");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("sign in again");
    expect(session.authed).toBe(false);
  });

  it("logs out back to the sign-in form", async () => {
    server.use(...baseRoutes("admin"));
    session.set({ access_token: "a", refresh_token: "r", role: "admin" });
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Log out" }));
    await waitFor(() => expect(screen.getByRole("form", { name: "Sign in" })).toBeInTheDocument());
    expect(sessionStorage.getItem("edge.refresh")).toBeNull();
  });

  it("first-time setup explains the CLI (the contract has no bootstrap endpoint)", async () => {
    render(<App />);
    await userEvent.setup().click(screen.getByRole("button", { name: "First-time setup" }));
    expect(screen.getByText(/bootstrap-admin/)).toBeInTheDocument();
  });

  it("refreshes an expired access token once and retries", async () => {
    let calls = 0;
    server.use(
      route("get", "/devices", () => (++calls === 1 ? { status: 401, json: { error: "unauthorized" } } : { json: [] })),
      ...baseRoutes("admin"),
    );
    session.set({ access_token: "old", refresh_token: "r", role: "admin" });
    render(<App />);
    expect(await screen.findByText("No devices visible to you.")).toBeInTheDocument();
    expect(calls).toBe(2);
  });
});
