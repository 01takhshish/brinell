"""Deterministic response cache.

The single largest cost lever in an eval platform. A suite is re-run constantly — after a prompt
tweak that touches 3 of 200 cases, after a flaky worker retries, after CI runs the same commit
twice — and most of those provider calls are byte-identical to ones already paid for.

Keyed by a fingerprint of everything that can change the output. Cached entries record that they
were cached, so a run's reported cost distinguishes *incurred* from *avoided* spend instead of
quietly reporting zero.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

from redis.asyncio import Redis

from crucible.config import settings
from crucible.domain.hashing import request_fingerprint
from crucible.logging import get_logger
from crucible.providers.base import Completion, CompletionRequest, ToolCall

log = get_logger(__name__)

# Bump when the serialised shape changes so stale entries are ignored rather than mis-decoded.
CACHE_VERSION = "v1"


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    saved_micros: int = 0


class ResponseCache:
    def __init__(self, redis: Redis | None, ttl_seconds: int | None = None, namespace: str = "crucible:cache"):
        self._redis = redis
        self._ttl = ttl_seconds or settings.response_cache_ttl_seconds
        self._ns = namespace
        self.stats = CacheStats()

    @staticmethod
    def key_for(provider: str, request: CompletionRequest) -> str:
        return request_fingerprint(provider, request.model, request.cache_payload())

    def _redis_key(self, fingerprint: str) -> str:
        return f"{self._ns}:{CACHE_VERSION}:{fingerprint}"

    async def get(self, provider: str, request: CompletionRequest) -> Completion | None:
        if self._redis is None or not settings.response_cache_enabled:
            return None
        try:
            raw = await self._redis.get(self._redis_key(self.key_for(provider, request)))
        except Exception as exc:
            log.warning("cache_unavailable", error=str(exc))
            return None
        if raw is None:
            self.stats.misses += 1
            return None
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return Completion(
            text=payload["text"],
            model=payload["model"],
            input_tokens=payload["input_tokens"],
            output_tokens=payload["output_tokens"],
            stop_reason=payload.get("stop_reason"),
            tool_calls=[ToolCall(**tc) for tc in payload.get("tool_calls", [])],
            raw={**payload.get("raw", {}), "cached": True},
            refusal=payload.get("refusal", False),
        )

    async def set(self, provider: str, request: CompletionRequest, completion: Completion) -> None:
        if self._redis is None or not settings.response_cache_enabled:
            return
        payload = {
            "text": completion.text,
            "model": completion.model,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "stop_reason": completion.stop_reason,
            "tool_calls": [asdict(tc) for tc in completion.tool_calls],
            "raw": completion.raw,
            "refusal": completion.refusal,
        }
        try:
            await self._redis.set(self._redis_key(self.key_for(provider, request)), json.dumps(payload), ex=self._ttl)
        except Exception as exc:
            log.warning("cache_write_failed", error=str(exc))
