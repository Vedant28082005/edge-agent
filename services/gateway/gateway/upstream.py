"""HTTP clients for device-hub, agent, vision-node, yolo-node. Secrets are read from files and never logged."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncIterator

import httpx

from .security import SECRETS

log = logging.getLogger("gateway.upstream")


class UpstreamError(Exception):
    """Network failure, timeout or an unusable reply. `status` is the HTTP status the gateway answers with."""

    def __init__(self, service: str, status: int = 502, code: str = "upstream_error") -> None:
        super().__init__(f"{service}: {code}")
        self.service, self.status, self.code = service, status, code


def read_secret_file(path: str, what: str) -> str | None:
    try:
        v = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("%s secret file is not readable", what)
        return None
    SECRETS.add(v)
    return v or None


class JsonClient:
    def __init__(self, name: str, base_url: str, timeout_s: float, headers: dict | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = name
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, headers=headers or {}, transport=transport)

    async def call(self, method: str, path: str, *, json=None, params=None, headers=None, files=None,
                   timeout: float | None = None) -> tuple[int, object]:
        try:
            r = await self._http.request(method, path, json=json, params=params, headers=headers, files=files,
                                         timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException:
            raise UpstreamError(self.name, 504, "upstream_timeout") from None
        except httpx.HTTPError as e:
            log.warning("%s call failed: %s", self.name, type(e).__name__)
            raise UpstreamError(self.name) from None
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = {"error": "bad_upstream_reply"}
        return r.status_code, body

    async def sse(self, path: str, *, params=None, headers=None) -> AsyncIterator[tuple[str | None, str, str]]:
        """Yield (id, event, data) frames of an SSE response until it closes."""
        try:
            async with self._http.stream("GET", path, params=params, headers=headers,
                                         timeout=httpx.Timeout(10.0, read=30.0)) as r:
                if r.status_code != 200:
                    raise UpstreamError(self.name, 502, f"http_{r.status_code}")
                fid, ev, data = None, "message", []
                async for line in r.aiter_lines():
                    if line == "":
                        if data:
                            yield fid, ev, "\n".join(data)
                        fid, ev, data = None, "message", []
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("id:"):
                        fid = line[3:].strip()
                    elif line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
        except httpx.HTTPError as e:
            raise UpstreamError(self.name, 502, type(e).__name__) from None

    def stream(self, method: str, path: str, **kw):
        return self._http.stream(method, path, **kw)

    async def aclose(self) -> None:
        await self._http.aclose()


class HubClient(JsonClient):
    def __init__(self, base_url: str, secret: str | None, timeout_s: float = 5.0, transport=None) -> None:
        super().__init__("device-hub", base_url, timeout_s, {"X-Service-Secret": secret} if secret else {}, transport)

    async def hub(self, method: str, path: str, actor: str | None = None, **kw):
        h = dict(kw.pop("headers", None) or {})
        if actor:
            h["X-Actor"] = actor[:64]
        return await self.call(method, path, headers=h, **kw)


class AgentClient(JsonClient):
    """agent-internal.yaml. Every call (including /v1/health and /v1/events) carries the agent-api secret."""

    def __init__(self, base_url: str, secret: str | None, timeout_s: float = 30.0, transport=None) -> None:
        super().__init__("agent", base_url, timeout_s, {"X-Service-Secret": secret} if secret else {}, transport)

    async def deliver_confirmation(self, request_id: str, body: dict) -> bool:
        """POST /v1/confirmations/{request_id}: 202 = accepted. Anything else (404, 409 action_hash_mismatch, 401, 5xx,
        network) is a failed hand-off. The token travels only in this loopback request body."""
        status, _ = await self.call("POST", f"/v1/confirmations/{request_id}", json=body)
        return status == 202
