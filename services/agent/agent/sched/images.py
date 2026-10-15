"""Image values handed from the API layer to the perception backends. RAM only: the bytes are never persisted, logged or
repr'd (`data` is excluded from repr), and `ReqCtx.drop_images()` releases them when the turn ends."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_REF_BAD = re.compile(r"[^A-Za-z0-9_-]")


def image_ref(ref: object, index: int) -> str:
    """Map `InlineImage.ref` (any string up to 64 chars) onto the nodes' stricter `image_ref` pattern `^[A-Za-z0-9_-]{1,64}$`.
    Anything outside the pattern becomes `_`; a ref with nothing usable left becomes `img<index>`."""
    s = _REF_BAD.sub("_", ref if isinstance(ref, str) else "")[:64]
    return s if s.strip("_") else f"img{index}"


@dataclass(frozen=True, slots=True)
class NodeImage:
    ref: str                                   # already mapped by `image_ref`
    media_type: str                            # image/jpeg | image/png | image/webp (magic bytes checked at the API layer)
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """Ask yolo-node to grab a frame itself. Only ever built by `YoloNodeClient.snapshot_source`, which has applied the
    role and room checks; no image crosses the wire."""
    source_id: str
