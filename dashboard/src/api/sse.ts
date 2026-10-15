import { ApiError, rawFetch } from "./client";
import type { SseEvent } from "./events";

/** Incremental SSE parser (`event:` + `data:` lines, blank-line terminated). */
export function createSseParser(emit: (e: SseEvent) => void) {
  let buf = "";
  let event = "message";
  let data: string[] = [];
  return (chunk: string) => {
    buf += chunk;
    let i: number;
    while ((i = buf.search(/\r?\n/)) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + (buf[i] === "\r" ? 2 : 1));
      if (line === "") {
        // unknown event names fall through the consumer's switch
        if (data.length) {
          try { emit({ event, data: JSON.parse(data.join("\n")) } as SseEvent); } catch { /* malformed event dropped */ }
        }
        event = "message"; data = [];
      } else if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
  };
}

export async function streamChat(
  body: { session_id?: string; text?: string; image_refs?: string[] },
  onEvent: (e: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await rawFetch("/chat", {
    method: "POST", signal, body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
  });
  if (!res.ok || !res.body) {
    let b: { error?: string; message?: string } = {};
    try { b = await res.json(); } catch { /* */ }
    throw new ApiError(res.status, b.error ?? `http_${res.status}`, b.message);
  }
  const parse = createSseParser(onEvent);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parse(dec.decode(value, { stream: true }));
  }
}
