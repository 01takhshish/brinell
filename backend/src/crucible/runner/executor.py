"""Executing one case.

`LlmCaller` is where every policy decision converges — cache, rate limit, retry, cost attribution,
tracing — so that agent code stays about *reasoning* and every provider call in the system is
observed identically, whichever architecture made it.

Nothing here touches the database. The executor returns a plain result object and the queue layer
persists it inside the transaction that also releases the case, which is what makes a crash
mid-case recoverable (docs/adr/0002).
"""

import time
from dataclasses import dataclass
from typing import Any

from crucible.domain.assertions import CaseVerdict, EvalContext, JudgeFn, evaluate_case
from crucible.domain.cost import price_book
from crucible.errors import ProviderError
from crucible.logging import get_logger
from crucible.providers.base import Completion, CompletionRequest
from crucible.providers.registry import ProviderRegistry
from crucible.runner.agents import ARCHITECTURE_RUNNERS, AgentOutput, TargetConfig
from crucible.runner.cache import ResponseCache
from crucible.runner.ratelimit import RateLimiter
from crucible.runner.retry import RetryPolicy, RetryReport, with_retry
from crucible.runner.tools import ToolBox

log = get_logger(__name__)


@dataclass
class SpanRecord:
    seq: int
    kind: str
    name: str
    provider: str | None = None
    model: str | None = None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    latency_ms: int = 0
    cache_hit: bool = False
    retries: int = 0
    error: str | None = None


@dataclass
class CaseOutcome:
    status: str  # passed | failed | error
    output: dict[str, Any] | None
    verdict: CaseVerdict | None
    spans: list[SpanRecord]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    unpriced: bool = False
    latency_ms: int = 0
    cache_hit: bool = False
    error: str | None = None


class LlmCaller:
    """One per case. Accumulates spans and totals for that case."""

    def __init__(
        self,
        registry: ProviderRegistry,
        cache: ResponseCache | None = None,
        limiter: RateLimiter | None = None,
        retry_policy: RetryPolicy | None = None,
        *,
        redact: bool = False,
    ):
        self._registry = registry
        self._cache = cache
        self._limiter = limiter
        self._policy = retry_policy or RetryPolicy()
        self._redact = redact
        self.spans: list[SpanRecord] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_micros = 0
        self.unpriced = False
        self.cache_hits = 0

    def _next_seq(self) -> int:
        return len(self.spans)

    def _capture_request(self, request: CompletionRequest) -> dict[str, Any]:
        if self._redact:
            return {"model": request.model, "messages": len(request.messages), "redacted": True}
        return {
            "model": request.model,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_tokens,
            "tools": [t.name for t in request.tools],
            "temperature": request.temperature,
            "thinking": request.thinking,
            "effort": request.effort,
        }

    async def call(self, request: CompletionRequest, *, name: str, kind: str = "llm") -> Completion:
        provider = self._registry.for_model(request.model)
        started = time.monotonic()
        report = RetryReport()
        cached = await self._cache.get(provider.name, request) if self._cache else None

        if cached is not None:
            completion, cache_hit, wait_s = cached, True, 0.0
        else:
            cache_hit = False
            estimated = sum(len(m.content) for m in request.messages) // 4 + request.max_tokens
            wait_s = await self._limiter.acquire(provider.name, request.model, estimated) if self._limiter else 0.0
            try:
                completion = await with_retry(lambda: provider.complete(request), self._policy, report=report)
            except ProviderError as exc:
                self.spans.append(
                    SpanRecord(
                        seq=self._next_seq(),
                        kind=kind,
                        name=name,
                        provider=provider.name,
                        model=request.model,
                        request=self._capture_request(request),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        retries=max(0, report.attempts - 1),
                        error=str(exc)[:1000],
                    )
                )
                raise
            if self._cache:
                await self._cache.set(provider.name, request, completion)

        latency_ms = int((time.monotonic() - started) * 1000)
        # A cache hit costs nothing; recording the price it *would* have cost is what lets the run
        # report avoided spend instead of pretending the call was free.
        priced = price_book().cost_micros(
            provider.name, completion.model, completion.input_tokens, completion.output_tokens
        )
        if priced is None:
            self.unpriced = True
        cost = 0 if cache_hit else (priced or 0)

        self.input_tokens += 0 if cache_hit else completion.input_tokens
        self.output_tokens += 0 if cache_hit else completion.output_tokens
        self.cost_micros += cost
        self.cache_hits += 1 if cache_hit else 0

        self.spans.append(
            SpanRecord(
                seq=self._next_seq(),
                kind=kind,
                name=name,
                provider=provider.name,
                model=completion.model,
                request=self._capture_request(request),
                response={
                    "text": completion.text[:8000] if not self._redact else "(redacted)",
                    "stop_reason": completion.stop_reason,
                    "tool_calls": [{"name": t.name, "arguments": t.arguments} for t in completion.tool_calls],
                    "refusal": completion.refusal,
                    "avoided_cost_micros": priced if cache_hit else None,
                    "rate_limit_wait_s": round(wait_s, 3) or None,
                },
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cost_micros=cost,
                latency_ms=latency_ms,
                cache_hit=cache_hit,
                retries=max(0, report.attempts - 1),
            )
        )
        return completion

    def invoke_tool(self, tools: ToolBox, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        result = tools.invoke(name, arguments)
        self.spans.append(
            SpanRecord(
                seq=self._next_seq(),
                kind="tool",
                name=f"tool.{name}",
                request={"arguments": arguments},
                response=result,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=None if result.get("ok") else str(result.get("error"))[:500],
            )
        )
        return result


class CaseExecutor:
    def __init__(
        self,
        registry: ProviderRegistry,
        cache: ResponseCache | None = None,
        limiter: RateLimiter | None = None,
        retry_policy: RetryPolicy | None = None,
        judge: JudgeFn | None = None,
    ):
        self._registry = registry
        self._cache = cache
        self._limiter = limiter
        self._policy = retry_policy
        self._judge = judge

    async def execute(
        self,
        *,
        target_config: dict[str, Any],
        case_input: dict[str, Any],
        expected: dict[str, Any] | None,
        assertions: list[Any],
    ) -> CaseOutcome:
        config = TargetConfig.from_dict(target_config)
        runner = ARCHITECTURE_RUNNERS.get(config.architecture)
        caller = LlmCaller(self._registry, self._cache, self._limiter, self._policy)
        started = time.monotonic()

        if runner is None:
            return CaseOutcome(
                status="error",
                output=None,
                verdict=None,
                spans=[],
                error=f"unknown architecture {config.architecture!r}; expected one of {sorted(ARCHITECTURE_RUNNERS)}",
            )

        toolbox = ToolBox(case_input.get("tool_fixtures"))
        try:
            agent_output: AgentOutput = await runner(caller, config, case_input, toolbox)
        except ProviderError as exc:
            # A provider failure is the *case's* result, not the run's. One unreachable model should
            # not abandon the other 499 cases.
            return CaseOutcome(
                status="error",
                output=None,
                verdict=None,
                spans=caller.spans,
                input_tokens=caller.input_tokens,
                output_tokens=caller.output_tokens,
                cost_micros=caller.cost_micros,
                unpriced=caller.unpriced,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=str(exc)[:2000],
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        output = agent_output.as_output_dict()
        verdict = await evaluate_case(
            assertions,
            EvalContext(
                output=output,
                input=case_input,
                expected=expected,
                latency_ms=latency_ms,
                cost_micros=None if caller.unpriced else caller.cost_micros,
                judge=self._judge,
            ),
        )
        for outcome in verdict.outcomes:
            if outcome.cost_micros:
                caller.spans.append(
                    SpanRecord(
                        seq=len(caller.spans),
                        kind="judge",
                        name=f"judge.{outcome.type}",
                        response={"score": outcome.score, "detail": outcome.detail},
                        cost_micros=outcome.cost_micros,
                        latency_ms=outcome.latency_ms,
                    )
                )
                caller.cost_micros += outcome.cost_micros

        return CaseOutcome(
            status="passed" if verdict.passed else "failed",
            output=output,
            verdict=verdict,
            spans=caller.spans,
            input_tokens=caller.input_tokens,
            output_tokens=caller.output_tokens,
            cost_micros=caller.cost_micros,
            unpriced=caller.unpriced,
            latency_ms=latency_ms,
            cache_hit=caller.cache_hits > 0,
        )
