// Message shapes that gateway.yaml describes only in prose (`/ws` description and agent-events.md).
// These are the only hand-written wire types; the contract has no schema for them (reported as a gap).
export type ConfirmationAction = Record<string, unknown> & { type: string };

export type ConfirmationRequest = {
  request_id: string; summary: string; action: ConfirmationAction; expires_in_s: number;
};

export type WsMessage =
  | { type: "state"; endpoint_id: string; values: Record<string, unknown>; ts?: number }
  | { type: "registry_changed"; version?: unknown; reason?: string; endpoint_ids: string[] }
  | { type: "scenes_changed"; scene_id: string; change: "created" | "updated" | "deleted" }
  | { type: "notification"; level: string; text: string; role?: string }
  | { type: "async_result"; ref: string; kind: string; text: string; latency_ms?: number }
  | { type: "perception"; [k: string]: unknown }
  | ({ type: "confirmation_request" } & ConfirmationRequest)
  | { type: "confirmation_declined"; request_id: string; summary: string }
  | { type: "confirmation_expired"; request_id: string; summary: string; reason?: string }
  | { type: "action_settled"; call_id?: string; endpoint_id: string; capability: string; value: unknown; observed: unknown; elapsed_ms: number }
  | { type: "action_settle_timeout"; call_id?: string; endpoint_id: string; capability: string; value: unknown; observed: unknown; settle_s: number };

export type SseEvent = { event: string; data: Record<string, unknown> };
