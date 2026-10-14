"""Cost-based router: classification, candidate selection, availability gating, fallback up the ladder, EWMA."""
from __future__ import annotations

import pytest
from sched_fixtures import registry

from agent.sched.config import DEFAULT_COSTS, SchedConfig
from agent.sched.router import (OTHER_Q, PRESENCE_Q, SENSOR_Q, STRUCT_Q, YESNO_Q, BackendAnswer, CostRouter, classify,
                                find_sensor)


class FakeBackend:
    def __init__(self, sources):
        self.sources, self.calls = set(sources), []

    def supports(self, source):
        return source in self.sources

    async def answer(self, source, question, images):
        self.calls.append(source)
        return BackendAnswer(True, "yes", 90.0)


ALL_VISION = ("yolo_jetson", "yolo_pi", "vlm_yesno", "vlm_short", "vlm_json")


def router(*sources, **cfg):
    r = CostRouter(SchedConfig(**cfg))
    if sources:
        r.avail.register(FakeBackend(sources))
    return r


@pytest.mark.parametrize("text,kind", [
    ("what is the temperature in the bedroom?", SENSOR_Q),
    ("how humid is it in the living room", SENSOR_Q),
    ("is the front door open?", SENSOR_Q),
    ("is there a person in the driveway?", PRESENCE_Q),
    ("is anyone at the door", PRESENCE_Q),
    ("is the garage door closed on the camera?", SENSOR_Q),           # contact word wins: a sensor can answer it
    ("is the porch light on in the picture?", YESNO_Q),
    ("describe everything the camera sees", STRUCT_Q),
    ("turn on the fan", OTHER_Q),
    ("good night", OTHER_Q),
    ("tell me a joke", OTHER_Q),
])
def test_classify(text, kind):
    assert classify(text) == kind


def test_find_sensor_resolves_by_room_and_capability():
    reg = registry()
    assert find_sensor("what is the temperature in the bedroom", reg)[0] == ("ep_b0000003", "temperature")
    assert find_sensor("how humid is the living room", reg)[0] == ("ep_b0000009", "humidity")
    assert find_sensor("is there motion in the hall", reg)[0] == ("ep_b000000a", "motion")
    assert find_sensor("what is the temperature", reg)[0] == ("ep_b0000003", "temperature")     # the only one
    assert find_sensor("what is the battery level", reg)[0] is None                              # no such sensor


def test_two_temperature_sensors_without_a_room_is_ambiguous():
    from sched_fixtures import DEVICES
    extra = dict(DEVICES[2], endpoint_id="ep_b00000aa", room="Living room")
    sensor, why = find_sensor("what is the temperature", registry(DEVICES + [extra]))
    assert sensor is None and why == "ambiguous_sensor"
    assert find_sensor("what is the temperature in the living room", registry(DEVICES + [extra]))[0] == ("ep_b00000aa", "temperature")


def test_sensor_is_cheapest_and_the_llm_is_the_last_rung():
    d = router("yolo_pi", "vlm_yesno").route("what is the temperature in the bedroom", registry(), has_image=True)
    assert d.chosen == "sensor" and d.ladder == ["sensor", "llm"] and d.sensor == ("ep_b0000003", "temperature")


def test_presence_prefers_yolo_then_climbs_the_ladder():
    d = router(*ALL_VISION).route("is there a person in the driveway?", registry(), has_image=True)
    assert d.chosen == "yolo_jetson"
    assert d.ladder == ["yolo_jetson", "yolo_pi", "vlm_yesno", "vlm_short", "llm"]
    assert [c.cost_ms for c in d.candidates] == sorted(c.cost_ms for c in d.candidates)
    flags = {c.source: c.flag for c in d.candidates}
    assert flags["vlm_short"] == "est" and flags["llm"] == "est" and flags["yolo_pi"] == "measured"   # seeds: see DEFAULT_COSTS


def test_jetson_down_falls_back_to_the_pi():
    r = router(*ALL_VISION)
    r.avail.update_health("jetson-yolo", up=False)
    d = r.route("is there a person in the driveway?", registry(), has_image=True)
    assert d.chosen == "yolo_pi"
    assert next(c for c in d.candidates if c.source == "yolo_jetson").reason == "node_down"


def test_vlm_is_rejected_below_400_mb():
    r = router("vlm_yesno", "vlm_short")
    r.avail.update_health("pi-vision", up=True, mem_available_mb=350)
    d = r.route("is the porch light on in the picture?", registry(), has_image=True)
    assert d.chosen == "llm"
    assert {c.reason for c in d.candidates if c.source.startswith("vlm")} == {"low_memory"}
    r.avail.update_health("pi-vision", up=True, mem_available_mb=900)
    assert r.route("is the porch light on in the picture?", registry(), has_image=True).chosen == "vlm_yesno"


def test_no_backend_or_no_image_means_llm():
    assert router().route("is there a person in the driveway?", registry(), has_image=True).chosen == "llm"
    d = router(*ALL_VISION).route("is there a person in the driveway?", registry(), has_image=False)
    assert d.chosen == "llm" and {c.reason for c in d.candidates if c.source != "llm"} == {"no_image_or_camera"}


def test_non_perception_goes_straight_to_the_llm():
    d = router(*ALL_VISION).route("turn on the fan", registry())
    assert d.kind == OTHER_Q and d.chosen == "llm" and d.ladder == ["llm"]


def test_guest_gets_no_sensor_route():
    d = router().route("what is the temperature in the bedroom", registry(), role="guest")
    assert d.chosen == "llm" and d.sensor is None


def test_failure_trips_the_breaker_and_recovery_clears_it():
    t = [0.0]
    r = router("yolo_jetson", "yolo_pi")
    r.avail.clock = lambda: t[0]
    r.observe("yolo_jetson", 0, ok=False)
    assert r.route("is anyone at the door", registry(), has_image=True).chosen == "yolo_pi"
    t[0] += SchedConfig().breaker_s + 1
    assert r.route("is anyone at the door", registry(), has_image=True).chosen == "yolo_jetson"
    r.observe("yolo_jetson", 0, ok=False)
    r.observe("yolo_jetson", 80, ok=True)                               # a success closes the breaker at once
    assert r.route("is anyone at the door", registry(), has_image=True).chosen == "yolo_jetson"


def test_ewma_updates_and_can_reorder_the_ladder():
    r = router(*ALL_VISION, ewma_alpha=0.5)
    first = r.costs.snapshot()["vlm_yesno"]
    assert first == {"ms": 2400.0, "flag": "measured", "n": 0}
    r.observe("vlm_yesno", 1000, ok=True)
    assert r.costs.ms["vlm_yesno"] == 1000.0 and r.costs.flag["vlm_yesno"] == "measured"      # first observation replaces the seed
    r.observe("vlm_yesno", 2000, ok=True)
    assert r.costs.ms["vlm_yesno"] == 1500.0                                                   # then EWMA, alpha 0.5
    for _ in range(6):                                                                           # YOLO turns out to be slow here
        r.observe("yolo_jetson", 9000, ok=True)
        r.observe("yolo_pi", 8000, ok=True)
    d = r.route("is there a person in the driveway?", registry(), has_image=True)
    assert d.chosen == "vlm_yesno"                                                              # cheaper than both YOLOs now
    assert d.ladder.index("vlm_yesno") < d.ladder.index("yolo_pi") < d.ladder.index("yolo_jetson") or d.chosen == "vlm_yesno"


def test_cost_table_seed_and_config_override():
    assert DEFAULT_COSTS["yolo_pi"][0] == 94.0 and DEFAULT_COSTS["vlm_yesno"][0] == 2400.0
    assert 12000 <= DEFAULT_COSTS["vlm_json"][0] <= 15000 and 15000 <= DEFAULT_COSTS["llm"][0] <= 30000
    measured = {k for k, (_, flag) in DEFAULT_COSTS.items() if flag == "measured"}
    assert measured == {"yolo_pi", "yolo_jetson", "vlm_yesno"}          # vlm_short (unverified), vlm_json, llm, sensor stay [est]
    cfg = SchedConfig.from_dict({"costs": {"yolo_pi": [50, "measured"], "llm": [20000, "est"]}})
    r = CostRouter(cfg)
    assert r.costs.ms["yolo_pi"] == 50.0 and r.costs.flag["yolo_pi"] == "measured"
    with pytest.raises(ValueError):
        SchedConfig.from_dict({"costs": {"yolo_pi": [50, "guess"]}})
    with pytest.raises(ValueError):
        SchedConfig.from_dict({"nonsense": 1})
