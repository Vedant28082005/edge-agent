// Aliases over the generated contract types. Never hand-write API shapes here.
import type { components } from "./schema.gen";

type S = components["schemas"];
export type Role = S["Role"];
export type Endpoint = S["Endpoint"];
export type Capability = S["Capability"];
export type SetResult = S["SetResult"];
export type Node = S["Node"];
export type User = S["User"];
export type Scene = S["Scene"];
export type TokenPair = S["TokenPair"];
export type ForgetUsage = S["ForgetUsage"];
export type ApiErrorBody = S["Error"];
export type Risk = S["Risk"];
