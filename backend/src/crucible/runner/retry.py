"""Retry policy: exponential backoff with **full jitter**.

Equal-jitter and fixed backoff both leave retries correlated, so a rate-limited fan-out re-collides
on every wave. Full jitter (`sleep = uniform(0, base * 2**n)`) spreads them and empirically
minimises both contention and completion time.

A provider's own `retry-after` always wins over the computed delay — it is authoritative and
ignoring it is how an account gets throttled harder.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from crucible.errors import ProviderError, RateLimited
from crucible.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        return (rng or random).uniform(0, ceiling)


@dataclass
class RetryReport:
    attempts: int = 0
    total_delay_s: float = 0.0
    last_error: str | None = None


async def with_retry[T](
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    report: RetryReport | None = None,
    rng: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Retries only what is worth retrying: rate limits, timeouts, connection failures and 5xx.

    A 400 means the request is wrong and will be wrong every time; retrying it burns the budget and
    delays the real error.
    """
    report = report or RetryReport()
    last_exc: Exception | None = None

    for attempt in range(policy.max_attempts):
        report.attempts = attempt + 1
        try:
            return await fn()
        except RateLimited as exc:
            last_exc = exc
            delay = exc.retry_after if exc.retry_after is not None else policy.delay_for(attempt, rng)
        except ProviderError as exc:
            last_exc = exc
            if not exc.retryable:
                raise
            delay = policy.delay_for(attempt, rng)

        report.last_error = str(last_exc)
        if attempt == policy.max_attempts - 1:
            break
        log.info("provider_retry", attempt=attempt + 1, delay_s=round(delay, 3), error=str(last_exc)[:200])
        await sleep(delay)
        report.total_delay_s += delay

    assert last_exc is not None
    raise last_exc
