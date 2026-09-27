"""Structured logging. JSON in production, human-readable in dev; every log line inside a
request carries `request_id`, and every line inside a worker carries `run_id` / `case_id`."""

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def _inject_request_id(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    rid = request_id_var.get()
    if rid:
        event_dict.setdefault("request_id", rid)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_request_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "crucible") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
