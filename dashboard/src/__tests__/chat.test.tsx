import { act, fireEvent, screen } from "@testing-library/react";
import { vi } from "vitest";
import { session } from "../api/client";
import { createSseParser } from "../api/sse";
import { server } from "../test/server";
import { route } from "../test/contract";
import { baseRoutes } from "../test/fixtures";
import { chatStore } from "../chatStore";
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
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "light on");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Turned it on.")).toBeInTheDocument();
    expect(screen.getByText("light on")).toBeInTheDocument();
    expect(screen.getByText("Used 1 tool")).toBeInTheDocument();
    expect(screen.getByText("Turned on Ceiling light")).toBeInTheDocument();
    expect(screen.queryByText(/ep_00000001|set_state/)).not.toBeInTheDocument(); // no ids or raw tool names
    expect(screen.getByText("Quick action · <1 s")).toBeInTheDocument();
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
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "light on");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Used 1 tool")).toBeInTheDocument();
    expect(screen.queryByText("No reply")).not.toBeInTheDocument();
  });

  it("shows 'No reply' for a final turn with no text and no tool calls", async () => {
    server.use(
      route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "", route: "fast", latency_ms: 5 }]]) })),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "hi");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("No reply")).toBeInTheDocument();
  });

  it("shows a stream error event with a clear message", async () => {
    server.use(route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["error", { code: "llm_busy", message: "m" }]]) })), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "hi");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("busy");
    expect(document.querySelector(".msg.is-error")).toBeTruthy();
  });

  it("keeps chat history across navigation and a reload, but clears it on log out", async () => {
    server.use(
      route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "Turned it on.", route: "fast", latency_ms: 1 }]]) })),
      ...baseRoutes(),
    );
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "light on");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Turned it on.")).toBeInTheDocument();

    // navigating to another tab and back keeps it (module-level store)
    await user.click(screen.getByRole("button", { name: "Devices" }));
    await user.click(screen.getByRole("button", { name: "Chat" }));
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

  const hanging = (events: Array<[string, unknown]>) => route("post", "/chat", ({ request }) => {
    const enc = new TextEncoder();
    const body = new ReadableStream({
      start(c) {
        c.enqueue(enc.encode(sse(events)));
        request.signal.addEventListener("abort", () => { try { c.error(new DOMException("aborted", "AbortError")); } catch { /* closed */ } });
      },
    });
    return { headers: { "Content-Type": "text/event-stream" }, body };
  });

  it("Send becomes Stop while streaming; Stop aborts, marks the turn and re-enables input", async () => {
    server.use(hanging([["status", { text: "working" }]]), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "slow thing");
    await user.click(screen.getByRole("button", { name: "Send" }));
    const stop = await screen.findByRole("button", { name: "Stop" });
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toBeEnabled();
    await user.click(stop);
    expect(await screen.findByText("Stopped. Actions already done are kept.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toHaveFocus();
  });

  it("shows a typing indicator with elapsed seconds that keepalive status events update", async () => {
    server.use(hanging([["status", { text: "still checking" }]]), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "hi");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("still checking")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0s");
    expect(await screen.findByText("1s", {}, { timeout: 2500 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
  });

  it("Enter sends, Shift+Enter inserts a newline, and Clear chat empties the log", async () => {
    server.use(route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "Done.", route: "llm", latency_ms: 3 }]]) })), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    const box = screen.getByLabelText("Message") as HTMLTextAreaElement;
    await user.type(box, "line1{Shift>}{Enter}{/Shift}line2");
    expect(box.value).toBe("line1\nline2");
    await user.type(box, "{Enter}");
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    expect(screen.getByText("Thought for <1 s")).toBeInTheDocument();
    expect(box.value).toBe("");
    expect(box).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Chat options" }));
    await user.click(screen.getByRole("menuitem", { name: "Clear chat" }));
    expect(screen.queryByText("Done.")).not.toBeInTheDocument();
    expect(chatStore.getMsgs()).toEqual([]);
  });

  it("stops auto-scrolling when the user scrolls up and offers Jump to latest", async () => {
    server.use(route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "ok", route: "fast", latency_ms: 1 }]]) })), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    const log = document.querySelector(".chat-scroll") as HTMLElement;
    let top = 0;
    Object.defineProperty(log, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(log, "clientHeight", { configurable: true, value: 300 });
    Object.defineProperty(log, "scrollTop", { configurable: true, get: () => top, set: (v: number) => { top = v; } });
    await user.type(screen.getByLabelText("Message"), "one{Enter}");
    await screen.findByText("ok");
    expect(top).toBe(1000); // followed the newest message
    top = 100; fireEvent.scroll(log);
    const jump = await screen.findByRole("button", { name: "Jump to latest" });
    act(() => chatStore.setMsgs((m) => [...m, { kind: "assistant", text: "later", final: true, tools: [] }])); // a streamed event
    expect(top).toBe(100); // the user's position is respected
    expect(screen.getByRole("button", { name: "Jump to latest" })).toBeInTheDocument();
    await user.click(jump);
    expect(top).toBe(1000);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).not.toBeInTheDocument();
  });

  it("styles error and clarifying replies distinctly and shows route and time subtly", async () => {
    server.use(route("post", "/chat", () => ({ headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "Which light?", route: "llm", latency_ms: 9 }]]) })), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "turn on{Enter}");
    await screen.findByText("Which light?");
    expect(document.querySelector(".msg.is-clarify")).toBeTruthy();
    expect(document.querySelector(".meta time")).toBeTruthy();
  });

  it("renders streamed tokens as they arrive with a caret, never as HTML, then swaps in the final text", async () => {
    server.use(hanging([["token", { text: "Hello <b>there</b>" }], ["token", { text: "\nsecond line" }]]), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    await user.type(screen.getByLabelText("Message"), "hi{Enter}");
    const reply = await screen.findByText(/second line/);
    expect(reply.textContent).toBe("Hello <b>there</b>\nsecond line"); // literal text, line break kept
    expect(reply.querySelector("b")).toBeNull();
    expect(reply.querySelector(".caret")).toBeTruthy();
    expect(screen.queryByRole("status")).not.toBeInTheDocument(); // typing indicator only before the first token
    await user.click(screen.getByRole("button", { name: "Stop" }));
  });

  it("composer: Send is disabled when empty, Enter does not send an empty box, and Stop replaces Send while running", async () => {
    server.use(hanging([["status", { text: "working" }]]), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.type(screen.getByLabelText("Message"), "{Enter}");
    expect(chatStore.getMsgs()).toEqual([]);
    await user.type(screen.getByLabelText("Message"), "go{Enter}");
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
  });

  it("empty state offers suggestions built from the user's real devices; clicking one sends it", async () => {
    let body: unknown;
    server.use(route("post", "/chat", async ({ request }) => { body = await request.json(); return { headers: { "Content-Type": "text/event-stream" }, body: sse([["final", { text: "Done.", route: "fast", latency_ms: 1 }]]) }; }), ...baseRoutes());
    const { user } = await openApp();
    await user.click(screen.getByRole("button", { name: "Chat" }));
    const chip = await screen.findByRole("button", { name: "Turn on Ceiling light" });
    expect(screen.getByRole("button", { name: "Is Front door locked?" })).toBeInTheDocument();
    await user.click(chip);
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    expect(body).toMatchObject({ text: "Turn on Ceiling light" });
  });
});
