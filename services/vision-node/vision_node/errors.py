"""Error codes of the contract. `message` is FIXED per code and never echoes request content (contract: Errors)."""
from __future__ import annotations

STATUS = {
    "bad_request": 400, "unauthorized": 401, "payload_too_large": 413, "unsupported_media_type": 415,
    "bad_image": 422, "bad_audio": 422, "queue_full": 429, "model_error": 500, "schema_violation": 502,
    "vision_unavailable": 503, "timeout": 504,
}
MESSAGE = {
    "bad_request": "Invalid request.",
    "unauthorized": "Authentication required.",
    "payload_too_large": "Payload too large.",
    "unsupported_media_type": "Unsupported media type.",
    "bad_image": "The image could not be used.",
    "bad_audio": "The audio could not be used.",
    "queue_full": "Node busy, retry later.",
    "model_error": "Model backend error.",
    "schema_violation": "Model output did not satisfy the schema.",
    "vision_unavailable": "Model not available.",
    "timeout": "Deadline exceeded.",
}


class ApiError(Exception):
    """Raised anywhere; rendered as `{"error": code, "message": <fixed>}`. Carries no free text on purpose."""

    def __init__(self, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def body(self) -> dict:
        return {"error": self.code, "message": MESSAGE[self.code]}
