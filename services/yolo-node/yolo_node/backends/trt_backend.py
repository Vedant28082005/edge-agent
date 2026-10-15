"""TensorRT backend for the Jetson Nano (JetPack 4.6: TensorRT 8.2, CUDA 10.2, system Python 3.6).

The `tensorrt` and `pycuda` Python bindings of JetPack 4.6 exist only for the system Python 3.6, which cannot run
this service's dependencies (starlette, numpy>=1.20, Pillow>=9). So the engine lives in a separate worker process
(`trt_worker.py`, Python 3.6 syntax, depends only on numpy + tensorrt + pycuda) and this module (Python 3.8+) talks to
it over stdin/stdout pipes with length-prefixed raw float32 tensors. Pre- and post-processing stay here.
Image bytes never reach the worker: it only sees the letterboxed float tensor, never a file or a path.

NOT compiled or run in this repository's CI: it needs the real Jetson (see services/yolo-node/README.md)."""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
from typing import List

import numpy as np

from ..errors import BackendError
from . import Backend, RawDetection
from .yolo_post import letterbox, postprocess

HDR = struct.Struct("<I")
ERR = 0xFFFFFFFF
RESTART_BACKOFF_S = 30.0


class _PipeReader:
    def __init__(self, fd: int) -> None:
        self._fd, self._buf, self._eof = fd, bytearray(), False
        self._cv = threading.Condition()
        threading.Thread(target=self._pump, name="trt-pipe", daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                chunk = os.read(self._fd, 1 << 20)
            except OSError:
                chunk = b""
            with self._cv:
                if not chunk:
                    self._eof = True
                else:
                    self._buf += chunk
                self._cv.notify_all()
            if not chunk:
                return

    def read(self, n: int, timeout_s: float, line: bool = False) -> bytes:
        end = time.monotonic() + timeout_s
        with self._cv:
            while True:
                if line:
                    i = self._buf.find(b"\n")
                    if i >= 0:
                        out = bytes(self._buf[:i + 1])
                        del self._buf[:i + 1]
                        return out
                elif len(self._buf) >= n:
                    out = bytes(self._buf[:n])
                    del self._buf[:n]
                    return out
                if self._eof:
                    raise EOFError("worker exited")
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError("worker timeout")
                self._cv.wait(left)


class TrtBackend(Backend):
    name = "trt"

    def __init__(self, trt_cfg: dict, names: List[str], model_name: str) -> None:
        self.cfg, self.names = trt_cfg, names
        self.model = model_name
        self.size = 640
        self._p = None
        self._reader = None
        self._lock = threading.Lock()
        self._last_fail = 0.0
        self._out_shape = ()

    def _script(self) -> str:
        return self.cfg.get("worker_script") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "trt_worker.py")

    def load(self) -> None:
        self.close()
        try:
            self._p = subprocess.Popen([self.cfg["worker_python"], self._script(), self.cfg["engine_path"]],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
            self._reader = _PipeReader(self._p.stdout.fileno())
            line = self._read(0, self.cfg.get("start_timeout_s", 180), line=True)
            ready = json.loads(line.decode("ascii"))
            if not ready.get("ready"):
                raise RuntimeError("worker not ready")
            self.size = int(ready["input_shape"][2])
            self._out_shape = tuple(ready["output_shape"])
            self.engine_loaded = True
        except Exception as e:  # noqa: BLE001
            self.close()
            raise RuntimeError("TensorRT worker failed to start (%s)" % type(e).__name__)

    def close(self) -> None:
        p, self._p = self._p, None
        self._reader = None
        self.engine_loaded = False
        if p is not None:                    # a child this process started itself
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    # -- pipe helpers (portable: a reader thread fills a buffer, reads wait on it with a timeout) -------------------
    def _read(self, n, timeout_s, line=False) -> bytes:
        assert self._reader is not None
        return self._reader.read(n, timeout_s, line)

    def infer(self, rgb: np.ndarray, min_conf: float, max_det: int) -> List[RawDetection]:
        with self._lock:
            if self._p is None or self._p.poll() is not None:
                self.engine_loaded = False
                if time.monotonic() - self._last_fail < RESTART_BACKOFF_S:
                    raise BackendError("worker down")
                try:
                    self.load()
                except Exception:  # noqa: BLE001
                    self._last_fail = time.monotonic()
                    raise BackendError("worker down")
            h, w = rgb.shape[:2]
            try:
                x, ratio, px, py = letterbox(rgb, self.size)
                data = np.ascontiguousarray(x, dtype="<f4").tobytes()
                self._p.stdin.write(HDR.pack(len(data)) + data)
                to = self.cfg.get("io_timeout_s", 30)
                n = HDR.unpack(self._read(4, to))[0]
                if n == ERR:
                    raise BackendError("engine error")
                raw = self._read(n, to)
                out = np.frombuffer(raw, dtype="<f4").reshape(self._out_shape)
                return postprocess(out, self.names, ratio, px, py, w, h, min_conf)[:max_det]
            except BackendError:
                raise
            except Exception as e:  # noqa: BLE001 - worker hung/died: drop it, restart after the backoff
                self._last_fail = time.monotonic()
                self.close()
                raise BackendError(type(e).__name__)
