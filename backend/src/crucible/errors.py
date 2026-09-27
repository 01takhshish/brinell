"""One error envelope for the whole API:

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": ...}}

Handlers raise these; a single exception handler renders them. Unknown exceptions never leak
internals in production.
"""

from typing import Any


class CrucibleError(Exception):
    status: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, details: Any = None, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code
        if status:
            self.status = status


class BadRequest(CrucibleError):
    status, code = 400, "BAD_REQUEST"


class Unauthorized(CrucibleError):
    status, code = 401, "UNAUTHORIZED"


class Forbidden(CrucibleError):
    status, code = 403, "FORBIDDEN"


class NotFound(CrucibleError):
    status, code = 404, "NOT_FOUND"

    def __init__(self, entity: str = "Resource", **kw: Any):
        super().__init__(f"{entity} not found", **kw)


class Conflict(CrucibleError):
    status, code = 409, "CONFLICT"


class Unprocessable(CrucibleError):
    # Nothing raises this today: the 422s the API actually returns come from FastAPI's own request
    # validation, which `api/app.py` catches and renders into this same envelope. It stays because
    # this is a vocabulary, not a call graph — a gap at 422 is an invitation for the next handler to
    # write `BadRequest(..., status=422)` and put a second spelling of the same status in the API.
    status, code = 422, "UNPROCESSABLE"


class TooManyRequests(CrucibleError):
    """Inbound throttle tripped — distinct from `RateLimited`, which is a *provider* telling *us* no.

    `retry_after` is surfaced as a Retry-After header by the exception handler, because a client
    that knows how long to wait backs off correctly and one that does not just retries in a loop.
    """

    status, code = 429, "RATE_LIMITED"

    def __init__(self, message: str, *, retry_after: int | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ProviderError(CrucibleError):
    """A provider call failed in a way the runner should record against the case, not the run."""

    status, code = 502, "PROVIDER_ERROR"

    def __init__(self, message: str, *, provider: str, retryable: bool = False, **kw: Any):
        super().__init__(message, **kw)
        self.provider = provider
        self.retryable = retryable


class RateLimited(ProviderError):
    status, code = 429, "PROVIDER_RATE_LIMITED"

    def __init__(self, message: str, *, provider: str, retry_after: float | None = None, **kw: Any):
        super().__init__(message, provider=provider, retryable=True, **kw)
        self.retry_after = retry_after
