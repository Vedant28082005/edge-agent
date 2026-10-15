"""Mock backend: deterministic detections scaled to the image, optional delay/failure for tests. No model."""
from __future__ import annotations

import time
from typing import List, Optional

import numpy as np

from ..errors import BackendError
from . import Backend, RawDetection


class MockBackend(Backend):
    name = "mock"
    mock = True

    def __init__(self, model: str = "yolov8n-mock", delay_s: float = 0.0, fail: bool = False,
                 loaded: bool = True, detections: Optional[List[RawDetection]] = None) -> None:
        self.model = model
        self.delay_s = delay_s
        self.fail = fail
        self.engine_loaded = loaded
        self.calls = 0
        # (label, conf, relative [x, y, w, h]); the 0.10 one proves min_conf filtering
        self._rel = detections if detections is not None else [
            ("person", 0.81, [0.10, 0.10, 0.30, 0.60]),
            ("dog", 0.30, [0.50, 0.50, 0.20, 0.20]),
            ("cat", 0.10, [0.70, 0.10, 0.10, 0.10]),
        ]

    def load(self) -> None:
        self.engine_loaded = True

    def infer(self, rgb: np.ndarray, min_conf: float, max_det: int) -> List[RawDetection]:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise BackendError("mock failure")
        h, w = rgb.shape[:2]
        out = [(lab, c, [rx * w, ry * h, rw * w, rh * h]) for lab, c, (rx, ry, rw, rh) in self._rel if c >= min_conf]
        out.sort(key=lambda d: -d[1])
        return out[:max_det]
