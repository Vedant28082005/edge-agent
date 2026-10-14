export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static reset() { FakeWebSocket.instances = []; }
  static get last() { return FakeWebSocket.instances[FakeWebSocket.instances.length - 1]; }

  onopen: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string, public protocols: string[] | string = []) {
    FakeWebSocket.instances.push(this);
  }
  open() { this.onopen?.(); }
  send(m: unknown) { void m; }
  push(msg: unknown) { this.onmessage?.({ data: JSON.stringify(msg) }); }
  serverClose(code: number) { this.closed = true; this.onclose?.({ code }); }
  close() { if (!this.closed) { this.closed = true; this.onclose?.({ code: 1000 }); } }
}

export const tick = (ms = 0) => new Promise<void>((r) => setTimeout(r, ms));
export async function until(fn: () => boolean, ms = 2000) {
  const t0 = Date.now();
  while (!fn()) {
    if (Date.now() - t0 > ms) throw new Error("until: timeout");
    await tick(5);
  }
}
