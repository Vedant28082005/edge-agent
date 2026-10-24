import { ApiError } from "./api/client";

const MESSAGES: Record<string, string> = {
  // confirmation conflicts (gateway.yaml ConfirmationConflict)
  request_expired: "This request expired. Nothing was executed. Use Retry to ask again.",
  request_already_handled: "This request was already handled. Nothing more was executed.",
  request_not_retryable: "This request cannot be retried. Start a new action instead.",
  action_changed: "The action changed since it was requested (for example the device was re-configured). Nothing was executed; start a new request.",
  // token consumption outcomes (ConsumeDenied reasons), as they may surface in tool results / set errors
  expired: "The approval expired before it was used. Nothing was executed. Ask again.",
  used: "This approval was already used (burned). Nothing was executed. Ask again.",
  hash_mismatch: "The approval did not match the action, so it was burned. Nothing was executed. Ask again.",
  unknown: "The approval was not recognised. Nothing was executed. Ask again.",
  confirmation_required: "This device needs your confirmation first.",
  endpoint_offline: "The device is offline.",
  rate_limited: "Too many requests. Wait a moment and try again.",
  too_many_uploads: "Too many uploads held. Wait a moment and try again.",
  upstream_error: "A backend service returned an error.",
  upstream_timeout: "A backend service did not answer in time.",
  vision_unavailable: "Vision is unavailable right now.",
  bad_media_ref: "The attachment expired or was already used. Attach it again.",
  too_large: "That upload is too large.",
  unsupported_media_type: "That file type is not accepted.",
  llm_busy: "The assistant is busy. Try again shortly.",
  budget_exceeded: "The assistant ran out of steps for this request.",
  bad_request: "The request was not valid.",
  unauthorized: "Session expired, sign in again.",
  // automations (gateway.yaml tag automations)
  bad_rule: "That rule is not valid for the devices it uses. Check the values and try again.",
  confirm_endpoint_not_allowed: "Automations cannot control devices that need your confirmation, such as locks. Use a notification instead.",
  rule_limit: "The rule limit is reached. Delete a rule first.",
  rule_broken: "This rule uses a device that was removed or changed. Edit it before approving.",
  out_of_scope: "That rule uses a room you do not have access to.",
};

export function explain(e: unknown): string {
  if (e instanceof ApiError) {
    const base = MESSAGES[e.code];
    if (e.status === 403 && !base) return "You are not allowed to do that.";
    if (e.status === 404 && !base) return "Not found (it may have expired or been removed).";
    if (e.status === 429 && e.retryAfter) return `${base ?? MESSAGES.rate_limited} Retry in ${e.retryAfter}s.`;
    return base ?? `Error: ${e.code}`;
  }
  if (typeof e === "string") return MESSAGES[e] ?? e;
  return (e as Error)?.message ?? "Something went wrong.";
}
