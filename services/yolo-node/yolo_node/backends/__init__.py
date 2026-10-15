"""Detector backends behind one interface.

    mock   deterministic, no model (tests, --mock)
    onnx   onnxruntime CPU on a YOLOv8 .onnx export (Pi 5, PC, Jetson without TensorRT)
    trt    TensorRT FP16 engine in an isolated Python 3.6 worker process (Jetson Nano, JetPack 4.6)
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

# (label, conf 0..1, [x, y, w, h] in pixels of the submitted image), sorted by conf descending
RawDetection = Tuple[str, float, List[float]]


class Backend:
    name = "base"
    model = "yolov8n"
    mock = False
    engine_loaded = False          # health `engine_loaded`; false -> 503 vision_unavailable on detection

    def load(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def infer(self, rgb: np.ndarray, min_conf: float, max_det: int) -> List[RawDetection]:
        """Blocking; called from the single inference thread. Raises BackendError on failure."""
        raise NotImplementedError


def load_names(path: Optional[str]) -> List[str]:
    if not path:
        from .coco import COCO80
        return list(COCO80)
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def make_backend(cfg) -> Backend:
    if cfg.backend == "mock":
        from .mock import MockBackend
        return MockBackend(model=cfg.model_name)
    names = load_names(cfg.names_file)
    if cfg.backend == "onnx":
        from .onnx_backend import OnnxBackend
        return OnnxBackend(cfg.model_path, names, cfg.model_name, cfg.input_size, cfg.threads)
    if cfg.backend == "trt":
        from .trt_backend import TrtBackend
        return TrtBackend(cfg.trt, names, cfg.model_name)
    raise ValueError("backend")
