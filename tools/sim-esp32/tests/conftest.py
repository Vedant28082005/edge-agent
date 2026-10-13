import json
import sys
import uuid
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simesp32.clock import ManualClock  # noqa: E402
from simesp32.config import _node  # noqa: E402
from simesp32.node import Node  # noqa: E402
from simesp32.schemas import Validator  # noqa: E402
from simesp32.transport import MemoryBus, MemoryTransport  # noqa: E402

NID = "esp32-a1b2c3d4e501"

HOME = """
node_id: esp32-a1b2c3d4e501
failsafe_after_s: 60
heartbeat_s: 60
endpoints:
  - local_id: ch1
    type: fan
    capabilities:
      - {id: power, boot_value: false, failsafe_value: false, max_on_s: 100}
      - {id: speed, min: 1, max: 5, boot_value: 2, failsafe_value: 1}
  - local_id: l1
    type: light
    capabilities:
      - {id: power, boot_value: true}
      - {id: brightness, boot_value: 30}
      - {id: mode, values: [eco, boost], boot_value: eco}
  - local_id: c1
    type: cover
    sim: {travel_time_s: 10, interlock_s: 0.5, progress_s: 1}
    capabilities:
      - {id: position, boot_value: 0, failsafe_value: 0}
  - local_id: t1
    type: sensor
    capabilities:
      - {id: temperature, interval_s: 5, generator: {kind: sequence, values: [20.0, 21.5, 23.0]}}
      - {id: motion, interval_s: 5, generator: {kind: constant, value: true}}
"""


@pytest.fixture(scope="session")
def validator():
    return Validator()


class Rig:
    def __init__(self, validator, text=HOME, start=True, **overrides):
        raw = yaml.safe_load(text)
        for k, v in overrides.items():
            raw[k] = v
        self.cfg = _node(raw, 0)
        self.clock = ManualClock()
        self.bus = MemoryBus()
        self.tr = MemoryTransport(self.cfg.node_id, self.bus, self.clock, f"home/v1/nodes/{self.cfg.node_id}/status")
        self.node = Node(self.cfg, self.tr, validator, self.clock)
        self.node.boot()
        if start:
            self.tr.start()

    def cmd(self, endpoint=None, set=None, **extra):
        body = {"cmd_id": str(uuid.uuid4()), "endpoint": endpoint, "set": set, "by": "user:test"}
        body = {k: v for k, v in body.items() if v is not None}
        body.update(extra)
        self.bus.inject(self.cfg.node_id, body)
        return body["cmd_id"]

    def raw_cmd(self, payload):
        self.bus.inject(self.cfg.node_id, payload)

    def run(self, seconds, step=0.1):
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end - 1e-9:
            self.clock.advance(step)
            self.tr.poll()
            self.node.tick()

    def acks(self):
        return self.bus.on_topic("ack")

    def last_state(self):
        return self.bus.on_topic("state")[-1]


@pytest.fixture
def rig(validator):
    return Rig(validator)


@pytest.fixture
def make_rig(validator):
    def _make(text=HOME, **kw):
        return Rig(validator, text, **kw)
    return _make
