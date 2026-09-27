"""Provider resolution by model name, with lazy construction.

Adapters are built on first use so a project that only evaluates Claude models never needs an
OpenAI key, and the offline mock is always available for tests and the benchmark.
"""

from crucible.config import settings
from crucible.errors import BadRequest
from crucible.providers.base import Provider
from crucible.providers.mock import MockProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._instances: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        self._instances[provider.name] = provider

    def available(self) -> dict[str, bool]:
        return {
            "mock": True,
            "anthropic": bool(settings.anthropic_api_key) or "anthropic" in self._instances,
            "openai": bool(settings.openai_api_key) or "openai" in self._instances,
        }

    def get(self, name: str) -> Provider:
        if name in self._instances:
            return self._instances[name]
        if name == "mock":
            provider: Provider = MockProvider()
        elif name == "anthropic":
            from crucible.providers.anthropic_provider import AnthropicProvider

            provider = AnthropicProvider()
        elif name == "openai":
            from crucible.providers.openai_provider import OpenAIProvider

            provider = OpenAIProvider()
        else:
            raise BadRequest(f"unknown provider {name!r}")
        self._instances[name] = provider
        return provider

    def for_model(self, model: str) -> Provider:
        if model.startswith("mock"):
            return self.get("mock")
        if model.startswith("claude-"):
            return self.get("anthropic")
        if model.startswith(("gpt", "o1", "o3", "o4")):
            return self.get("openai")
        raise BadRequest(
            f"cannot infer a provider for model {model!r}; prefix it with a known family or register an adapter"
        )

    def provider_name_for_model(self, model: str) -> str:
        if model.startswith("mock"):
            return "mock"
        if model.startswith("claude-"):
            return "anthropic"
        if model.startswith(("gpt", "o1", "o3", "o4")):
            return "openai"
        raise BadRequest(f"cannot infer a provider for model {model!r}")


_registry: ProviderRegistry | None = None


def registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
