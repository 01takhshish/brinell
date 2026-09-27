"""OpenAI adapter. Same contract as the Anthropic one; differences are confined to this file.

Note the token-field rename (`prompt_tokens`/`completion_tokens` vs `input_tokens`/`output_tokens`)
and that reasoning models take `max_completion_tokens` rather than `max_tokens` — normalising both
here is the entire reason this abstraction exists.
"""

import json
from typing import Any

import openai
from openai import AsyncOpenAI

from crucible.config import settings
from crucible.errors import ProviderError, RateLimited
from crucible.providers.base import Completion, CompletionRequest, ToolCall

_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, client: AsyncOpenAI | None = None):
        key = api_key or settings.openai_api_key
        if client is None and not key:
            raise ProviderError("OPENAI_API_KEY is not configured", provider=self.name)
        self._client = client or AsyncOpenAI(api_key=key, max_retries=0, timeout=120.0)

    def supports(self, model: str) -> bool:
        return model.startswith(("gpt", "o1", "o3", "o4"))

    def _build(self, request: CompletionRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)

        payload: dict[str, Any] = {"model": request.model, "messages": messages}
        reasoning = request.model.startswith(_REASONING_PREFIXES)
        payload["max_completion_tokens" if reasoning else "max_tokens"] = request.max_tokens
        if request.temperature is not None and not reasoning:
            payload["temperature"] = request.temperature
        if request.stop_sequences and not reasoning:
            payload["stop"] = request.stop_sequences
        if request.effort and reasoning:
            payload["reasoning_effort"] = request.effort
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in request.tools
            ]
        if request.response_format:
            payload["response_format"] = request.response_format
        return payload

    async def complete(self, request: CompletionRequest) -> Completion:
        try:
            response = await self._client.chat.completions.create(**self._build(request))
        except openai.RateLimitError as exc:
            raise RateLimited(str(exc), provider=self.name) from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise ProviderError(f"connection failure: {exc}", provider=self.name, retryable=True) from exc
        except openai.APIStatusError as exc:
            raise ProviderError(
                f"{exc.status_code}: {exc.message}", provider=self.name, retryable=exc.status_code >= 500
            ) from exc

        choice = response.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            fn = getattr(call, "function", None)
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.arguments}
            tool_calls.append(ToolCall(id=call.id, name=fn.name, arguments=args))

        usage = response.usage
        return Completion(
            text=message.content or "",
            model=response.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            stop_reason=choice.finish_reason,
            tool_calls=tool_calls,
            refusal=bool(getattr(message, "refusal", None)),
            raw={"id": response.id, "finish_reason": choice.finish_reason},
        )
