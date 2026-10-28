import { screen } from "@testing-library/react";
import { server } from "../test/server";
import { baseRoutes } from "../test/fixtures";
import { openApp, push } from "./helpers";

describe("activity feed", () => {
  it("lists live WebSocket events", async () => {
    server.use(...baseRoutes());
    const { user, ws } = await openApp();
    push(ws, { type: "notification", level: "action_blocked", text: "Rule wanted to unlock the front door" });
    await user.click(screen.getByRole("button", { name: "Activity" }));
    expect(await screen.findByText(/action_blocked: Rule wanted to unlock the front door/)).toBeInTheDocument();
  });
});
