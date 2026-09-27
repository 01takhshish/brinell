"""Retry policy.

The rule the runner depends on: retry what can succeed later, fail fast on what cannot. A 400 is
a permanent verdict about the request, and retrying it three times only delays the real error and
spends the budget on nothing.
"""

from __future__ import annotations

import random

import pytest

from crucible.errors import ProviderError, RateLimited
from crucible.runner.retry import RetryPolicy, RetryReport, with_retry


class Recorder:
    """Stands in for `asyncio.sleep` so backoff is asserted, not waited for."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def test_a_transient_failure_is_retried_until_it_succeeds():
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("503", provider="mock", retryable=True)
        return "ok"

    sleeper = Recorder()
    report = RetryReport()
    result = await with_retry(flaky, RetryPolicy(max_attempts=5), report=report, sleep=sleeper)

    assert result == "ok"
    assert report.attempts == 3
    assert len(sleeper.delays) == 2, "one sleep per retry, none after the success"


async def test_a_permanent_failure_is_not_retried():
    calls = {"n": 0}

    async def bad_request() -> str:
        calls["n"] += 1
        raise ProviderError("400: max_tokens is required", provider="mock", retryable=False)

    sleeper = Recorder()
    with pytest.raises(ProviderError):
        await with_retry(bad_request, RetryPolicy(max_attempts=5), sleep=sleeper)

    assert calls["n"] == 1, "a 400 will be a 400 forever"
    assert sleeper.delays == []


async def test_retry_after_beats_computed_backoff():
    """When a provider tells us how long to wait, guessing is worse than listening."""

    async def limited() -> str:
        raise RateLimited("slow down", provider="mock", retry_after=7.5)

    sleeper = Recorder()
    with pytest.raises(RateLimited):
        await with_retry(limited, RetryPolicy(max_attempts=3, base_delay_s=0.5), sleep=sleeper)

    assert sleeper.delays == [7.5, 7.5]


async def test_giving_up_raises_the_last_error_not_a_wrapper():
    async def always() -> str:
        raise ProviderError("529 overloaded", provider="mock", retryable=True)

    with pytest.raises(ProviderError, match="529"):
        await with_retry(always, RetryPolicy(max_attempts=2), sleep=Recorder())


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=10.0)
    rng = random.Random(0)
    # Full jitter: the delay is uniform in [0, ceiling], so assert the ceiling, not the value.
    assert all(0 <= policy.delay_for(attempt, rng) <= min(10.0, 2**attempt) for attempt in range(8))
    assert max(policy.delay_for(20, rng) for _ in range(50)) <= 10.0


def test_jitter_spreads_retries():
    """Without jitter, every worker that hit the same rate limit retries at the same instant."""
    policy = RetryPolicy(base_delay_s=2.0)
    delays = {policy.delay_for(3, random.Random(seed)) for seed in range(20)}
    assert len(delays) > 15, "delays should not collapse onto one value"
