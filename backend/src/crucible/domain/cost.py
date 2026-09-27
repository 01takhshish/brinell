"""Cost accounting.

Money is integer micro-dollars (1e-6 USD) end to end. Floats accumulate error across tens of
thousands of spans, and "roughly what the run cost" is not a useful number when you are deciding
whether an eval suite is affordable to run on every pull request.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

PRICING_PATH = Path(__file__).with_name("pricing.json")
MICROS_PER_USD = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    provider: str
    model: str
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    as_of: str | None
    source: str | None


class PriceBook:
    """Loaded once from pricing.json. Unknown models return None, never zero."""

    def __init__(self, data: dict[str, Any]):
        self._prices: dict[tuple[str, str], ModelPrice] = {}
        self._providers: dict[str, dict[str, Any]] = {}
        for provider, block in data.items():
            if provider.startswith("_") or not isinstance(block, dict):
                continue
            self._providers[provider] = block
            for model, p in (block.get("models") or {}).items():
                self._prices[(provider, model)] = ModelPrice(
                    provider=provider,
                    model=model,
                    input_per_mtok_usd=float(p["input"]),
                    output_per_mtok_usd=float(p["output"]),
                    as_of=block.get("as_of"),
                    source=block.get("source"),
                )

    def lookup(self, provider: str, model: str) -> ModelPrice | None:
        return self._prices.get((provider, model))

    def cost_micros(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> int | None:
        """None means "this model has no price on file" — the caller must surface that, not hide it."""
        price = self.lookup(provider, model)
        if price is None:
            return None
        usd = (input_tokens * price.input_per_mtok_usd + output_tokens * price.output_per_mtok_usd) / 1_000_000
        return round(usd * MICROS_PER_USD)

    def known_models(self) -> list[ModelPrice]:
        return sorted(self._prices.values(), key=lambda p: (p.provider, p.model))


@lru_cache(maxsize=1)
def price_book() -> PriceBook:
    return PriceBook(json.loads(PRICING_PATH.read_text(encoding="utf-8")))


def micros_to_usd(micros: int) -> float:
    return micros / MICROS_PER_USD


def format_usd(micros: int | None) -> str:
    if micros is None:
        return "unpriced"
    usd = micros_to_usd(micros)
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.6f}".rstrip("0").rstrip(".")
    return f"${usd:,.4f}".rstrip("0").rstrip(".") if usd < 1 else f"${usd:,.2f}"
