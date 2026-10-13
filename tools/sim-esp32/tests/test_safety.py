"""Firmware safety behaviour: boot_value, failsafe_value, max_on_s, curtain travel time and interlock."""
import pytest

from simesp32.clock import ManualClock
from simesp32.cover import Cover


# ---------------------------------------------------------------- boot_value
def test_boot_value_applied_before_any_network(make_rig):
    r = make_rig(start=False)
    assert not r.tr.connected and r.bus.published == []
    assert r.node.values["l1"] == {"power": True, "brightness": 30, "mode": "eco"}
    assert r.node.values["ch1"] == {"power": False, "speed": 2}
    assert r.node.covers["c1"].position == 0


# ---------------------------------------------------------------- failsafe
def test_failsafe_applies_after_hub_lost_for_failsafe_after_s(rig):
    rig.cmd("ch1", {"power": True, "speed": 5})
    rig.tr.drop(10_000)                                    # link down, no reconnect in this test
    rig.run(59)
    assert rig.node.values["ch1"] == {"power": True, "speed": 5}
    rig.run(2)
    assert rig.node.values["ch1"] == {"power": False, "speed": 1}      # failsafe_value of both caps
    assert rig.node.stats["failsafes"] == 1
    rig.run(120)
    assert rig.node.stats["failsafes"] == 1                # applied once per outage


def test_failsafe_only_touches_caps_that_declare_it(rig):
    rig.cmd("l1", {"brightness": 77, "power": False})      # l1 declares no failsafe_value at all
    rig.tr.drop(10_000)
    rig.run(61)
    assert rig.node.values["l1"]["brightness"] == 77 and rig.node.values["l1"]["power"] is False


def test_failsafe_cancelled_by_reconnect_before_deadline(rig):
    rig.cmd("ch1", {"speed": 4})
    rig.tr.drop(30)
    rig.run(45)                                            # back after 30 s < 60 s
    rig.run(120)
    assert rig.node.values["ch1"]["speed"] == 4 and rig.node.stats["failsafes"] == 0


def test_failsafe_not_reapplied_after_reconnect_and_new_cmd(rig):
    rig.tr.drop(100)
    rig.run(70)
    assert rig.node.stats["failsafes"] == 1
    rig.run(40)                                            # reconnected
    rig.cmd("ch1", {"power": True})
    rig.run(10)
    assert rig.node.values["ch1"]["power"] is True        # the new user command is respected
    rig.tr.drop(100)                                       # a second outage arms it again
    rig.run(61)
    assert rig.node.values["ch1"]["power"] is False and rig.node.stats["failsafes"] == 2


def test_failsafe_after_zero_keeps_last_values(make_rig):
    r = make_rig(failsafe_after_s=0)
    r.cmd("ch1", {"speed": 4})                             # (power would trip max_on_s=100 after 100 s)
    r.tr.drop(10_000)
    r.run(3600)
    assert r.node.values["ch1"]["speed"] == 4 and r.node.stats["failsafes"] == 0


def test_failsafe_counts_from_boot_if_broker_never_reachable(make_rig):
    r = make_rig(start=False)
    r.node.values["ch1"]["power"] = True                   # (a local button press while offline)
    r.run(61)
    assert r.node.values["ch1"]["power"] is False


def test_failsafe_publishes_state_after_reconnect(rig):
    rig.cmd("ch1", {"power": True})
    rig.tr.drop(100)
    rig.run(65)
    rig.run(40)
    assert rig.last_state()["endpoints"]["ch1"]["power"] is False


# ---------------------------------------------------------------- max_on_s
def test_max_on_forces_off_after_continuous_on_time(rig):          # ch1.power max_on_s = 100
    rig.cmd("ch1", {"power": True})
    rig.run(99)
    assert rig.node.values["ch1"]["power"] is True
    rig.run(2)
    assert rig.node.values["ch1"]["power"] is False
    assert rig.last_state()["endpoints"]["ch1"]["power"] is False   # and the hub is told
    assert rig.node.stats["max_on_trips"] == 1


def test_max_on_is_continuous_not_restarted_by_repeat_on(rig):
    rig.cmd("ch1", {"power": True})
    rig.run(60)
    rig.cmd("ch1", {"power": True})
    rig.run(41)
    assert rig.node.values["ch1"]["power"] is False


def test_max_on_resets_when_turned_off_and_on(rig):
    rig.cmd("ch1", {"power": True})
    rig.run(90)
    rig.cmd("ch1", {"power": False})
    rig.cmd("ch1", {"power": True})
    rig.run(90)
    assert rig.node.values["ch1"]["power"] is True


def test_max_on_works_while_hub_is_down(rig):
    rig.cmd("ch1", {"power": True})
    rig.tr.drop(10_000)
    rig.run(101)
    assert rig.node.values["ch1"]["power"] is False


def test_max_on_zero_means_unlimited(make_rig):
    r = make_rig(failsafe_after_s=0)
    r.node.caps["ch1"]["power"].max_on_s = 0
    r.node.on_since.clear()
    r.cmd("ch1", {"power": True})
    r.run(100_000, step=100)
    assert r.node.values["ch1"]["power"] is True


def test_max_on_counts_from_boot_if_boot_value_is_on(make_rig):
    r = make_rig("""
node_id: esp32-a1b2c3d4e598
endpoints:
  - {local_id: o1, type: outlet, capabilities: [{id: power, boot_value: true, max_on_s: 10}]}
""")
    r.run(11)
    assert r.node.values["o1"]["power"] is False


# ---------------------------------------------------------------- curtain
def run_cover(cv: Cover, clock, seconds, step=0.05, check=True):
    end = clock.monotonic() + seconds
    while clock.monotonic() < end - 1e-9:
        clock.advance(step)
        cv.update(clock.monotonic())
        if check:
            assert not (cv.up and cv.down), "interlock violated: both relays energised"


def test_cover_travel_time():
    clock = ManualClock()
    cv = Cover(0, 0, 100, 10, 0.5, clock.monotonic())
    cv.set_target(50, clock.monotonic())
    run_cover(cv, clock, 2.0)
    assert 15 <= cv.pos <= 21 and cv.up and not cv.down
    run_cover(cv, clock, 3.5)
    assert cv.position == 50 and not cv.moving and not cv.busy


def test_cover_full_travel_takes_travel_time_s():
    clock = ManualClock()
    cv = Cover(0, 0, 100, 12, 0.5, 0)
    cv.set_target(100, 0)
    run_cover(cv, clock, 11.5)
    assert cv.position < 100 and cv.moving
    run_cover(cv, clock, 1.0)
    assert cv.position == 100 and not cv.moving


def test_cover_reversal_waits_for_interlock_and_never_energises_both():
    clock = ManualClock()
    cv = Cover(0, 0, 100, 10, 0.5, 0)
    cv.set_target(100, 0)
    run_cover(cv, clock, 3.0)
    assert cv.up
    t_rev = clock.monotonic()
    cv.set_target(0, t_rev)
    assert not cv.up and not cv.down                       # up dropped immediately, down must wait
    stopped_at = cv.pos
    run_cover(cv, clock, 0.4)
    assert not cv.down and cv.pos == stopped_at            # still inside the dead time
    run_cover(cv, clock, 0.3)
    assert cv.down                                         # energised only after >= interlock_s
    run_cover(cv, clock, 20)
    assert cv.position == 0 and not cv.moving


def test_cover_set_to_current_position_does_not_move():
    clock = ManualClock()
    cv = Cover(30, 0, 100, 10, 0.5, 0)
    cv.set_target(30, 0)
    run_cover(cv, clock, 1)
    assert not cv.moving and cv.position == 30


def test_cover_retarget_in_same_direction_keeps_moving_without_dead_time():
    clock = ManualClock()
    cv = Cover(0, 0, 100, 10, 0.5, 0)
    cv.set_target(30, 0)
    run_cover(cv, clock, 1.5)
    cv.set_target(80, clock.monotonic())
    assert cv.up
    run_cover(cv, clock, 20)
    assert cv.position == 80


def test_cover_zero_travel_time_is_instant():
    cv = Cover(0, 0, 100, 0, 0.5, 0)
    cv.set_target(70, 0)
    assert cv.position == 70 and not cv.moving


def test_cover_through_node_ack_is_immediate_state_follows_travel(make_rig):
    r = make_rig()
    cid = r.cmd("c1", {"position": 50})
    assert r.acks()[-1]["values"] == {"position": 50} and r.acks()[-1]["ok"]       # ack = accepted target
    assert r.last_state()["endpoints"]["c1"]["position"] == 0                    # state = actual position
    r.run(2.6)
    mid = r.last_state()["endpoints"]["c1"]["position"]
    assert 15 <= mid <= 30                                                       # progress published while moving
    r.run(4)
    assert r.last_state()["endpoints"]["c1"]["position"] == 50                   # final state published
    assert r.node.stats["violations"] == 0


def test_cover_reversal_via_commands_interlock(make_rig):
    r = make_rig()
    cov = r.node.covers["c1"]
    r.cmd("c1", {"position": 100})
    for _ in range(40):
        r.run(0.1)
        assert not (cov.up and cov.down)
    r.cmd("c1", {"position": 0})
    seen_idle = False
    for _ in range(200):
        r.run(0.1)
        assert not (cov.up and cov.down)
        seen_idle = seen_idle or (not cov.up and not cov.down and cov.busy)
    assert seen_idle and cov.position == 0


def test_cover_failsafe_moves_to_failsafe_position(make_rig):
    r = make_rig()
    r.cmd("c1", {"position": 80})
    r.run(9)
    r.tr.drop(10_000)
    r.run(61)                                              # failsafe -> travel back to 0 takes 8 s
    r.run(10)
    assert r.node.covers["c1"].position == 0
