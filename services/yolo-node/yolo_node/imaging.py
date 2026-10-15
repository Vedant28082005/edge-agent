"""Image checks and decoding. Everything is in RAM: bytes are never written to disk and never logged."""
from __future__ import annotations

import io
from typing import Tuple

import numpy as np
from PIL import Image

from .errors import NodeError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SIDE = 4096
MAX_PIXELS = 16 * 1024 * 1024

Image.MAX_IMAGE_PIXELS = MAX_PIXELS   # PIL raises DecompressionBombError beyond 2x this; we check explicitly first


def sniff(data: bytes) -> str:
    """Format by magic bytes (the declared Content-Type is ignored). Raises 415 when not jpeg/png/webp."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise NodeError("unsupported_media_type")


def check_header(data: bytes) -> Tuple[int, int]:
    """Cheap pre-queue validation: magic bytes, size, then dimensions from the header only (422 bad_image)."""
    if len(data) > MAX_IMAGE_BYTES:
        raise NodeError("payload_too_large")
    sniff(data)
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001 - any parse failure is a bad image; never echo detail
        raise NodeError("bad_image")
    if w < 1 or h < 1 or w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
        raise NodeError("bad_image")
    return w, h


def decode_rgb(data: bytes) -> np.ndarray:
    """Full decode to an HxWx3 uint8 RGB array. Re-checks the header limits."""
    check_header(data)
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return np.asarray(im.convert("RGB"), dtype=np.uint8)
    except NodeError:
        raise
    except Exception:  # noqa: BLE001
        raise NodeError("bad_image")
