"""Deterministic offline provider.

Exists for three reasons, all of them load-bearing:

* the integration tests exercise the real runner end to end without network or spend;
* the published benchmark is reproducible by anyone who clones the repo;
* failure modes that are rare and expensive against a live API (rate limits, overload, malformed
  output, refusals) can be produced on demand and asserted on.

Same input always yields the same output, so a cache hit and a fresh call are indistinguishable —
which is exactly what makes it usable as a test oracle.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from crucible.domain.hashing import canonical_json
from crucible.errors import ProviderError, RateLimited
from crucible.providers.base import Completion, CompletionRequest, ToolCall


@dataclass
class MockBehaviour:
    """Scripted imperfection, seeded per request so it is stable across runs."""

    failure_rate: float = 0.0  # fraction of calls that raise a retryable provider error
    rate_limit_rate: float = 0.0
    latency_ms: int = 0
    quality: float = 1.0  # fraction of cases answered correctly when an expected answer is known
    verbosity: int = 1  # multiplies output tokens; lets the benchmark model chatty architectures


class MockProvider:
    name = "mock"

    def __init__(self, behaviour: MockBehaviour | None = None):
        self.behaviour = behaviour or MockBehaviour()
        self.calls = 0
        # How many times each exact request has been seen. Transient failures are drawn per
        # attempt, not per request: a failure the runner is told to retry has to be able to
        # succeed on the retry, or `failure_rate` would model permanently broken cases instead.
        # Keying on the request keeps it deterministic — the same suite fails the same way twice,
        # whatever order concurrency happens to run it in.
        self._attempts: dict[int, int] = {}

    def supports(self, model: str) -> bool:
        return model.startswith("mock")

    @staticmethod
    def _seed(request: CompletionRequest) -> int:
        digest = hashlib.blake2b(canonical_json(request.cache_payload()).encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big")

    @staticmethod
    def _unit(seed: int, salt: int) -> float:
        """Deterministic pseudo-uniform in [0, 1) derived from the request, not from wall-clock RNG."""
        mixed = hashlib.blake2b(f"{seed}:{salt}".encode(), digest_size=8).digest()
        return int.from_bytes(mixed, "big") / 2**64

    async def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        seed = self._seed(request)
        b = self.behaviour
        attempt = self._attempts[seed] = self._attempts.get(seed, 0) + 1

        if b.rate_limit_rate and self._unit(seed, 1000 + attempt) < b.rate_limit_rate:
            raise RateLimited("mock rate limit", provider=self.name, retry_after=0.01)
        if b.failure_rate and self._unit(seed, 2000 + attempt) < b.failure_rate:
            raise ProviderError("mock transient failure", provider=self.name, retryable=True)
        if b.latency_ms:
            await asyncio.sleep(b.latency_ms / 1000)

        prompt = request.messages[-1].content if request.messages else ""
        # The directive is ground truth for the *case*, so it is looked for across the whole
        # conversation rather than only in the last message. A multi-step architecture's final call
        # ends with tool output or an instruction to summarise, and if the mock only read that, the
        # benchmark would score every multi-step architecture at zero and look like a finding about
        # ReAct rather than an artefact of where the marker happened to sit.
        directive = next(
            (d for m in reversed(request.messages) if (d := self._extract_directive(m.content)) is not None),
            None,
        )
        correct = self._unit(seed, 3) < b.quality

        if request.tools and self._unit(seed, 4) < 0.5:
            tool = request.tools[0]
            return self._finish(
                request,
                text="",
                tool_calls=[ToolCall(id=f"mock_{seed % 10**6}", name=tool.name, arguments={"query": prompt[:80]})],
            )

        if directive is not None:
            answer = directive if correct else self._corrupt(directive, seed)
            body = json.dumps({"answer": answer}) if request.response_format else answer
        else:
            body = f"mock response to: {prompt[:160]}"
        body = " ".join([body] * max(1, b.verbosity))
        return self._finish(request, text=body)

    @staticmethod
    def _extract_directive(prompt: str) -> str | None:
        """Test prompts may embed `[[expect:VALUE]]`, which the mock answers correctly at rate
        `quality`. That is what lets the benchmark model an architecture that is 4% more accurate."""
        marker = "[[expect:"
        start = prompt.find(marker)
        if start == -1:
            return None
        end = prompt.find("]]", start)
        return prompt[start + len(marker) : end].strip() if end != -1 else None

    @staticmethod
    def _corrupt(value: str, seed: int) -> str:
        return f"{value[:-1]}{'x' if not value.endswith('x') else 'y'}" if value else "unknown"

    def _finish(self, request: CompletionRequest, text: str, tool_calls: list[ToolCall] | None = None) -> Completion:
        input_tokens = max(1, sum(len(m.content) for m in request.messages) // 4 + len(request.system or "") // 4)
        output_tokens = max(1, len(text) // 4)
        raw: dict[str, Any] = {"provider": "mock", "deterministic": True}
        return Completion(
            text=text,
            model=request.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason="tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls or [],
            raw=raw,
        )
