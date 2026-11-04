"""Runs ON the Pi (python3, stdlib only). Reads a COPY of the agent's sqlite file and writes the last N turn traces as JSON.

    python3 dump_traces.py DB_COPY OUT.json [N]

The trace table holds no user text by design, so each trace is joined with the chat history (`messages`): the newest user
message of the same user at or before the trace's timestamp (within 180 s), and the assistant message that followed it.
Guest turns are never persisted, so those show "(not stored)". Nothing here prints a secret: traces carry route, ids,
counts and timings, and the model's raw reply truncated to 300 characters."""
from __future__ import annotations

import json
import sqlite3
import sys
import time

KEEP = ("route", "decision", "source", "fast_reason", "confidence", "outcome", "llm_raw", "steps", "text_source", "say_verdict",
        "roster_hash", "prompt_eval_tokens", "cache_hit_tokens", "prompt_tokens", "cached_tokens", "generated_tokens", "llm_calls",
        "latency_ms", "stage_ms", "planned_calls", "executed", "stripped_writes", "fallbacks", "candidates", "flags", "route_mode",
        "constrained", "cache_hit", "prefix_hash")


def main() -> None:
    db, out = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM turn_traces ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    result = []
    for r in rows:
        d = json.loads(r["detail"])
        user_text, reply = "(not stored)", ""
        if r["user_id"]:
            m = con.execute("SELECT id, text FROM messages WHERE user_id=? AND role='user' AND ts<=? AND ts>=? ORDER BY id DESC LIMIT 1",
                            (r["user_id"], r["ts"] + 1, r["ts"] - 180)).fetchone()
            if m:
                user_text = m["text"]
                a = con.execute("SELECT text FROM messages WHERE user_id=? AND role='assistant' AND id>? ORDER BY id LIMIT 1",
                                (r["user_id"], m["id"])).fetchone()
                reply = a["text"] if a else ""
        row = {"id": r["id"], "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), "user_id": r["user_id"],
               "user_text": user_text, "reply": reply}
        row.update({k: d.get(k) for k in KEEP if k in d})
        result.append(row)
    result.reverse()                                   # oldest first: reads like the conversation
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    print(f"{len(result)} traces -> {out}")


if __name__ == "__main__":
    main()
