// MSW helpers derived from the contract: every mocked route must exist in gateway.yaml with that method, and every
// status a handler returns must be declared for that operation. A contract change that removes a route or status
// therefore fails the tests instead of silently drifting.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";
import { http, HttpResponse, type HttpHandler } from "msw";

type Spec = { paths: Record<string, Record<string, { responses?: Record<string, unknown> }>> };
const spec = parse(readFileSync(resolve(__dirname, "../../../contracts/openapi/gateway.yaml"), "utf8")) as Spec;

type Method = "get" | "post" | "put" | "patch" | "delete";

function op(method: Method, path: string) {
  const o = spec.paths[path]?.[method];
  if (!o) throw new Error(`contract has no ${method.toUpperCase()} ${path}`);
  return o;
}

export function declared(method: Method, path: string, status: number): void {
  const r = op(method, path).responses ?? {};
  const resolved = r[String(status)] ?? (status === 200 ? undefined : undefined);
  if (!resolved) throw new Error(`contract declares no ${status} for ${method.toUpperCase()} ${path}`);
}

const colon = (p: string) => p.replace(/\{(\w+)\}/g, ":$1");

type Reply = { status?: number; json?: unknown; headers?: Record<string, string>; body?: BodyInit };
type Resolver = (ctx: { params: Record<string, string | readonly string[] | undefined>; request: Request }) => Reply | Promise<Reply>;

export function route(method: Method, path: string, resolver: Resolver): HttpHandler {
  op(method, path);
  return http[method](colon(path), async (info) => {
    const r = await resolver({ params: info.params, request: info.request });
    const status = r.status ?? 200;
    declared(method, path, status);
    if (r.body !== undefined) return new HttpResponse(r.body, { status, headers: r.headers });
    if (r.json === undefined) return new HttpResponse(null, { status, headers: r.headers });
    return HttpResponse.json(r.json as object, { status, headers: r.headers });
  });
}
