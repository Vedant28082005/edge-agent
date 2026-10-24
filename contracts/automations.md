# Automations (rules engine), contract v1

A bounded, closed rule layer in the gateway. **No LLM runs at rule time.** Schemas and routes live in `openapi/gateway.yaml` (tag `automations`, `AutomationRule*`, `Notification*`, `/admin/automations/kill-switch`); this file states the invariants. Everything here is a contract: changing a rule field, trigger, action, outcome or notification level needs a version bump (this header, `gateway.yaml` `info.version`) and new samples.

## Rule

`rule_id` (`ru_` + 8 hex), `name` (48), `owner` (user id), `enabled`, `trigger`, optional `condition`, `action`, `cooldown_s` (10..86400), `max_runs_per_day` (1..1000), `status` (`proposed|approved|disabled`), `created_by` (`user|llm`), plus server-set `approved_by/at`, `created_at/updated_at`, read-only `rooms` and `broken`.

- **Triggers (exactly five):** `device_state_change {endpoint_id, capability, to?, from?}`, `device_offline {endpoint_id, for_s?}`, `duration_in_state {endpoint_id, capability, value, for_s>=10}`, `time_of_day {at: HH:MM, days?}`, `schedule {every_s>=60, between?, days?}`. Time is the household time zone.
- **Condition (optional):** `{all: [...]}`, at most 4 clauses, AND only, no nesting: `device_state {endpoint_id, capability, op, value}` or `time_window {after, before, days?}`.
- **Action (exactly one):** `notify {level?: info|warning, text<=120}` or `set_state {endpoint_id, capability, value}`. No scenes, no tool calls, no free-form code, no templates.
- A rule runs only while `status = approved` and `enabled` and not `broken` and the kill switch is off. `enabled` is true exactly when `status` is `approved` (the schema enforces both directions).
- Evaluation order per trigger: kill switch -> status -> owner scope (`blocked_scope`) -> condition -> `risk` check (`blocked_confirm`) -> cooldown -> daily limit -> execute. Skips and blocks are recorded in the run history, never silent.

## Invariants (normative)

1. **Automations never target `risk: confirm` endpoints.** A `set_state` action on one is refused at save (409 `confirm_endpoint_not_allowed`), at approval, and again at every run (`blocked_confirm`: nothing executes, **no `confirmation_request` is created**, a `action_blocked` notification is sent instead). Such a rule can only `notify`. There is no admin opt-in and no standing approval. Reads in triggers/conditions of a `risk: confirm` endpoint are allowed.
2. **LLM-created rules start `proposed` and cannot run until a user approves them.** The LLM has no tool and no route for rules (`mcp-tools.md` stays at five tools; it may only propose JSON in its reply). A user saves that JSON with `source: llm_proposal`; the gateway forces `created_by: llm`, `status: proposed`, `enabled: false`. Only `POST /automations/{id}/approve` by an `admin` or a `member` in scope moves it to `approved`, and records `approved_by` (a user id, never `llm` or a service). Clients cannot send `status`, `created_by`, `owner` or approval fields. Any edit of an approved rule drops it back to `proposed`.
3. **Every autonomous action is logged with source `automation:<rule_id>`** in the action log (`ActionLogEntry.source`; `user_id` is `household`), including blocked and failed ones, and each execution links `action_log_id` from its run record. A bare `automation` source is invalid.
4. **Roles.** `admin` and `member` may create, edit, approve, disable, delete and read; `guest` gets 403 on every `/automations*` and `/notifications*` route and never receives rule or automation WebSocket messages. The kill switch is admin only.
5. **Room scope.** Every endpoint a rule reads (trigger, condition) or acts on must be in a room of the creator, checked at save and approval, and **re-checked at every run against the owner's current rooms** (a member who loses a room cannot keep acting there: `blocked_scope`). A member cannot see or approve a rule that touches a room outside their scope; room-less constructs are not expressible (all targets are endpoint ids). Admins are unrestricted.
6. **Global kill switch.** `PUT /admin/automations/kill-switch {automations_enabled:false}` stops all execution, `set_state` and `notify` alike; it is persisted and survives restarts; rules keep their status. Changing it is audit-logged and broadcast as `automations_changed {change: kill_switch}`.
7. **Bounds.** At most 100 rules; an action rate is capped by `cooldown_s` and `max_runs_per_day`; an engine-wide cap of 60 automated `set_state` per hour guards against feedback loops (excess runs are `failed` with `error: rate_limited`). A rule whose action triggers itself is dampened by its own cooldown.
8. **Untrusted strings.** Rule `name` and `notify.text` are user-authored labels, length-limited and displayed as plain text; notification text built by the server uses admin-set endpoint and rule labels only. No device-supplied string is stored in a rule or notification, and none of it is ever placed in an LLM prompt. No secret, `confirmation_token` or token appears in a rule, run record, notification or event.
9. **Registry changes.** On every registry change the gateway re-validates all rules; a rule referencing a forgotten endpoint, a missing capability, a type/range mismatch or a now-`risk: confirm` action target becomes `broken` (never run, never silently deleted), a `rule_broken` notification is sent, and `automations_changed {change: broken}` is pushed. `adminForgetNode` counts affected rules in `ForgetUsage.automations`.

## Notifications

`Notification` = `{notification_id (nt_ + 12 hex), ts, level, source, rule_id?, endpoint_id?, text, read?}`. `level` is closed: `info|warning|action_blocked|rule_proposed|rule_broken|rule_limit|kill_switch` (unknown values display as `info`). `source` is `system` or `automation:<rule_id>`. Persisted 30 days, listed by `GET /notifications`, per-user read state via `POST /notifications/read`.

Live delivery: WebSocket `automation_notification` (the same object without `read`) and `automations_changed {rule_id|null, change, automations_enabled?}` (no rule content; refetch). The legacy `notification {level, text}` message stays for non-automation use. There is no new SSE stream: the dashboard uses `/ws`. The `action_blocked` example in `agent-events.md` is now emitted as `automation_notification`.

## Run history

`GET /automations/{id}/runs`: `AutomationRun {run_id, rule_id, ts, outcome, action_status?, action_log_id?, call_id?, error?, rule_deleted?}`; `outcome` is `fired|skipped_condition|skipped_cooldown|skipped_daily_limit|skipped_offline|blocked_confirm|blocked_scope|failed|suppressed_kill_switch`; `error` is a code from `mcp-tools.md` or `rate_limited`, never free text. Retained 30 days.

## Out of scope

Standing approvals for `risk: confirm`, rule mining, scenes as actions, multiple actions, OR/nesting, cross-rule dependencies, and the engine's implementation. No change to `agent-internal.yaml`, `device-hub-internal.yaml` or `mcp-tools.md` is needed: the engine lives in the gateway and executes through the same hub path as `POST /devices/{id}/set` with `risk: confirm` always failing closed.

Contract version: **1**
