"""Inbound throttling for the credential endpoints.

Login and signup are the two routes an attacker can profitably hammer: one to guess passwords, the
other to mass-create accounts. Everything else in the API sits behind a session or a project-scoped
key, so this module deliberately covers only those two — a blanket per-request limiter would mostly
throttle the dashboard's own polling.

The counters live in Redis (already in the stack for the response cache and the provider limiter)
so that two API replicas see one budget. It is a fixed window via INCR: an attacker can burst up to
twice the limit by straddling a window boundary, and that is fine — the threat is thousands of guesses
an hour, not twenty-one instead of twenty. A sliding window would double the Redis work to defend
against a burst that does not matter.

Like the provider limiter, this one fails **open**: if Redis is unreachable, the attempt is allowed
and a warning is logged. Failing closed would turn a Redis blip into "nobody can log in", which is
a worse incident than a window of unthrottled attempts. The trade-off is real and the log line is
the evidence an operator needs to know the window happened.

The per-IP dimension trusts ``request.client.host``. Behind a reverse proxy that is the proxy's
address unless uvicorn is started with ``--proxy-headers`` and ``--forwarded-allow-ips`` naming the
proxy — see the Dockerfile CMD and RUNBOOK. Without that, per-IP throttling degrades to a single
shared bucket: still a cap on total guessing, just not attribution.
"""

import hashlib
import time

from redis.asyncio import Redis

from crucible.config import settings
from crucible.errors import TooManyRequests
from crucible.logging import get_logger

log = get_logger(__name__)

_NS = "crucible:throttle"

_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def dispose_throttle() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def _key(scope: str, identifier: str, window_seconds: int) -> str:
    # Identifiers include email addresses; hashing keeps PII out of Redis keys, which show up in
    # MONITOR output, keyspace scans and debugging screenshots.
    digest = hashlib.sha256(identifier.lower().encode()).hexdigest()[:16]
    window = int(time.time() // window_seconds)
    return f"{_NS}:{scope}:{digest}:{window}"


async def check(scope: str, identifier: str, *, limit: int, window_seconds: int) -> None:
    """Count this attempt and raise ``TooManyRequests`` if the window's budget is spent.

    Attempts are counted whether or not they later succeed. Counting only failures reads as
    friendlier, but it means the throttle cannot fire until *after* the password check has run —
    and the password check (Argon2) is the expensive part worth protecting.
    """
    key = _key(scope, identifier, window_seconds)
    try:
        count = await _client().incr(key)
        if count == 1:
            await _client().expire(key, window_seconds + 1)
        if count > limit:
            ttl = await _client().ttl(key)
            raise TooManyRequests(
                "too many attempts; try again later",
                retry_after=max(1, ttl if ttl > 0 else window_seconds),
            )
    except TooManyRequests:
        raise
    except Exception as exc:
        # Fail open — see the module docstring. The scope and error are enough for an operator to
        # see both that the window happened and why.
        log.warning("throttle_unavailable", scope=scope, error=str(exc))
