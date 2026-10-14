"""Per-user conversation window with a rolling summary (ARCHITECTURE section 6, short-term layer).

* The window is chosen by TOKEN budget (est tokens, newest first) and capped at `max_messages` (the prompt builder's own
  cap: the last 1-2 turns); what falls out of it is folded
  into the rolling summary, which has its own token budget (oldest content is dropped first).
* Strict isolation: every read and write goes through `Store` methods that filter on `user_id` (and `session_id`); this
  class never takes a bare session id. Guests have no persistent window (the Store keeps them in a small in-process LRU
  and nothing is summarised).
* Everything returned goes into the prompt TAIL, after the byte-stable prefix, and is already trimmed to the budgets
  `PromptBuilder` enforces, so it can never push the prefix out.
* The default summariser is extractive and deterministic. `summarizer` is the hook for an LLM summary produced when the
  model is idle (ARCHITECTURE section 6); no persistent fact store is touched here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..store import Store
from ..util import clean_user_text, est_tokens, truncate_tokens

Summarizer = Callable[[str, list], str]       # (previous summary, [(role, text)]) -> new summary


def extractive_summary(previous: str, rows: list) -> str:
    add = " ".join(("U: " if role == "user" else "A: ") + clean_user_text(text, 80) for role, text in rows)
    return (previous + " " + add).strip()


@dataclass
class Window:
    history: list            # [(role, text)] oldest first, starts with a user message
    summary: str
    tokens: int
    first_id: int            # id of the oldest message in the window (0 if none)


class MemoryWindow:
    def __init__(self, store: Store, window_tokens: int = 400, summary_tokens: int = 100,
                 per_message_tokens: int = 200, max_messages: int = 4, summarizer: Summarizer | None = None) -> None:
        self.store, self.window_tokens, self.summary_tokens = store, window_tokens, summary_tokens
        self.max_messages = max_messages            # same cap as PromptBuilder (1-2 turns), so what the prompt drops is folded
        self.per_message_tokens = per_message_tokens
        self.summarizer = summarizer or extractive_summary

    def _select(self, rows: list) -> tuple[list, int]:
        picked, used = [], 0
        for rid, role, text in reversed(rows):
            t = truncate_tokens(text, self.per_message_tokens)
            n = est_tokens(t)
            if used + n > self.window_tokens or len(picked) >= self.max_messages:
                break
            picked.append((rid, role, t))
            used += n
        picked.reverse()
        while picked and picked[0][1] != "user":          # chat templates want the window to open with a user turn
            used -= est_tokens(picked[0][2])
            picked.pop(0)
        return picked, used

    def load(self, user_id: str, session_id: str, guest: bool = False) -> Window:
        picked, used = self._select(self.store.recent_rows(user_id, session_id, 16, guest))
        summary = "" if guest else truncate_tokens(self.store.summary(user_id, session_id), self.summary_tokens)
        return Window([(r, t) for _, r, t in picked], summary, used, picked[0][0] if picked else 0)

    def fold(self, user_id: str, session_id: str, guest: bool = False) -> bool:
        """Fold messages that no longer fit the window into the summary. Call after a turn is stored."""
        if guest:
            return False
        rows = self.store.recent_rows(user_id, session_id, 16)
        picked, _ = self._select(rows)
        cutoff = picked[0][0] if picked else (rows[-1][0] + 1 if rows else 0)
        upto = self.store.summary_upto(user_id, session_id)
        old = [(role, text) for rid, role, text in rows if upto < rid < cutoff]
        if not old:
            return False
        text = self.summarizer(self.store.summary(user_id, session_id), old)
        text = text[-(self.summary_tokens * 3):]               # rolling: keep the newest part (est_tokens = bytes / 3)
        self.store.put_summary(user_id, session_id, text, cutoff - 1)
        return True
