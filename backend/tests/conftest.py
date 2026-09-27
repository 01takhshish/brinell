"""Shared fixtures.

The split that matters: `tests/unit` must run with nothing installed but Python — no Postgres, no
Redis, no API keys. Everything that needs a real database lives in `tests/integration` and is
skipped (not failed) when the database is absent, so `make test` is always green on a fresh clone
and `make test-all` is the honest one.
"""

from __future__ import annotations

import pytest

from crucible.providers.mock import MockBehaviour, MockProvider
from crucible.providers.registry import ProviderRegistry


@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider(MockBehaviour())


@pytest.fixture
def registry(mock_provider: MockProvider) -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(mock_provider)
    return reg
