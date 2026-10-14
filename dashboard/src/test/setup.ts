import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./server";
import { session } from "../api/client";
import { FakeWebSocket } from "./fakes";
import { wsFactory } from "../api/ws";

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
  // MSW makes globalThis.WebSocket read-only on listen(), so tests inject the fake through the factory hook.
  wsFactory.make = (u, p) => new FakeWebSocket(u, p) as unknown as WebSocket;
});
afterEach(() => {
  server.resetHandlers();
  session.clear();
  try { sessionStorage.clear(); localStorage.clear(); } catch { /* */ }
  FakeWebSocket.reset();
});
afterAll(() => server.close());

