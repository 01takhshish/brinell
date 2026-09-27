"""HTTP client for the CLI.

Thin on purpose. Its one real job is turning the API's error envelope back into a message a person
can act on: `ApiError` carries the code, the message and the request id, so a support conversation
can start with "request 4f2a…" instead of "it didn't work".
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str | None, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details

    def __str__(self) -> str:
        suffix = f" (request {self.request_id})" if self.request_id else ""
        return f"{self.message}{suffix}"


class CrucibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """`transport` exists so tests can drive the CLI against the ASGI app without a socket.

        The alternative — starting a server on a port for every test — makes the suite slower and
        flakier without testing anything the transport does not already cover.
        """
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> CrucibleClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise ApiError(0, "CONNECTION_FAILED", f"cannot reach {self._client.base_url}: {exc}", None) from exc
        except httpx.TimeoutException as exc:
            raise ApiError(0, "TIMEOUT", f"{method} {path} timed out", None) from exc

        if response.status_code >= 400:
            raise _to_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)


def _to_error(response: httpx.Response) -> ApiError:
    request_id = response.headers.get("X-Request-ID")
    try:
        envelope = response.json().get("error", {})
    except ValueError:
        # A proxy or a crash can return HTML; do not pretend it was our envelope.
        return ApiError(response.status_code, "HTTP_ERROR", response.text[:200], request_id)
    return ApiError(
        response.status_code,
        envelope.get("code", "HTTP_ERROR"),
        envelope.get("message", response.reason_phrase),
        envelope.get("request_id") or request_id,
        envelope.get("details"),
    )
