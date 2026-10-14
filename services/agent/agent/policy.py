"""needs_confirmation(action, trigger): the one place that decides whether an action may run, must be confirmed, or may
only be announced.

* `risk: confirm` + a user trigger  -> CONFIRM (always asks; there is no "remember my choice").
* `risk: confirm` + any other trigger (rule, perception escalation, proactive) -> NOTIFY_ONLY: never executed, never a
  prompt, a notification is sent instead (contracts/agent-events.md).
* `risk: confirm` + a guest user -> DENY (guests can never obtain a confirmation; the gateway enforces it too).
* everything else -> ALLOW (the hub still validates)."""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Decision(enum.Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    NOTIFY_ONLY = "notify_only"
    DENY = "deny"


@dataclass(frozen=True)
class Trigger:
    kind: str                         # user | rule | perception | proactive
    ref: str = ""                     # rule id, source id, ...
    user_id: str | None = None
    guest: bool = False

    @property
    def is_user(self) -> bool:
        return self.kind == "user"


@dataclass(frozen=True)
class Action:
    tool: str
    args: dict
    risk: str = "normal"              # normal | confirm (from the registry, or learned from a hub refusal)


def needs_confirmation(action: Action, trigger: Trigger) -> Decision:
    if action.risk != "confirm":
        return Decision.ALLOW
    if not trigger.is_user:
        return Decision.NOTIFY_ONLY
    if trigger.guest:
        return Decision.DENY
    return Decision.CONFIRM
