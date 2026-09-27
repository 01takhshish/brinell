"""Cost accounting. The one behaviour this file exists to protect: an unpriced model reports
`None`, never `0`. A silent zero turns "we do not know what this cost" into "this was free",
which is the kind of number that ends up in a slide."""

from crucible.domain.cost import format_usd, micros_to_usd, price_book


def test_known_model_is_priced():
    book = price_book()
    cost = book.cost_micros("anthropic", "claude-haiku-4-5", 1_000_000, 1_000_000)
    assert cost is not None and cost > 0


def test_unknown_model_returns_none_not_zero():
    assert price_book().cost_micros("anthropic", "claude-model-from-the-future", 1000, 1000) is None


def test_zero_tokens_on_a_known_model_is_zero_not_none():
    """Distinguishing "no tokens" from "no price" is the whole point of the None."""
    assert price_book().cost_micros("anthropic", "claude-haiku-4-5", 0, 0) == 0


def test_output_tokens_cost_more_than_input_tokens():
    book = price_book()
    inp = book.cost_micros("anthropic", "claude-haiku-4-5", 1_000_000, 0)
    out = book.cost_micros("anthropic", "claude-haiku-4-5", 0, 1_000_000)
    assert inp is not None and out is not None and out > inp


def test_cost_is_linear_in_tokens():
    book = price_book()
    one = book.cost_micros("anthropic", "claude-haiku-4-5", 1_000_000, 0)
    two = book.cost_micros("anthropic", "claude-haiku-4-5", 2_000_000, 0)
    assert one is not None and two is not None and two == 2 * one


def test_prices_are_integers_micros_not_floats():
    """Money is integer micro-dollars everywhere; a float here would drift over a 10k-case run."""
    cost = price_book().cost_micros("anthropic", "claude-haiku-4-5", 12_345, 6_789)
    assert isinstance(cost, int)


def test_openai_section_is_deliberately_unpriced():
    """We do not ship guessed prices. Until someone copies the real numbers from the pricing page,
    OpenAI models report `None` and the platform says "cost unknown" instead of inventing one."""
    assert price_book().cost_micros("openai", "gpt-4o-mini", 1000, 1000) is None


def test_formatting_says_unpriced_rather_than_zero_dollars():
    assert format_usd(None) == "unpriced"
    assert format_usd(0) == "$0.00"


def test_micros_to_usd():
    assert micros_to_usd(1_500_000) == 1.5
