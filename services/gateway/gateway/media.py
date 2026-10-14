"""In-memory media refs (images, voice transcripts). Never written to disk; short TTL; deleted after use."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .security import new_id

IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def sniff_image(data: bytes) -> str | None:
    for magic, ct in IMAGE_MAGIC:
        if data.startswith(magic):
            return ct
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


AUDIO_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/webm", "video/webm", "audio/mp4", "audio/m4a",
               "audio/x-m4a", "audio/aac", "audio/x-aac"}


def sniff_audio(data: bytes) -> bool:
    return (data[:4] == b"RIFF" and data[8:12] == b"WAVE") or data[:4] == b"\x1a\x45\xdf\xa3" or data[4:8] == b"ftyp" \
        or data[:2] in (b"\xff\xf1", b"\xff\xf9")


@dataclass
class Item:
    user_id: str
    kind: str                 # image | voice
    data: bytes | None
    content_type: str
    text: str | None
    expires: float


class MediaStore:
    def __init__(self, ttl_s: int, max_total: int, max_per_user: int, clock=time.time) -> None:
        self.ttl, self.max_total, self.max_user, self.clock = ttl_s, max_total, max_per_user, clock
        self._items: dict[str, Item] = {}

    def _sweep(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._items.items() if v.expires <= now]:
            del self._items[k]

    def _size(self) -> int:
        return sum(len(i.data or b"") for i in self._items.values())

    def put(self, user_id: str, kind: str, data: bytes | None, content_type: str, text: str | None = None) -> str | None:
        """None = quota exceeded (per-user count or total bytes)."""
        self._sweep()
        if sum(1 for i in self._items.values() if i.user_id == user_id) >= self.max_user:
            return None
        if self._size() + len(data or b"") > self.max_total:
            return None
        ref = new_id(("img_" if kind == "image" else "aud_"))
        self._items[ref] = Item(user_id, kind, data, content_type, text, self.clock() + self.ttl)
        return ref

    def take(self, ref: str, user_id: str, kind: str) -> Item | None:
        """Return and delete (single use). A ref of another user is indistinguishable from an unknown one."""
        self._sweep()
        it = self._items.get(ref)
        if it is None or it.user_id != user_id or it.kind != kind:
            return None
        del self._items[ref]
        return it

    def discard(self, ref: str) -> None:
        self._items.pop(ref, None)

    def __len__(self) -> int:
        self._sweep()
        return len(self._items)
