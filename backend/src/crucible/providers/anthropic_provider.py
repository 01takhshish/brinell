"""Anthropic adapter.

Two model-family details this has to get right, because both fail *loudly at request time* rather
than degrading:

* Sampling parameters (`temperature`) were removed on Claude 4.6 and later — sending one is a 400.
* Extended thinking is configured as `{"type": "adaptive"}` on 4.6+; older models take an explicit
  `budget_tokens`. Effort is a separate knob under `output_config`.

Capability lookup is a table rather than a try/except, so an unsupported combination is a clear
error at build time instead of a confusing 400 in the middle of a 500-case run.
"""

from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from crucible.config import settings
from crucible.errors import ProviderError, RateLimited
from crucible.providers.base import Completion, CompletionRequest, ToolCall

# Families that reject temperature/top_p and use adaptive thinking.
_ADAPTIVE_THINKING_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def accepts_sampling(model: str) -> bool:
    return not model.startswith(_ADAPTIVE_THINKING_PREFIXES)


def uses_adaptive_thinking(model: str) -> bool:
    return model.startswith(_ADAPTIVE_THINKING_PREFIXES)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, client: AsyncAnthropic | None = None):
        key = api_key or settings.anthropic_api_key
        if client is None and not key:
            raise ProviderError("ANTHROPIC_API_KEY is not configured", provider=self.name)
        # max_retries=0: retry policy belongs to the runner, which also owns the rate limiter and
        # the budget. Two independent retry loops would multiply, not compose.
        self._client = client or AsyncAnthropic(api_key=key, max_retries=0, timeout=120.0)

    def supports(self, model: str) -> bool:
        return model.startswith("claude-")

    def _build(self, request: CompletionRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.system:
            payload["system"] = request.system
        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences
        if request.tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in request.tools
            ]
        if request.temperature is not None and accepts_sampling(request.model):
            payload["temperature"] = request.temperature
        if request.thinking:
            payload["thinking"] = (
                {"type": "adaptive"}
                if uses_adaptive_thinking(request.model)
                else {"type": "enabled", "budget_tokens": max(1024, request.max_tokens // 2)}
            )
        if request.effort:
            payload["output_config"] = {"effort": request.effort}
        return payload

    async def complete(self, request: CompletionRequest) -> Completion:
        payload = self._build(request)
        try:
            message = await self._client.messages.create(**payload)
        except anthropic.RateLimitError as exc:
            retry_after = _retry_after(exc)
            raise RateLimited(str(exc), provider=self.name, retry_after=retry_after) from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise ProviderError(f"connection failure: {exc}", provider=self.name, retryable=True) from exc
        except anthropic.APIStatusError as exc:
            # 5xx and 529 (overloaded) are worth retrying; a 400 will fail identically forever.
            raise ProviderError(
                f"{exc.status_code}: {exc.message}", provider=self.name, retryable=exc.status_code >= 500
            ) from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {"value": block.input}
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=args))

        return Completion(
            text="".join(text_parts),
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
            tool_calls=tool_calls,
            # `refusal` is an ordinary 200 response, not an exception. A case that hits it is a
            # result to record, not a transport failure to retry.
            refusal=message.stop_reason == "refusal",
            raw={
                "id": message.id,
                "stop_reason": message.stop_reason,
                "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", None),
            },
        )


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    try:
        return float(header) if header else None
    except ValueError:
        return None
