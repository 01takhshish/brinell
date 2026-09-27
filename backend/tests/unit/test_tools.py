"""Tool execution during an eval.

The design rule under test: an eval never calls a real system. Tools resolve from fixtures the
case carries, and the two built-ins that do run are pure. A tool with no fixture must say so
loudly rather than quietly returning something that looks like a result.
"""

from __future__ import annotations

import pytest

from crucible.domain.hashing import canonical_json
from crucible.runner.tools import ToolBox, safe_arithmetic


def test_arithmetic_is_evaluated_without_eval():
    assert safe_arithmetic("2 + 3 * 4") == 14.0
    assert safe_arithmetic("(1200 - 200) / 4") == 250.0
    assert safe_arithmetic("-2 ** 3") == -8.0


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "[x for x in range(10)]",
        "1 if True else 2",
        "len('abc')",
    ],
)
def test_anything_that_is_not_arithmetic_is_rejected(expression: str):
    with pytest.raises((ValueError, SyntaxError)):
        safe_arithmetic(expression)


def test_a_tool_without_a_fixture_is_reported_as_unresolved():
    result = ToolBox().invoke("search_orders", {"order_id": "A-1"})
    assert result["ok"] is False
    assert result["source"] == "unresolved"
    assert "fixture" in result["error"], "the message has to tell the author how to fix it"


def test_a_fixture_answers_the_tool():
    box = ToolBox({"search_orders": {"status": "refunded"}})
    assert box.invoke("search_orders", {"order_id": "A-1"}) == {
        "ok": True,
        "source": "fixture",
        "result": {"status": "refunded"},
    }


def test_argument_specific_fixtures_win_over_the_default():
    box = ToolBox(
        {
            "lookup": {
                "__by_args__": {canonical_json({"id": "A-1"}): "found A-1"},
                "__default__": "not found",
            }
        }
    )
    assert box.invoke("lookup", {"id": "A-1"})["result"] == "found A-1"
    assert box.invoke("lookup", {"id": "A-2"})["result"] == "not found"


def test_argument_matching_ignores_key_order():
    """Models emit arguments in whatever order they like; a fixture must not depend on it."""
    box = ToolBox({"lookup": {"__by_args__": {canonical_json({"a": 1, "b": 2}): "hit"}}})
    assert box.invoke("lookup", {"b": 2, "a": 1})["result"] == "hit"


def test_a_fixture_overrides_a_builtin():
    """A case that wants the calculator to be broken must be able to say so."""
    box = ToolBox({"calculator": {"error": "service down"}})
    assert box.invoke("calculator", {"expression": "2+2"})["result"] == {"error": "service down"}


def test_a_bad_calculator_expression_fails_the_call_not_the_run():
    result = ToolBox().invoke("calculator", {"expression": "2 +"})
    assert result["ok"] is False
    assert "cannot evaluate" in result["error"]
