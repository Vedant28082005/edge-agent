"""Image intake: magic-byte check, header-only size/pixel limits, resize to 224 px on the long side, all in RAM."""
from __future__ import annotations

import io

from PIL import Image

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 4096
MAX_PIXELS = 16_000_000
TARGET_SIDE = 224
_FORMATS = {"JPEG", "PNG", "WEBP"}

Image.MAX_IMAGE_PIXELS = MAX_PIXELS     # Pillow raises DecompressionBombError beyond 2x this


class BadImage(Exception):
    """Not a usable image; the message is fixed text, never request content."""


def prepare(data: bytes, target: int = TARGET_SIDE) -> bytes:
    """Validate and return a JPEG whose long side is `target` px (never upscaled beyond it). EXIF is dropped."""
    if len(data) > MAX_BYTES:
        raise BadImage("too_large")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in _FORMATS:
                raise BadImage("unsupported_format")
            w, h = im.size
            if w < 1 or h < 1 or w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
                raise BadImage("bad_dimensions")
            im.draft("RGB", (target * 2, target * 2))      # JPEG: decode at reduced size
            im = im.convert("RGB")
            im.thumbnail((target, target), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, "JPEG", quality=90)
            return out.getvalue()
    except BadImage:
        raise
    except Exception as exc:                                  # PIL errors, bombs, truncated data
        raise BadImage("undecodable") from exc
