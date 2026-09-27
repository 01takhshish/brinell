"""Application assembly: middleware, error rendering, routes.

Two things here are worth more than the wiring. The first is that *every* failure leaves through
one handler and one envelope, so a client never has to parse three shapes of error. The second is
that an unhandled exception is logged with its traceback and returned as an opaque 500 with a
request id — the operator can find the traceback by id, and the caller learns nothing about the
internals.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from crucible.api.routers import analysis, auth, catalog, health, projects, runs
from crucible.api.throttle import dispose_throttle
from crucible.config import settings
from crucible.db.session import dispose_engine
from crucible.errors import CrucibleError
from crucible.logging import configure_logging, get_logger, request_id_var

log = get_logger("crucible.api")

REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level, settings.log_json)
    log.info("api_starting", env=settings.env, port=settings.api_port)
    yield
    # Explicit disposal: without it, asyncpg connections are closed by the garbage collector on a
    # loop that is already shutting down, which logs alarming tracebacks on every deploy.
    await dispose_engine()
    await dispose_throttle()
    log.info("api_stopped")


def _envelope(status: int, code: str, message: str, request_id: str | None, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "request_id": request_id}}
    if details is not None:
        body["error"]["details"] = details
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse(status_code=status, content=body, headers=headers)


def create_app() -> FastAPI:
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title="Crucible",
        version="0.1.0",
        summary="Evaluation and observability for LLM agents",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        # Credentials are cookies here, so the origin list must be explicit — `*` with credentials
        # is rejected by browsers anyway.
        allow_origins=[settings.app_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        """One id per request, honoured from upstream if present so a trace survives the proxy."""
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(CrucibleError)
    async def crucible_error_handler(request: Request, exc: CrucibleError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        # 5xx is our fault and gets a stack trace; 4xx is the caller's and gets one line.
        if exc.status >= 500:
            log.error("request_failed", code=exc.code, path=request.url.path, exc_info=exc)
        else:
            log.info("request_rejected", code=exc.code, path=request.url.path, message=exc.message)
        response = _envelope(exc.status, exc.code, exc.message, request_id, exc.details)
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 body is a bare list; wrapping it keeps one error shape for clients.
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ())[1:]), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return _envelope(422, "VALIDATION_ERROR", "request body failed validation", _rid(request), details)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        code = codes.get(exc.status_code, "HTTP_ERROR")
        return _envelope(exc.status_code, code, str(exc.detail), _rid(request))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = _rid(request)
        log.error("unhandled_exception", path=request.url.path, exc_info=exc)
        # The message is deliberately useless to the caller. The request id is how an operator
        # finds the traceback that is useful.
        return _envelope(500, "INTERNAL_ERROR", "an unexpected error occurred", request_id)

    for router in (health.router, auth.router, projects.router, catalog.router, runs.router, analysis.router):
        app.include_router(router, prefix="" if router is health.router else "/api/v1")

    return app


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


app = create_app()
