"""Distributed provider rate limiting.

Provider limits are per *account*, not per process, so the limiter has to be shared: two workers
on two machines evaluating the same suite must not each think they own the full quota. The bucket
lives in Redis and is refilled by a Lua script so check-and-consume is atomic — doing it with
GET/SET races, and the failure mode of a racy limiter is a burst of 429s at exactly the moment a
run is most parallel.

Two independent buckets per (provider, model): requests/minute and tokens/minute. Providers
enforce both, and a suite of long prompts will hit the token ceiling long before the request one.
"""

import asyncio
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from crucible.logging import get_logger

log = get_logger(__name__)

# Returns {allowed, wait_ms}. Tokens are floats; the bucket refills continuously rather than in
# fixed windows, which avoids the thundering herd you get at the top of each minute.
_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now_ms
end

local elapsed = math.max(0, now_ms - ts)
tokens = math.min(capacity, tokens + elapsed * refill_per_ms)

if tokens >= requested then
  tokens = tokens - requested
  redis.call('HMSET', key, 'tokens', tokens, 'ts', now_ms)
  redis.call('PEXPIRE', key, ttl_ms)
  return {1, 0}
end

local deficit = requested - tokens
local wait_ms = math.ceil(deficit / refill_per_ms)
redis.call('HMSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, ttl_ms)
return {0, wait_ms}
"""


@dataclass(frozen=True)
class RateLimit:
    requests_per_minute: int
    tokens_per_minute: int

    @property
    def enabled(self) -> bool:
        return self.requests_per_minute > 0 or self.tokens_per_minute > 0


DEFAULT_LIMITS: dict[str, RateLimit] = {
    # Conservative defaults; override per project once you know your real tier.
    "anthropic": RateLimit(requests_per_minute=1000, tokens_per_minute=400_000),
    "openai": RateLimit(requests_per_minute=1000, tokens_per_minute=400_000),
    "mock": RateLimit(requests_per_minute=0, tokens_per_minute=0),
}


class RateLimiter:
    def __init__(self, redis: Redis | None, limits: dict[str, RateLimit] | None = None, namespace: str = "crucible:rl"):
        self._redis = redis
        self._limits = limits or DEFAULT_LIMITS
        self._ns = namespace
        self._script_sha: str | None = None

    def limit_for(self, provider: str) -> RateLimit:
        return self._limits.get(provider, RateLimit(0, 0))

    async def _consume(self, key: str, capacity: int, per_minute: int, amount: float) -> int:
        """Returns milliseconds to wait; 0 means the tokens were granted."""
        assert self._redis is not None
        refill_per_ms = per_minute / 60_000
        args = [capacity, refill_per_ms, int(time.time() * 1000), amount, 120_000]
        if self._script_sha is None:
            self._script_sha = await self._redis.script_load(_BUCKET_LUA)
        try:
            allowed, wait_ms = await self._redis.evalsha(self._script_sha, 1, key, *args)
        except Exception as exc:  # script cache flushed, or Redis restarted
            if "NOSCRIPT" not in str(exc):
                raise
            self._script_sha = await self._redis.script_load(_BUCKET_LUA)
            allowed, wait_ms = await self._redis.evalsha(self._script_sha, 1, key, *args)
        return 0 if int(allowed) == 1 else int(wait_ms)

    async def acquire(self, provider: str, model: str, estimated_tokens: int, *, max_wait_s: float = 60.0) -> float:
        """Block until this call is allowed. Returns the seconds spent waiting (recorded on the span).

        With Redis unavailable the limiter fails **open**: an eval run degrading into provider-side
        429s is recoverable, a run that cannot start at all is not.
        """
        limit = self.limit_for(provider)
        if self._redis is None or not limit.enabled:
            return 0.0

        waited = 0.0
        deadline = time.monotonic() + max_wait_s
        while True:
            wait_ms = 0
            try:
                if limit.requests_per_minute:
                    wait_ms = await self._consume(
                        f"{self._ns}:req:{provider}:{model}", limit.requests_per_minute, limit.requests_per_minute, 1
                    )
                if wait_ms == 0 and limit.tokens_per_minute:
                    wait_ms = await self._consume(
                        f"{self._ns}:tok:{provider}:{model}",
                        limit.tokens_per_minute,
                        limit.tokens_per_minute,
                        max(1, estimated_tokens),
                    )
            except Exception as exc:
                log.warning("rate_limiter_unavailable", provider=provider, error=str(exc))
                return waited

            if wait_ms == 0:
                return waited
            if time.monotonic() >= deadline:
                log.warning("rate_limit_wait_exceeded", provider=provider, model=model, max_wait_s=max_wait_s)
                return waited
            sleep_s = min(wait_ms / 1000, 5.0)
            await asyncio.sleep(sleep_s)
            waited += sleep_s
