"""Provider abstraction.

One normalised request/response shape across Anthropic, OpenAI and the offline mock, so the runner,
the cost accounting and the trace view never branch on vendor. Adapters translate *only*; every
policy decision (retries, rate limits, caching, budgets) lives in `crucible.runner`.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    messages: list[Message]
    system: str | None = None
    max_tokens: int = 2048
    tools: list[ToolSpec] = field(default_factory=list)
    stop_sequences: list[str] = field(default_factory=list)
    temperature: float | None = None
    thinking: bool = False
    effort: str | None = None
    response_format: dict[str, Any] | None = None

    def cache_payload(self) -> dict[str, Any]:
        """Everything that can change the output — and nothing that cannot."""
        return {
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "system": self.system,
            "max_tokens": self.max_tokens,
            "tools": [{"name": t.name, "description": t.description, "schema": t.input_schema} for t in self.tools],
            "stop": self.stop_sequences,
            "temperature": self.temperature,
            "thinking": self.thinking,
            "effort": self.effort,
            "response_format": self.response_format,
        }


@dataclass
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    refusal: bool = False


@runtime_checkable
class Provider(Protocol):
    name: str

    async def complete(self, request: CompletionRequest) -> Completion: ...

    def supports(self, model: str) -> bool: ...
