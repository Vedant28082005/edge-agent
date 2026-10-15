import asyncio

from conftest import H, conforms
from yolo_node.backends.mock import MockBackend


async def test_queue_full_429(make_harness):
    h = make_harness(backend=MockBackend(delay_s=0.6))
    rs = await asyncio.gather(*[h.detect({}) for _ in range(11)])     # 1 running + 8 waiting + 2 rejected
    codes = sorted(r.status_code for r in rs)
    assert codes == [200] * 9 + [429] * 2, codes
    for r in rs:
        conforms("/v1/detect", "post", r)
        if r.status_code == 429:
            assert r.json() == {"error": "queue_full", "message": "queue full"}
            assert int(r.headers["Retry-After"]) >= 1
    assert h.backend.calls == 9
    assert h.node.sched.depth == 0 and not h.node.sched.busy


async def test_timeout_while_running_504(make_harness):
    h = make_harness(backend=MockBackend(delay_s=1.6))
    r = await h.detect({"timeout_ms": 1000})
    assert r.status_code == 504 and r.json() == {"error": "timeout", "message": "job exceeded its deadline"}
    conforms("/v1/detect", "post", r)
    assert h.node.sched.busy                        # the slot stays held until the worker thread really finished
    await asyncio.sleep(1.2)
    assert not h.node.sched.busy


async def test_queued_job_past_deadline_dropped_without_running(make_harness):
    h = make_harness(backend=MockBackend(delay_s=1.3))
    first = asyncio.create_task(h.detect({}))
    await asyncio.sleep(0.2)
    second = await h.detect({"timeout_ms": 1000})   # still queued when its deadline hits
    assert second.status_code == 504
    assert (await first).status_code == 200
    assert h.backend.calls == 1                     # the dropped job never ran
    assert h.node.sched.depth == 0


async def test_one_inference_at_a_time(make_harness):
    active, peak = 0, 0
    b = MockBackend(delay_s=0.15)
    real = b.infer

    def spy(*a, **k):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            return real(*a, **k)
        finally:
            active -= 1
    b.infer = spy
    h = make_harness(backend=b)
    rs = await asyncio.gather(*[h.detect({}) for _ in range(4)])
    assert all(r.status_code == 200 for r in rs) and peak == 1
    assert sorted(round(r.json()["queue_ms"] / 100) for r in rs)[-1] >= 3        # later jobs report real queue wait


async def test_health_shows_queue_and_busy(make_harness):
    h = make_harness(backend=MockBackend(delay_s=0.5))
    t = [asyncio.create_task(h.detect({})) for _ in range(3)]
    await asyncio.sleep(0.2)
    j = (await h.client.get("/health")).json()
    conforms("/health", "get", await h.client.get("/health"))
    assert j["busy"] is True and j["queue_depth"] == 2
    await asyncio.gather(*t)


async def test_client_disconnect_does_not_wedge_queue(make_harness):
    h = make_harness(backend=MockBackend(delay_s=0.3))
    t = [asyncio.create_task(h.detect({})) for _ in range(3)]
    await asyncio.sleep(0.1)
    t[1].cancel()
    await asyncio.gather(*t, return_exceptions=True)
    await asyncio.sleep(0.5)
    r = await h.detect({})
    assert r.status_code == 200 and h.node.sched.depth == 0
