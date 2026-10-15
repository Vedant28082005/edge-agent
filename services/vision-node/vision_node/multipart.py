"""Minimal in-RAM multipart/form-data parser. Starlette's form parser spools big parts to temp files; the contract
forbids any temp file, so the (size-capped) body is parsed from bytes here. Filenames are never read or kept."""
from __future__ import annotations

import re

from .errors import ApiError

_BOUNDARY = re.compile(r'boundary=(?:"([^"]{1,70})"|([^;\s"]{1,70}))', re.I)
_NAME = re.compile(r'(?:^|;)\s*name="([A-Za-z0-9_]{1,32})"', re.I)
MAX_PARTS = 4
MAX_HEADER_BYTES = 1024


def boundary_of(content_type: str | None) -> bytes:
    if not content_type or not content_type.lower().lstrip().startswith("multipart/form-data"):
        raise ApiError("unsupported_media_type")
    m = _BOUNDARY.search(content_type)
    if not m:
        raise ApiError("bad_request")
    return (m.group(1) or m.group(2)).encode("ascii", "ignore")


def parse(body: bytes, boundary: bytes, max_part: dict[str, int]) -> dict[str, bytes]:
    """Returns {part name: bytes}. Only names in `max_part` are accepted, each exactly once, each within its size cap."""
    if not boundary:
        raise ApiError("bad_request")
    chunks = (b"\r\n" + body).split(b"\r\n--" + boundary)
    if len(chunks) < 3 or not chunks[-1].startswith(b"--"):
        raise ApiError("bad_request")
    out: dict[str, bytes] = {}
    for chunk in chunks[1:-1]:
        if len(out) >= MAX_PARTS:
            raise ApiError("bad_request")
        if not chunk.startswith(b"\r\n"):
            chunk = chunk.lstrip(b" \t")          # tolerate transport padding before the CRLF
            if not chunk.startswith(b"\r\n"):
                raise ApiError("bad_request")
        chunk = chunk[2:]
        sep = chunk.find(b"\r\n\r\n")
        if sep < 0 or sep > MAX_HEADER_BYTES:
            raise ApiError("bad_request")
        headers = chunk[:sep].decode("latin-1")
        data = chunk[sep + 4:]
        name = None
        for line in headers.split("\r\n"):
            k, _, v = line.partition(":")
            if k.strip().lower() == "content-disposition":
                m = _NAME.search(v)
                name = m.group(1) if m else None
        if name is None or name not in max_part or name in out:
            raise ApiError("bad_request")
        if len(data) > max_part[name]:
            raise ApiError("payload_too_large")
        out[name] = data
    return out
