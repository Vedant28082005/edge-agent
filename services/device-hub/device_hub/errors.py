"""Domain errors. `code` is a stable machine string; `status` the HTTP status the internal API maps it to."""
from __future__ import annotations

from typing import Any


class HubError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 400, **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = status
        self.extra = extra

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class NotFound(HubError):
    def __init__(self, code: str = "not_found", message: str = "", **extra: Any) -> None:
        super().__init__(code, message, 404, **extra)


class Conflict(HubError):
    def __init__(self, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(code, message, 409, **extra)
