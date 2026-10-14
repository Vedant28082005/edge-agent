from conftest import names, one


async def test_turn_on_bedroom_fan(env):
    ctx, ev = await env.chat("turn on the bedroom fan")
    n = names(ev)
    assert n[0] == "status" and "tool_call" in n and "tool_result" in n and n[-1] == "final"
    sets = [c for c in env.fake.calls if c["tool"] == "set_state"]
    assert len(sets) == 1
    assert sets[0]["args"] == {"endpoint_id": "ep_a1000002", "capability": "power", "value": True}
    assert one(ev, "final")["route"] == "llm"
    assert "fan" in one(ev, "final")["text"].lower()
    # every event carries seq (monotonic) and req_id
    seqs = [d["seq"] for _, d in ev]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert {d["req_id"] for _, d in ev} == {ctx.req_id}
    # out-of-band fields: user_id and call_id attached by the executor, never the token here
    meta = sets[0]["meta"]
    assert meta["user_id"] == "alice" and meta["call_id"] == one(ev, "tool_call")["call_id"]
    assert "confirmation_token" not in meta
