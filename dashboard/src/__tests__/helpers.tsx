import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { session } from "../api/client";
import { FakeWebSocket, until } from "../test/fakes";
import { tokens } from "../test/fixtures";
import type { Role } from "../api/types";

export async function openApp(role: Role = "admin") {
  session.set(tokens(role));
  render(<App />);
  await until(() => FakeWebSocket.instances.length > 0);
  act(() => FakeWebSocket.last.open());
  await waitFor(() => expect(screen.queryByText("Loading…")).not.toBeInTheDocument());
  return { user: userEvent.setup(), ws: FakeWebSocket.last };
}

export const push = (ws: FakeWebSocket, msg: unknown) => act(() => ws.push(msg));
