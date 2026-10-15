"""Minimal strict multipart/form-data parser over an in-memory body (RAM only, no temp files).

Accepts exactly the parts the contract defines. Filenames are ignored (never stored or logged)."""
from __future__ import annotations

import re
from typing import Dict, Tuple

from .errors import NodeError

_CT = re.compile(r'^multipart/form-data\s*;\s*boundary=(?:"([^"]{1,70})"|([^";\s]{1,70}))\s*$', re.I)
_NAME = re.compile(rb'(?:^|;)\s*name="([A-Za-z0-9_]{1,32})"', re.I)
_BOUNDARY_OK = re.compile(r"^[A-Za-z0-9'()+_,\-./:=? ]{1,70}$")
MAX_PART_HEADERS = 1024


def parse(body: bytes, content_type: str, allowed: Tuple[str, ...]) -> Dict[str, Tuple[str, bytes]]:
    """Return {name: (part_content_type, data)}. Raises NodeError(415) for a non-multipart request and
    NodeError(400) for anything malformed, unknown or duplicated."""
    m = _CT.match(content_type or "")
    if not m:
        raise NodeError("unsupported_media_type")
    boundary = m.group(1) or m.group(2)
    if not _BOUNDARY_OK.match(boundary):
        raise NodeError("bad_request")
    delim = b"--" + boundary.encode("ascii")
    if not body.startswith(delim):
        raise NodeError("bad_request")
    pos = len(delim)
    parts: Dict[str, Tuple[str, bytes]] = {}
    while True:
        if body[pos:pos + 2] == b"--":
            break
        if body[pos:pos + 2] != b"\r\n":
            raise NodeError("bad_request")
        pos += 2
        hend = body.find(b"\r\n\r\n", pos)
        if hend < 0 or hend - pos > MAX_PART_HEADERS:
            raise NodeError("bad_request")
        headers = body[pos:hend]
        start = hend + 4
        nxt = body.find(b"\r\n" + delim, start)
        if nxt < 0:
            raise NodeError("bad_request")
        name = None
        ctype = ""
        for line in headers.split(b"\r\n"):
            low = line.lower()
            if low.startswith(b"content-disposition:"):
                nm = _NAME.search(line.split(b":", 1)[1])
                if nm:
                    name = nm.group(1).decode("ascii")
            elif low.startswith(b"content-type:"):
                ctype = line.split(b":", 1)[1].decode("latin-1").strip().lower()[:100]
        if name is None or name not in allowed or name in parts:
            raise NodeError("bad_request")
        parts[name] = (ctype, body[start:nxt])
        pos = nxt + 2 + len(delim)
        if len(parts) > len(allowed):
            raise NodeError("bad_request")
    return parts
