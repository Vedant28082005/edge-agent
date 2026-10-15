"""Closed error set of the contract. `message` is fixed text per code and never echoes request content."""
from __future__ import annotations

from typing import Optional

STATUS = {
    "bad_request": 400, "unauthorized": 401, "payload_too_large": 413, "unsupported_media_type": 415,
    "bad_image": 422, "queue_full": 429, "model_error": 500, "vision_unavailable": 503, "timeout": 504,
    "unknown_source": 404, "source_offline": 409,
}
MESSAGES = {
    "bad_request": "malformed request",
    "unauthorized": "missing or wrong X-Service-Secret",
    "payload_too_large": "request too large",
    "unsupported_media_type": "unsupported media type",
    "bad_image": "image could not be decoded or is too large in pixels",
    "queue_full": "queue full",
    "model_error": "inference failed",
    "vision_unavailable": "detector not loaded",
    "timeout": "job exceeded its deadline",
    "unknown_source": "unknown source",
    "source_offline": "source offline",
}


class NodeError(Exception):
    def __init__(self, code: str, retry_after: Optional[int] = None, status: Optional[int] = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status or STATUS[code]
        self.retry_after = retry_after

    def body(self) -> dict:
        return {"error": self.code, "message": MESSAGES[self.code]}


class BackendError(Exception):
    """Raised by a backend when inference fails (mapped to model_error 500; message is never returned)."""
