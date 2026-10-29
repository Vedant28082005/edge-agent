"""Pluggable frame sources. Anything that can produce encoded image bytes on demand is a FrameSource; the service only
ever calls `grab()`. A USB (OpenCV) or Pi camera source is a new class registered in `make_source`, nothing else changes.
Frames live in RAM only and are never written anywhere."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Protocol

_EXT = {".jpg", ".jpeg", ".png", ".webp"}


class FrameError(Exception):
    """No frame available; the message is fixed text."""


class FrameSource(Protocol):
    name: str

    async def grab(self) -> bytes: ...

    def describe(self) -> dict: ...


def resolve_allowed(root: Path, rel: str) -> Path:
    """`rel` must name an image file inside `root` (symlinks resolved, no escape)."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or len(rel) > 255:
        raise FrameError("bad_path")
    base = root.resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base) or p.suffix.lower() not in _EXT or not p.is_file():
        raise FrameError("bad_path")
    return p


def _read(p: Path) -> bytes:
    try:
        return p.read_bytes()
    except OSError as exc:
        raise FrameError("unreadable") from exc


class FolderSource:
    """Cycles through the images of a directory (sorted by name), one per grab(). Also serves a single file."""
    name = "folder"

    def __init__(self, root: Path):
        self.root = Path(root)
        self._i = 0

    def _files(self) -> list[Path]:
        base = self.root.resolve()
        if base.is_file():
            return [base]
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return []
        out = []
        for n in names:
            try:
                p = resolve_allowed(base, n)
            except FrameError:
                continue
            out.append(p)
        return out

    async def grab(self) -> bytes:
        files = await asyncio.to_thread(self._files)
        if not files:
            raise FrameError("no_frames")
        p = files[self._i % len(files)]
        self._i += 1
        return await asyncio.to_thread(_read, p)

    def describe(self) -> dict:
        return {"kind": self.name}


def make_source(spec: str) -> FrameSource:
    """`folder:<dir>` or a bare path. Later: `usb:<index>` / `picam` register here."""
    kind, sep, arg = spec.partition(":")
    if sep and kind == "folder":
        return FolderSource(Path(arg))
    if sep and kind in ("usb", "picam"):
        raise FrameError("source_not_implemented")
    return FolderSource(Path(spec))
