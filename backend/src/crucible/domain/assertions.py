"""Assertion engine — how a case is scored.

Design rules:

* Every assertion returns a **score in [0, 1]** as well as pass/fail. Binary pass/fail alone makes
  regressions look like cliffs and hides "the answer got worse but still passed".
* A case's score is the weighted mean of its assertions; a case **passes only if every assertion
  passes**. Averaging pass/fail would let a confident wrong answer slide through on the strength
  of a latency check.
* Assertion specs are validated as a discriminated union, so a typo in a suite definition is a 422
  at configuration time rather than a silent always-passing check in production.
* Evaluation is pure apart from the judge, which is injected. That keeps the whole engine testable
  without a network.
"""

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol

import jsonschema
from jsonpath_ng import parse as parse_jsonpath
from pydantic import BaseModel, Field, TypeAdapter

# ─── Specs ───────────────────────────────────────────────────────────────────


class _Base(BaseModel):
    weight: float = Field(default=1.0, gt=0, le=100)
    path: str | None = Field(default=None, description="JSONPath into the output; defaults to the output text")
    description: str | None = None


class _Expected(_Base):
    """Assertions that compare against something. Resolution order: literal `value`, then
    `value_from` (a dot path into `expected`/`input`), then `expected.answer`, then `expected`."""

    value: Any = None
    value_from: str | None = None


class EqualsAssertion(_Expected):
    type: Literal["equals"] = "equals"
    ignore_case: bool = True
    trim: bool = True


class ContainsAssertion(_Expected):
    type: Literal["contains"] = "contains"
    ignore_case: bool = True


class NotContainsAssertion(_Expected):
    type: Literal["not_contains"] = "not_contains"
    ignore_case: bool = True


class RegexAssertion(_Base):
    type: Literal["regex"] = "regex"
    pattern: str
    should_match: bool = True


class FuzzyAssertion(_Expected):
    type: Literal["fuzzy"] = "fuzzy"
    min_ratio: float = Field(default=0.85, ge=0, le=1)


class JsonSchemaAssertion(_Base):
    type: Literal["json_schema"] = "json_schema"
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"populate_by_name": True}


class JsonPathEqualsAssertion(_Expected):
    type: Literal["json_path_equals"] = "json_path_equals"
    path: str = "$"


class NumericWithinAssertion(_Expected):
    type: Literal["numeric_within"] = "numeric_within"
    tolerance: float = Field(default=0.0, ge=0)


class LatencyBudgetAssertion(_Base):
    type: Literal["latency_budget_ms"] = "latency_budget_ms"
    max_ms: int = Field(gt=0)


class CostBudgetAssertion(_Base):
    type: Literal["cost_budget_micros"] = "cost_budget_micros"
    max_micros: int = Field(gt=0)


class LlmJudgeAssertion(_Base):
    type: Literal["llm_judge"] = "llm_judge"
    criteria: str
    min_score: float = Field(default=0.7, ge=0, le=1)
    model: str | None = None


class RubricCriterion(BaseModel):
    name: str
    description: str
    weight: float = Field(default=1.0, gt=0)


class RubricAssertion(_Base):
    type: Literal["rubric"] = "rubric"
    criteria: list[RubricCriterion] = Field(min_length=1, max_length=12)
    min_score: float = Field(default=0.7, ge=0, le=1)
    model: str | None = None


AssertionSpec = Annotated[
    EqualsAssertion
    | ContainsAssertion
    | NotContainsAssertion
    | RegexAssertion
    | FuzzyAssertion
    | JsonSchemaAssertion
    | JsonPathEqualsAssertion
    | NumericWithinAssertion
    | LatencyBudgetAssertion
    | CostBudgetAssertion
    | LlmJudgeAssertion
    | RubricAssertion,
    Field(discriminator="type"),
]

ASSERTION_ADAPTER: TypeAdapter[AssertionSpec] = TypeAdapter(AssertionSpec)
ASSERTION_LIST_ADAPTER: TypeAdapter[list[AssertionSpec]] = TypeAdapter(list[AssertionSpec])

ASSERTION_TYPES = [
    "equals",
    "contains",
    "not_contains",
    "regex",
    "fuzzy",
    "json_schema",
    "json_path_equals",
    "numeric_within",
    "latency_budget_ms",
    "cost_budget_micros",
    "llm_judge",
    "rubric",
]


# ─── Judge ───────────────────────────────────────────────────────────────────


class JudgeVerdict(BaseModel):
    """Structured output contract for the judge model."""

    score: float = Field(ge=0, le=1, description="0 = fails the criteria entirely, 1 = fully satisfies it")
    reasoning: str = Field(description="One or two sentences justifying the score")


@dataclass
class JudgeCall:
    criteria: str
    output_text: str
    expected_text: str | None
    input_text: str
    model: str | None = None


@dataclass
class JudgeResponse:
    verdict: JudgeVerdict
    cost_micros: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""


class JudgeFn(Protocol):
    async def __call__(self, call: JudgeCall) -> JudgeResponse: ...


# ─── Evaluation ──────────────────────────────────────────────────────────────


@dataclass
class EvalContext:
    output: dict[str, Any]
    input: dict[str, Any]
    expected: dict[str, Any] | None = None
    latency_ms: int = 0
    cost_micros: int | None = None
    judge: JudgeFn | None = None


@dataclass
class AssertionOutcome:
    type: str
    passed: bool
    score: float
    weight: float
    detail: str
    description: str | None = None
    cost_micros: int = 0
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "passed": self.passed,
            "score": round(self.score, 4),
            "weight": self.weight,
            "detail": self.detail,
            "description": self.description,
            "cost_micros": self.cost_micros,
            "latency_ms": self.latency_ms,
        }


@dataclass
class CaseVerdict:
    passed: bool
    score: float
    outcomes: list[AssertionOutcome] = field(default_factory=list)
    judge_cost_micros: int = 0

    def to_list(self) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self.outcomes]


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return json.dumps(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _dot_get(source: dict[str, Any], path: str) -> Any:
    cur: Any = source
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def extract_actual(spec: Any, ctx: EvalContext) -> Any:
    """The value under test: a JSONPath into the output, or the output's text."""
    path = getattr(spec, "path", None)
    if not path:
        return ctx.output.get("text", ctx.output)
    matches = parse_jsonpath(path).find(ctx.output)
    if not matches:
        return None
    return matches[0].value if len(matches) == 1 else [m.value for m in matches]


def extract_expected(spec: Any, ctx: EvalContext) -> Any:
    if getattr(spec, "value", None) is not None:
        return spec.value
    source = {"expected": ctx.expected or {}, "input": ctx.input}
    if getattr(spec, "value_from", None):
        return _dot_get(source, spec.value_from)
    if ctx.expected is None:
        return None
    if isinstance(ctx.expected, dict) and "answer" in ctx.expected:
        return ctx.expected["answer"]
    return ctx.expected


def _norm(value: Any, *, ignore_case: bool = False, trim: bool = True) -> str:
    text = _stringify(value)
    if trim:
        text = text.strip()
    return text.lower() if ignore_case else text


async def evaluate_assertion(spec: Any, ctx: EvalContext) -> AssertionOutcome:
    kind = spec.type
    weight = spec.weight
    desc = spec.description

    def result(passed: bool, score: float, detail: str, **extra: int) -> AssertionOutcome:
        return AssertionOutcome(
            type=kind,
            passed=passed,
            score=max(0.0, min(1.0, score)),
            weight=weight,
            detail=detail,
            description=desc,
            **extra,
        )

    # A `value_from` that resolves to nothing is almost always a typo in the suite, not a genuine
    # empty expectation. Saying so beats reporting `expected ''`, which reads like a model error.
    if getattr(spec, "value_from", None) and extract_expected(spec, ctx) is None:
        return result(
            False,
            0.0,
            f"value_from {spec.value_from!r} resolved to nothing; paths are rooted at "
            "'expected' and 'input' (for example 'expected.answer')",
        )

    if kind == "equals":
        actual, expected = extract_actual(spec, ctx), extract_expected(spec, ctx)
        a = _norm(actual, ignore_case=spec.ignore_case, trim=spec.trim)
        e = _norm(expected, ignore_case=spec.ignore_case, trim=spec.trim)
        ok = a == e
        return result(ok, 1.0 if ok else 0.0, "equal" if ok else f"expected {e!r}, got {a!r}")

    if kind in ("contains", "not_contains"):
        haystack = _norm(extract_actual(spec, ctx), ignore_case=spec.ignore_case)
        needle = _norm(extract_expected(spec, ctx), ignore_case=spec.ignore_case)
        present = needle in haystack
        ok = present if kind == "contains" else not present
        verb = "contains" if present else "does not contain"
        return result(ok, 1.0 if ok else 0.0, f"output {verb} {needle!r}")

    if kind == "regex":
        text = _stringify(extract_actual(spec, ctx))
        matched = re.search(spec.pattern, text, re.DOTALL) is not None
        ok = matched == spec.should_match
        return result(ok, 1.0 if ok else 0.0, f"pattern {spec.pattern!r} {'matched' if matched else 'did not match'}")

    if kind == "fuzzy":
        a = _norm(extract_actual(spec, ctx), ignore_case=True)
        e = _norm(extract_expected(spec, ctx), ignore_case=True)
        ratio = difflib.SequenceMatcher(None, a, e).ratio()
        ok = ratio >= spec.min_ratio
        # Score is the similarity itself, so a near miss reads differently from nonsense.
        return result(ok, ratio, f"similarity {ratio:.3f} (threshold {spec.min_ratio})")

    if kind == "json_schema":
        actual = extract_actual(spec, ctx)
        if isinstance(actual, str):
            try:
                actual = json.loads(actual)
            except json.JSONDecodeError as exc:
                return result(False, 0.0, f"output is not valid JSON: {exc.msg}")
        try:
            jsonschema.validate(actual, spec.schema_)
            return result(True, 1.0, "matches schema")
        except jsonschema.ValidationError as exc:
            location = "/".join(str(p) for p in exc.absolute_path) or "(root)"
            return result(False, 0.0, f"schema violation at {location}: {exc.message}")

    if kind == "json_path_equals":
        actual, expected = extract_actual(spec, ctx), extract_expected(spec, ctx)
        ok = actual == expected
        return result(
            ok, 1.0 if ok else 0.0, f"{spec.path} = {_stringify(actual)!r}, expected {_stringify(expected)!r}"
        )

    if kind == "numeric_within":
        raw_actual, raw_expected = extract_actual(spec, ctx), extract_expected(spec, ctx)
        try:
            a_num, e_num = float(raw_actual), float(raw_expected)
        except (TypeError, ValueError):
            return result(False, 0.0, f"not numeric: got {raw_actual!r}, expected {raw_expected!r}")
        delta = abs(a_num - e_num)
        ok = delta <= spec.tolerance
        score = 1.0 if ok else max(0.0, 1.0 - delta / (abs(e_num) or 1.0))
        return result(ok, score, f"|{a_num} - {e_num}| = {delta:g} (tolerance {spec.tolerance:g})")

    if kind == "latency_budget_ms":
        ok = ctx.latency_ms <= spec.max_ms
        return result(
            ok,
            1.0 if ok else max(0.0, spec.max_ms / max(ctx.latency_ms, 1)),
            f"{ctx.latency_ms}ms against a {spec.max_ms}ms budget",
        )

    if kind == "cost_budget_micros":
        if ctx.cost_micros is None:
            # An unpriced model must not silently pass a budget check.
            return result(False, 0.0, "cost unknown: no price on file for this model")
        ok = ctx.cost_micros <= spec.max_micros
        return result(
            ok,
            1.0 if ok else max(0.0, spec.max_micros / max(ctx.cost_micros, 1)),
            f"{ctx.cost_micros} micros against a {spec.max_micros} budget",
        )

    if kind in ("llm_judge", "rubric"):
        if ctx.judge is None:
            return result(False, 0.0, "no judge configured for this run")
        criteria = spec.criteria if kind == "llm_judge" else _rubric_prompt(spec)
        call = JudgeCall(
            criteria=criteria,
            output_text=_stringify(extract_actual(spec, ctx)),
            expected_text=_stringify(extract_expected(spec, ctx)) if ctx.expected else None,
            input_text=_stringify(ctx.input),
            model=spec.model,
        )
        response = await ctx.judge(call)
        ok = response.verdict.score >= spec.min_score
        return result(
            ok,
            response.verdict.score,
            f"judge scored {response.verdict.score:.2f} (min {spec.min_score}): {response.verdict.reasoning}",
            cost_micros=response.cost_micros,
            latency_ms=response.latency_ms,
        )

    return result(False, 0.0, f"unknown assertion type {kind!r}")


def _rubric_prompt(spec: RubricAssertion) -> str:
    lines = ["Score the response against this weighted rubric and return a single overall score in [0, 1]."]
    total = sum(c.weight for c in spec.criteria)
    for c in spec.criteria:
        lines.append(f"- {c.name} (weight {c.weight / total:.0%}): {c.description}")
    return "\n".join(lines)


async def evaluate_case(specs: list[Any], ctx: EvalContext) -> CaseVerdict:
    """Run every assertion, even after one fails.

    Short-circuiting would be cheaper, but a triage view that says "assertion 1 failed" and hides
    the other four is useless: you want to know whether the answer was also slow, also over budget,
    and also malformed.
    """
    outcomes = [await evaluate_assertion(spec, ctx) for spec in specs]
    if not outcomes:
        return CaseVerdict(passed=True, score=1.0, outcomes=[])
    total_weight = sum(o.weight for o in outcomes)
    score = sum(o.score * o.weight for o in outcomes) / total_weight if total_weight else 0.0
    return CaseVerdict(
        passed=all(o.passed for o in outcomes),
        score=score,
        outcomes=outcomes,
        judge_cost_micros=sum(o.cost_micros for o in outcomes),
    )


def parse_assertions(raw: list[dict[str, Any]]) -> list[AssertionSpec]:
    return ASSERTION_LIST_ADAPTER.validate_python(raw)


def dump_assertions(specs: list[Any]) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json", by_alias=True, exclude_none=True) for s in specs]
