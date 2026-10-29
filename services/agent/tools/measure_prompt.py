"""Prompt size for N simulated devices: the classic prompt vs the generated one (sched.generated_prompt).

    python tools/measure_prompt.py                      # [est] token counts (agent.util.est_tokens: 1 per 3 UTF-8 bytes)
    python tools/measure_prompt.py --llama http://HOST:8080 [--runs 3]
        # [measured] real token counts via llama-server /tokenize, and cold vs warm prefill time from the server's own
        # `timings` (cold = a fresh prefix, warm = the same prefix again with cache_prompt). Run this on the node that has the model.

Never prints a secret: the simulated registry is made up here."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.devices import DeviceRegistry  # noqa: E402
from agent.prompt import PromptBuilder  # noqa: E402
from agent.util import est_tokens  # noqa: E402

P = {"id": "power", "kind": "actuator", "type": "bool"}
B = {"id": "brightness", "kind": "actuator", "type": "int", "min": 0, "max": 100}
S = {"id": "speed", "kind": "actuator", "type": "int", "min": 1, "max": 5}
POS = {"id": "position", "kind": "actuator", "type": "int", "min": 0, "max": 100}
LOCK = {"id": "lock", "kind": "actuator", "type": "enum", "values": ["locked", "unlocked"]}
TEMP = {"id": "temperature", "kind": "sensor", "type": "float", "unit": "C"}
ROOMS = ["Bedroom", "Living room", "Kitchen", "Hall", "Study", "Bathroom"]
KINDS = [("Ceiling light", "light", [P, B]), ("Fan", "fan", [P, S]), ("Blinds", "cover", [POS]), ("Lamp", "light", [P, B]),
         ("Door lock", "lock", [LOCK]), ("Temp sensor", "sensor", [TEMP]), ("Plug", "outlet", [P])]


def simulated(n: int) -> DeviceRegistry:
    devs = []
    for i in range(n):
        name, typ, caps = KINDS[i % len(KINDS)]
        devs.append({"endpoint_id": f"ep_{i + 1:08x}", "name": name if i < len(KINDS) else f"{name} {i // len(KINDS) + 1}",
                     "room": ROOMS[i % len(ROOMS)], "type": typ, "tags": [], "online": True,
                     "risk": "confirm" if typ == "lock" else "normal", "capabilities": caps})
    reg = DeviceRegistry()
    reg.replace(devs, 1)
    return reg


def prompts(n: int, text: str = "turn off the bedroom lamp") -> dict[str, list[dict]]:
    out = {}
    for label, gen in (("classic", False), ("generated", True)):
        b = PromptBuilder(simulated(n), generated=gen)
        out[label] = b.build(text).messages
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llama", default="")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--gguf", default="", help="a Qwen-family .gguf: real token counts offline (BPE read from its vocab; a proxy "
                                              "for Qwen2.5 because the Qwen2/3 text vocab is shared), no server needed")
    ap.add_argument("--sizes", default="5,10,20")
    a = ap.parse_args()
    rows = []
    for n in (int(x) for x in a.sizes.split(",")):
        for label, msgs in prompts(n).items():
            sysmsg = msgs[0]["content"]
            tail = "\n".join(m["content"] for m in msgs[1:])
            row = {"devices": n, "prompt": label, "bytes": len((sysmsg + tail).encode()), "est_tokens_prefix": est_tokens(sysmsg),
                   "est_tokens_total": est_tokens(sysmsg + tail)}
            if a.gguf:
                from qwen_tok import Tok
                tk = Tok(a.gguf)
                tpl = lambda ms: "".join(f"<|im_start|>{m['role']}" + chr(10) + f"{m['content']}<|im_end|>" + chr(10) for m in ms)   # noqa: E731
                row["tokens_prefix"] = tk.count(tpl(msgs[:1]))
                row["tokens_total"] = tk.count(tpl(msgs) + "<|im_start|>assistant" + chr(10))
                row["cold_prefill_s_at_10tps_est"] = round(row["tokens_total"] / 10, 1)
                row["warm_prefill_s_at_10tps_est"] = round((row["tokens_total"] - row["tokens_prefix"]) / 10, 1)
            if a.llama:
                import httpx
                with httpx.Client(timeout=300) as c:
                    def tok(t: str) -> int:
                        return len(c.post(a.llama + "/tokenize", json={"content": t}).json()["tokens"])
                    row["tokens_prefix"], row["tokens_total"] = tok(sysmsg), tok(sysmsg + "\n" + tail)
                    cold, warm = [], []
                    for r in range(a.runs):
                        # a unique first line defeats the cache: a genuinely cold prefill of the whole prompt
                        body = {"messages": [{"role": "system", "content": f"run {time.time_ns()} {r}\n" + sysmsg}, msgs[-1]],
                                "max_tokens": 1, "cache_prompt": True, "id_slot": 0, "temperature": 0}
                        t = c.post(a.llama + "/v1/chat/completions", json=body).json().get("timings", {})
                        cold.append(t.get("prompt_ms"))
                        t = c.post(a.llama + "/v1/chat/completions", json=body).json().get("timings", {})
                        warm.append(t.get("prompt_ms"))
                    row["cold_prefill_ms"], row["warm_prefill_ms"] = cold, warm
            rows.append(row)
    print(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
