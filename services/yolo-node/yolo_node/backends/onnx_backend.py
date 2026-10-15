"""onnxruntime CPU backend for a YOLOv8 ONNX export (also the Pi 5 / PC path).

Export once on the PC (needs `pip install ultralytics`, which is NOT a runtime dependency of this service):
    yolo export model=yolov8n.pt format=onnx opset=12 imgsz=640 simplify=True
and place the .onnx at `model_path`."""
from __future__ import annotations

import threading
from typing import List

import numpy as np

from ..errors import BackendError
from . import Backend, RawDetection
from .yolo_post import letterbox, postprocess


class OnnxBackend(Backend):
    name = "onnx"

    def __init__(self, model_path: str, names: List[str], model_name: str, input_size: int = 640,
                 threads: int = 2) -> None:
        self.model_path, self.names, self.model = model_path, names, model_name
        self.size, self.threads = input_size, threads
        self._sess = None
        self._input = ""
        self._lock = threading.Lock()

    def load(self) -> None:
        try:
            import onnxruntime as ort
        except ImportError:
            raise SystemExit("onnxruntime is not installed (pip install onnxruntime)")
        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        try:
            self._sess = ort.InferenceSession(self.model_path, so, providers=["CPUExecutionProvider"])
        except Exception as e:  # noqa: BLE001 - leave engine_loaded False: /health ok=false, detect 503
            self._sess = None
            self.engine_loaded = False
            raise RuntimeError("could not load model (%s)" % type(e).__name__)
        inp = self._sess.get_inputs()[0]
        self._input = inp.name
        shp = inp.shape
        if len(shp) == 4 and isinstance(shp[2], int) and shp[2] == shp[3]:
            self.size = shp[2]
        self.engine_loaded = True

    def close(self) -> None:
        self._sess = None
        self.engine_loaded = False

    def infer(self, rgb: np.ndarray, min_conf: float, max_det: int) -> List[RawDetection]:
        if self._sess is None:
            raise BackendError("not loaded")
        h, w = rgb.shape[:2]
        try:
            x, ratio, px, py = letterbox(rgb, self.size)
            with self._lock:
                out = self._sess.run(None, {self._input: x})[0]
            return postprocess(out, self.names, ratio, px, py, w, h, min_conf)[:max_det]
        except BackendError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BackendError(type(e).__name__)
