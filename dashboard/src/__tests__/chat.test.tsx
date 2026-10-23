import { screen } from "@testing-library/react";
import { vi } from "vitest";
import { session } from "../api/client";
import { createSseParser } from "../api/sse";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes } from "../test/fixtures";
import { openApp } from "./helpers";

const sse = (events: Array<[string, unknown]>) =>
  events.map(([e, d], i) => `event: ${e}\ndata: ${JSON.stringify({ seq: i, req_id: "r1", ...(d as object) })}\n\n`).join("");

describe("chat", () => {
  it("parser handles split chunks and CRLF", () => {
    const got: string[] = [];
    const p = createSseParser((e) => got.push(`${e.event}:${JSON.stringify(e.data)}`));
    p('event: token\ndata: {"te');
    p('xt":"hi"}\r\n\r\nevent: final\ndata: {"text":"done"}\n\n');
    expect(got).toEqual(['token:{"text":"hi"}', 'final:{"text":"done"}']);
  });

  it("sends a message, streams tokens and shows tool calls compactly", async () => {
    let body: unknown;
    server.use(
            route("post", "/chat", async ({ request }) => {
        body = await request.json();
        return {
          headers: { "Content-Type": "text/event-stream" },
          body: sse([
            ["status", { text: "working" }],
            ["tool_call", { call_id: "c1", tool: "set_state", args: { endpoint_id: "ep_00000001", capability: "power", value: true } }],
            ["tool_result", { call_id: "c1", tool: "set_state", result: { ok: true, status: "done" }, ok: true }],
            ["token", { text: "Turning " }],
            ["token", { text: "it on." }],
            ["final", { text: "Turned it on.", route: "fast", latency_ms: 12 }],
          ]),
        };
      }),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "chat" }));
    await user.type(screen.getByLabelText("Message"), "light on");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Turned it on.")).toBeInTheDocument();
    expect(screen.getByText("light on")).toBeInTheDocument();
    expect(screen.getByText("set_state")).toBeInTheDocument();
    expect(screen.getByText(/✓/)).toBeInTheDocument();
    expect(screen.getByText(/fast · 12 ms/)).toBeInTheDocument();
    expect(body).toMatchObject({ text: "light on" });
    expect((body as { session_id: string }).session_id).toBeTruthy();
  });

  it("shows tool chips even when the final reply text is empty", async () => {
    server.use(
            route("post", "/chat", () => ({
        headers: { "Content-Type": "text/event-stream" },
        body: sse([
          ["tool_call", { call_id: "c1", tool: "set_state", args: { endpoint_id: "ep_00000001", capability: "power", value: true } }],
          ["tool_result", { call_id: "c1", tool: "set_state", result: { ok: true, status: "done" }, ok: true }],
          ["final", { text: "", route: "fast", latency_ms: 5 }],
        ]),
      })),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "chat" }));
    await user.type(screen.getByLabelText("Message"), "light on");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("set_state")).toBeInTheDocument();
    expect(screen.queryByText("No reply")).not.toBeInTheDocument();
  });

  it("shows 'No reply' for a final turn with no text and no tool calls", async () => {
    server.use(
      route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "", route: "fast", latency_ms: 5 }]]) })),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "chat" }));
    await user.type(screen.getByLabelText("Message"), "hi");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("No reply")).toBeInTheDocument();
  });

  it("shows a stream error event with a clear message", async () => {
    server.use(route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["error", { code: "llm_busy", message: "m" }]]) })), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "chat" }));
    await user.type(screen.getByLabelText("Message"), "hi");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("busy");
  });

  it("keeps chat history across navigation and a reload, but clears it on log out", async () => {
    server.use(
      route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "Turned it on.", route: "fast", latency_ms: 1 }]]) })),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "chat" }));
    await user.type(screen.getByLabelText("Message"), "light on");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Turned it on.")).toBeInTheDocument();

    // navigating to another tab and back keeps it (module-level store)
    await user.click(screen.getByRole("button", { name: "devices" }));
    await user.click(screen.getByRole("button", { name: "chat" }));
    expect(screen.getByText("Turned it on.")).toBeInTheDocument();
    expect(screen.getByText("light on")).toBeInTheDocument();

    // a reload re-imports every module fresh; only sessionStorage survives that
    expect(sessionStorage.getItem("edge.chat")).toContain("Turned it on.");
    vi.resetModules();
    const { chatStore: afterReload } = await import("../chatStore");
    expect(afterReload.getMsgs().some((m) => m.kind === "assistant" && m.text === "Turned it on.")).toBe(true);

    // logging out clears it
    session.clear();
    expect(sessionStorage.getItem("edge.chat")).toBeNull();
  });
});
