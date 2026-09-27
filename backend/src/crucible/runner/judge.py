"""LLM-as-judge.

Two implementations behind one protocol:

* `AnthropicJudge` uses the SDK's structured-output path (`messages.parse` with a Pydantic model),
  so the verdict is schema-valid or the call fails — no regex-scraping a score out of prose.
* `GenericJudge` targets any other provider through the normal completion path, instructing JSON
  and parsing defensively.

The judge is deliberately a *small, cheap* model by default. A judge that costs as much as the
system under test doubles the price of every eval run, and grading against explicit criteria is a
much easier task than the one being graded.
"""

import json
import re
import time
from typing import Any

from crucible.config import settings
from crucible.domain.assertions import JudgeCall, JudgeResponse, JudgeVerdict
from crucible.domain.cost import price_book
from crucible.logging import get_logger
from crucible.providers.base import CompletionRequest, Message
from crucible.providers.registry import ProviderRegistry

log = get_logger(__name__)

SYSTEM = (
    "You are a strict, consistent evaluator. Score how well the RESPONSE satisfies the CRITERIA. "
    "Judge only what the criteria ask about; ignore style unless the criteria mention it. "
    "Return a score in [0,1] where 0 fails the criteria entirely and 1 fully satisfies it, plus one "
    "or two sentences of reasoning."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_prompt(call: JudgeCall) -> str:
    parts = [f"CRITERIA:\n{call.criteria}", f"\nINPUT:\n{call.input_text[:4000]}"]
    if call.expected_text:
        parts.append(f"\nREFERENCE ANSWER:\n{call.expected_text[:4000]}")
    parts.append(f"\nRESPONSE:\n{call.output_text[:8000]}")
    return "\n".join(parts)


class AnthropicJudge:
    def __init__(self, registry: ProviderRegistry, model: str | None = None):
        self._registry = registry
        self.model = model or settings.judge_model

    async def __call__(self, call: JudgeCall) -> JudgeResponse:
        from crucible.providers.anthropic_provider import AnthropicProvider

        model = call.model or self.model
        provider = self._registry.get("anthropic")
        assert isinstance(provider, AnthropicProvider)
        started = time.monotonic()
        message = await provider._client.messages.parse(
            model=model,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": build_prompt(call)}],
            output_format=JudgeVerdict,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        verdict = message.parsed_output or JudgeVerdict(score=0.0, reasoning="judge returned an unparseable verdict")
        cost = price_book().cost_micros("anthropic", model, message.usage.input_tokens, message.usage.output_tokens)
        return JudgeResponse(
            verdict=verdict,
            cost_micros=cost or 0,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            latency_ms=latency_ms,
            model=model,
        )


class GenericJudge:
    """Provider-agnostic fallback: instruct JSON, parse defensively, never crash a run on a
    malformed verdict — a judge that fails is a zero with an explanation, not a dead case."""

    def __init__(self, registry: ProviderRegistry, model: str | None = None):
        self._registry = registry
        self.model = model or settings.judge_model

    async def __call__(self, call: JudgeCall) -> JudgeResponse:
        model = call.model or self.model
        provider = self._registry.for_model(model)
        request = CompletionRequest(
            model=model,
            system=SYSTEM + '\nReturn ONLY JSON: {"score": <0..1>, "reasoning": "<text>"}',
            messages=[Message(role="user", content=build_prompt(call))],
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        started = time.monotonic()
        completion = await provider.complete(request)
        latency_ms = int((time.monotonic() - started) * 1000)
        verdict = _parse_verdict(completion.text)
        cost = price_book().cost_micros(provider.name, model, completion.input_tokens, completion.output_tokens)
        return JudgeResponse(
            verdict=verdict,
            cost_micros=cost or 0,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            latency_ms=latency_ms,
            model=model,
        )


def _parse_verdict(text: str) -> JudgeVerdict:
    match = _JSON_RE.search(text or "")
    if match:
        try:
            data: dict[str, Any] = json.loads(match.group(0))
            return JudgeVerdict(score=float(data.get("score", 0.0)), reasoning=str(data.get("reasoning", ""))[:2000])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    log.warning("judge_unparseable", sample=(text or "")[:200])
    return JudgeVerdict(score=0.0, reasoning="judge returned an unparseable verdict")


def build_judge(registry: ProviderRegistry, model: str | None = None) -> AnthropicJudge | GenericJudge:
    chosen = model or settings.judge_model
    if chosen.startswith("claude-") and settings.anthropic_api_key:
        return AnthropicJudge(registry, chosen)
    return GenericJudge(registry, chosen)
