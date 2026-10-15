"""Bounded queue (429), STT priority, deadlines (504), one heavy job at a time."""
import asyncio
import time

import pytest

from conftest import SECRET, png, wav
from vision_node.errors import ApiError
from vision_node.scheduler import PRIO_OTHER, PRIO_STT, Scheduler

Q = {"question": "Is the light on?"}


async def until(pred, timeout=3.0):
    t = time.monotonic()
    while not pred():
        assert time.monotonic() - t < timeout, "condition not reached"
        await asyncio.sleep(0.005)


def spawn(coro):
    return asyncio.ensure_future(coro)


@pytest.mark.anyio
async def test_queue_full_returns_429_with_retry_after(env, contract):
    gate = env.backends.vlm.gate = asyncio.Event()
    running = spawn(env.post("/v1/vlm/short", Q, png()))
    await until(lambda: env.vision.sched.busy)
    waiting = []
    for i in range(4):
        waiting.append(spawn(env.post("/v1/vlm/short", {"question": f"q{i}"}, png())))
    await until(lambda: env.vision.sched.depth == 4)
    h = (await env.client.get("/health")).json()
    assert h["busy"] is True and h["queue_depth"] == 4

    r = await env.post("/v1/vlm/short", Q, png())
    assert r.status_code == 429 and r.json()["error"] == "queue_full"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert not list(contract.validator("Error").iter_errors(r.json()))
    # STT is rejected too once the queue is full (priority does not bypass the bound)
    assert (await env.post("/v1/stt", {}, wav(), "audio")).status_code == 429

    gate.set()
    done = await asyncio.gather(running, *waiting)
    assert [d.status_code for d in done] == [200] * 5
    assert env.vision.sched.depth == 0 and not env.vision.sched.busy


@pytest.mark.anyio
async def test_one_heavy_job_at_a_time(env):
    active = peak = 0
    orig = env.backends.vlm.short

    async def counted(*a, **k):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return await orig(*a, **k)
        finally:
            active -= 1
    env.backends.vlm.short = counted
    env.backends.stt.delay = 0.03
    rs = await asyncio.gather(*[env.post("/v1/vlm/short", {"question": f"q{i}"}, png()) for i in range(3)],
                              env.post("/v1/stt", {}, wav(), "audio"))
    assert all(r.status_code == 200 for r in rs) and peak == 1
    assert max(r.json()["queue_ms"] for r in rs) > 0


@pytest.mark.anyio
async def test_stt_has_priority_over_waiting_vlm_and_detect(env):
    order = []
    gate = env.backends.vlm.gate = asyncio.Event()
    orig_stt, orig_det = env.backends.stt.transcribe, env.backends.detect.detect

    async def stt(w):
        order.append("stt")
        return await orig_stt(w)

    async def det(*a):
        order.append("detect")
        return await orig_det(*a)
    env.backends.stt.transcribe, env.backends.detect.detect = stt, det
    first = spawn(env.post("/v1/vlm/short", Q, png()))
    await until(lambda: env.vision.sched.busy)
    d = spawn(env.post("/v1/detect", {}, png()))
    await until(lambda: env.vision.sched.depth == 1)
    s = spawn(env.post("/v1/stt", {}, wav(), "audio"))
    await until(lambda: env.vision.sched.depth == 2)
    gate.set()
    assert all(r.status_code == 200 for r in await asyncio.gather(first, d, s))
    assert order == ["stt", "detect"]


@pytest.mark.anyio
async def test_job_expiring_in_queue_is_dropped_without_running(env):
    gate = env.backends.vlm.gate = asyncio.Event()
    first = spawn(env.post("/v1/vlm/short", Q, png()))
    await until(lambda: env.vision.sched.busy)
    t0 = time.monotonic()
    r = await env.post("/v1/vlm/yesno", {**Q, "timeout_ms": 1000}, png())
    assert r.status_code == 504 and r.json()["error"] == "timeout" and 0.9 < time.monotonic() - t0 < 2.5
    assert env.vision.sched.depth == 0                     # the expired waiter left the queue
    assert env.backends.vlm.calls == 1                     # only the first job ever reached the model
    gate.set()
    assert (await first).status_code == 200
    assert not env.vision.sched.busy


@pytest.mark.anyio
async def test_running_job_exceeding_timeout_is_504_and_slot_is_freed(env):
    env.backends.vlm.delay = 5
    t0 = time.monotonic()
    r = await env.post("/v1/vlm/short", {**Q, "timeout_ms": 1000}, png())
    assert r.status_code == 504 and time.monotonic() - t0 < 2.5
    assert not env.vision.sched.busy and env.vision.sched.depth == 0
    env.backends.vlm.delay = 0
    assert (await env.post("/v1/vlm/short", Q, png())).status_code == 200


@pytest.mark.anyio
async def test_node_cap_applies_when_timeout_is_larger_or_absent(make_env):
    env = make_env(node_cap_ms=1000)
    env.backends.stt.delay = 5
    t0 = time.monotonic()
    assert (await env.post("/v1/stt", {}, wav(), "audio")).status_code == 504
    assert time.monotonic() - t0 < 2.5


@pytest.mark.anyio
async def test_uploads_beyond_admission_are_refused_before_the_body_is_read(env):
    sched = env.vision.sched
    sched._inflight = sched.max_inflight
    r = await env.client.post("/v1/vlm/short", content=b"x", headers={
        "X-Service-Secret": SECRET, "Content-Type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 429
    sched._inflight = 0
    assert (await env.post("/v1/vlm/short", Q, png())).status_code == 200
    assert sched._inflight == 0


@pytest.mark.anyio
async def test_slow_upload_is_cut_off(env, monkeypatch):
    import vision_node.app as app_mod
    monkeypatch.setattr(app_mod, "BODY_READ_S", 0.2)

    async def slow():
        yield b"--zz"
        await asyncio.sleep(1)
        yield b"x"
    r = await env.client.post("/v1/vlm/short", content=slow(), headers={
        "X-Service-Secret": SECRET, "Content-Type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 504 and env.vision.sched._inflight == 0


# ---------------------------------------------------------------------------------------------- scheduler unit
@pytest.mark.anyio
async def test_scheduler_cancelled_waiter_does_not_leak_the_slot():
    s = Scheduler(max_waiting=4)
    assert await s.acquire(PRIO_OTHER, time.monotonic() + 5) == 0
    t = spawn(s.acquire(PRIO_OTHER, time.monotonic() + 5))
    await until(lambda: s.depth == 1)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert s.depth == 0
    s.release()
    assert not s.busy
    assert await s.acquire(PRIO_OTHER, time.monotonic() + 1) == 0


@pytest.mark.anyio
async def test_scheduler_priority_then_fifo():
    s = Scheduler(max_waiting=4)
    await s.acquire(PRIO_OTHER, time.monotonic() + 5)
    got = []

    async def w(name, prio):
        await s.acquire(prio, time.monotonic() + 5)
        got.append(name)
        s.release()
    tasks = [spawn(w("a", PRIO_OTHER)), spawn(w("b", PRIO_OTHER))]
    await until(lambda: s.depth == 2)
    tasks.append(spawn(w("stt", PRIO_STT)))
    await until(lambda: s.depth == 3)
    s.release()
    await asyncio.gather(*tasks)
    assert got == ["stt", "a", "b"]


@pytest.mark.anyio
async def test_scheduler_rejects_fifth_waiter():
    s = Scheduler(max_waiting=4)
    await s.acquire(PRIO_OTHER, time.monotonic() + 5)
    ts = [spawn(s.acquire(PRIO_OTHER, time.monotonic() + 5)) for _ in range(4)]
    await until(lambda: s.depth == 4)
    with pytest.raises(ApiError) as e:
        await s.acquire(PRIO_STT, time.monotonic() + 5)
    assert e.value.code == "queue_full"
    for t in ts:
        t.cancel()
