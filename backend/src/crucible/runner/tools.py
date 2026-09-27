"""Tool execution during an eval.

Evals must be reproducible and must never touch production systems, so tools are resolved from
**fixtures carried by the case** rather than executed for real. A case declares what its tools
return; the agent's job is to call the right tool with the right arguments, and that is what is
being measured.

Two exceptions are executed for real because they are pure and safe: `calculator` (arithmetic only)
and `echo`. Anything else without a fixture returns a clearly-labelled miss, which shows up in the
trace instead of silently looking like a successful call.
"""

import ast
import operator
from collections.abc import Callable
from typing import Any

from crucible.domain.hashing import canonical_json

_OPS: dict[type[ast.AST], Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_arithmetic(expression: str) -> float:
    """Evaluates arithmetic without `eval`. Anything that is not a number or an operator raises."""

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return float(_OPS[type(node.op)](_eval(node.left), _eval(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return float(_OPS[type(node.op)](_eval(node.operand)))
        raise ValueError(f"unsupported expression element: {ast.dump(node)[:80]}")

    return _eval(ast.parse(expression, mode="eval"))


class ToolBox:
    """Fixtures are matched most-specific first: exact (name, arguments), then name only."""

    def __init__(self, fixtures: dict[str, Any] | None = None):
        self._fixtures = fixtures or {}

    def _fixture_for(self, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        by_name = self._fixtures.get(name)
        if by_name is None:
            return False, None
        if isinstance(by_name, dict) and "__by_args__" in by_name:
            key = canonical_json(arguments)
            table = by_name["__by_args__"]
            if key in table:
                return True, table[key]
            return True, by_name.get("__default__", None)
        return True, by_name

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        found, value = self._fixture_for(name, arguments)
        if found:
            return {"ok": True, "source": "fixture", "result": value}
        if name == "calculator":
            expression = str(arguments.get("expression", arguments.get("query", "")))
            try:
                return {"ok": True, "source": "builtin", "result": safe_arithmetic(expression)}
            except (ValueError, SyntaxError, ZeroDivisionError) as exc:
                return {"ok": False, "source": "builtin", "error": f"cannot evaluate {expression!r}: {exc}"}
        if name == "echo":
            return {"ok": True, "source": "builtin", "result": arguments}
        return {
            "ok": False,
            "source": "unresolved",
            "error": f"no fixture for tool {name!r}; add one to the case so the run stays reproducible",
        }
