"""Image sources (camera optional). Configured in the node's config file only; the API never receives a URL, path or
device from a caller, only an opaque `source_id` that must match a configured one.

A frame is returned as an RGB array with its capture time, detected on and dropped: never stored, never logged."""
from __future__ import annotations

import os
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .errors import NodeError
from .imaging import MAX_IMAGE_BYTES, decode_rgb

Frame = Tuple[np.ndarray, float]          # (rgb, time.monotonic() at capture)
STALE_S = 5.0                              # a streaming source with no frame for this long is offline
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


class Source:
    source_id = ""
    type = ""

    def online(self) -> bool:
        raise NotImplementedError

    def grab(self) -> Frame:
        """Blocking. Raises NodeError(source_offline)."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class FolderReplay(Source):
    """Cycles through test images in a directory (evaluation and demos)."""
    type = "folder_replay"

    def __init__(self, source_id: str, path: str) -> None:
        self.source_id, self.path, self._i = source_id, path, 0
        self._lock = threading.Lock()

    def _files(self) -> List[str]:
        try:
            return sorted(f for f in os.listdir(self.path) if f.lower().endswith(IMG_EXT))
        except OSError:
            return []

    def online(self) -> bool:
        return bool(self._files())

    def grab(self) -> Frame:
        files = self._files()
        if not files:
            raise NodeError("source_offline")
        with self._lock:
            name = files[self._i % len(files)]
            self._i += 1
        try:
            with open(os.path.join(self.path, name), "rb") as f:
                data = f.read(MAX_IMAGE_BYTES + 1)
            return decode_rgb(data), time.monotonic()
        except (OSError, NodeError):
            raise NodeError("source_offline")


class HttpSnapshot(Source):
    """GET a still image from a URL taken from the config file (e.g. ESP32-CAM). Never from a caller."""
    type = "http_snapshot"

    def __init__(self, source_id: str, url: str, timeout_s: float = 4.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise SystemExit("http_snapshot url must be http(s)")
        self.source_id, self.url, self.timeout_s = source_id, url, timeout_s
        self._ok_at = 0.0

    def online(self) -> bool:
        return time.monotonic() - self._ok_at < 60.0 or self._ok_at == 0.0   # unknown until the first grab

    def grab(self) -> Frame:
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout_s) as r:   # noqa: S310 - config-supplied URL
                data = r.read(MAX_IMAGE_BYTES + 1)
            rgb = decode_rgb(data)
        except Exception:  # noqa: BLE001
            self._ok_at = -1e9
            raise NodeError("source_offline")
        self._ok_at = time.monotonic()
        return rgb, self._ok_at


class OpenCVStream(Source):
    """usb / csi / rtsp via OpenCV (optional dependency `opencv-python-headless`; on the Jetson use the JetPack build).
    A thread keeps only the latest frame, so `frame_age_ms` is real and no backlog builds up."""

    def __init__(self, source_id: str, type_: str, device: Any, cv2_module: Any = None) -> None:
        self.source_id, self.type, self.device = source_id, type_, device
        self._cv2 = cv2_module
        self._latest: Optional[Frame] = None
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._cv2 is None:
            try:
                import cv2
            except ImportError:
                raise SystemExit("source %s needs opencv (pip install opencv-python-headless)" % self.source_id)
            self._cv2 = cv2
        self._t = threading.Thread(target=self._run, name="cam-" + self.source_id, daemon=True)
        self._t.start()

    def _run(self) -> None:
        cv2 = self._cv2
        cap = None
        while not self._stop.is_set():
            if cap is None:
                api = getattr(cv2, "CAP_GSTREAMER", 0) if self.type == "csi" else 0
                cap = cv2.VideoCapture(self.device, api) if api else cv2.VideoCapture(self.device)
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                cap = None
                self._stop.wait(2.0)
                continue
            self._latest = (np.ascontiguousarray(frame[:, :, ::-1]), time.monotonic())   # BGR -> RGB
        if cap is not None:
            cap.release()

    def online(self) -> bool:
        f = self._latest
        return f is not None and time.monotonic() - f[1] < STALE_S

    def grab(self) -> Frame:
        f = self._latest
        if f is None or time.monotonic() - f[1] >= STALE_S:
            raise NodeError("source_offline")
        return f

    def close(self) -> None:
        self._stop.set()


class MockCamera(Source):
    """Synthetic frames for --mock: a flat grey image, always online unless `up` is set False."""
    type = "usb"

    def __init__(self, source_id: str, size: Tuple[int, int] = (640, 480), age_s: float = 0.04) -> None:
        self.source_id, self.size, self.age_s, self.up = source_id, size, age_s, True

    def online(self) -> bool:
        return self.up

    def grab(self) -> Frame:
        if not self.up:
            raise NodeError("source_offline")
        w, h = self.size
        return np.full((h, w, 3), 127, dtype=np.uint8), time.monotonic() - self.age_s


def build_sources(cfg_sources: List[Dict[str, Any]], mock: bool) -> Dict[str, Source]:
    out: Dict[str, Source] = {}
    for s in cfg_sources:
        sid, typ = s["source_id"], s["type"]
        if mock and not s.get("path") and not s.get("url"):
            out[sid] = MockCamera(sid)
        elif typ == "folder_replay":
            out[sid] = FolderReplay(sid, s["path"])
        elif typ == "http_snapshot":
            out[sid] = HttpSnapshot(sid, s["url"])
        else:
            src = OpenCVStream(sid, typ, s.get("device", 0))
            src.start()
            out[sid] = src
    return out
