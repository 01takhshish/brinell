"""Assertions.

Two rules are load-bearing and easy to break by accident:

* a case passes only if **every** assertion passes, while the *score* is a weighted mean — so a
  case can score 0.9 and still be red, which is what makes partial credit safe to report;
* every assertion runs even after one fails, because triage needs the whole picture.
"""

import json

import pytest
from pydantic import ValidationError

from crucible.domain.assertions import (
    EvalContext,
    JudgeCall,
    JudgeResponse,
    JudgeVerdict,
    dump_assertions,
    evaluate_case,
    parse_assertions,
)


def ctx(text="hello world", **kw):
    base = {"output": {"text": text, "parsed": kw.pop("parsed", None), "tool_calls": [], "steps": []}, "input": {}}
    base.update(kw)
    return EvalContext(**base)


async def run(specs, context):
    return await evaluate_case(parse_assertions(specs), context)


# ─── individual assertion types ──────────────────────────────────────────────


async def test_equals_ignores_case_and_whitespace_by_default():
    v = await run([{"type": "equals", "value": "HELLO WORLD"}], ctx("  hello world  "))
    assert v.passed and v.score == 1.0


async def test_equals_can_be_made_strict():
    v = await run([{"type": "equals", "value": "HELLO", "ignore_case": False}], ctx("hello"))
    assert not v.passed


async def test_contains_and_not_contains_are_opposites():
    passed = await run([{"type": "contains", "value": "world"}], ctx())
    failed = await run([{"type": "not_contains", "value": "world"}], ctx())
    assert passed.passed and not failed.passed


async def test_regex_should_match_can_be_inverted():
    v = await run([{"type": "regex", "pattern": r"^\d+$", "should_match": False}], ctx("abc"))
    assert v.passed


async def test_fuzzy_gives_partial_credit_below_the_threshold():
    v = await run([{"type": "fuzzy", "value": "hello world", "min_ratio": 0.99}], ctx("hello wrld"))
    assert not v.passed
    assert 0.0 < v.score < 1.0, "a near-miss should score close to 1, not 0"


async def test_json_schema_validates_parsed_output():
    schema = {"type": "object", "required": ["refund_id"], "properties": {"refund_id": {"type": "string"}}}
    payload = {"refund_id": "r_1"}
    ok = await run([{"type": "json_schema", "schema": schema}], ctx(json.dumps(payload), parsed=payload))
    bad = await run([{"type": "json_schema", "schema": schema}], ctx("{}", parsed={}))
    assert ok.passed and not bad.passed


async def test_json_path_equals_reads_into_the_parsed_object():
    v = await run(
        [{"type": "json_path_equals", "path": "parsed.refund.id", "value": "r_9"}],
        ctx('{"refund": {"id": "r_9"}}', parsed={"refund": {"id": "r_9"}}),
    )
    assert v.passed


async def test_numeric_within_tolerance():
    near = await run([{"type": "numeric_within", "value": 10, "tolerance": 0.5}], ctx("10.4"))
    far = await run([{"type": "numeric_within", "value": 10, "tolerance": 0.5}], ctx("11.0"))
    assert near.passed and not far.passed


async def test_numeric_within_fails_cleanly_on_non_numeric_output():
    v = await run([{"type": "numeric_within", "value": 10}], ctx("not a number"))
    assert not v.passed and "numeric" in v.outcomes[0].detail


async def test_latency_budget():
    over = await run([{"type": "latency_budget_ms", "max_ms": 100}], ctx(latency_ms=250))
    under = await run([{"type": "latency_budget_ms", "max_ms": 100}], ctx(latency_ms=50))
    assert not over.passed and under.passed


# ─── the cost-unknown rule ───────────────────────────────────────────────────


async def test_cost_budget_fails_when_the_model_has_no_price():
    """`None` cost means "we have no price on file". Treating that as "within budget" would let an
    unpriced model quietly pass a spend gate, so it fails and says why."""
    v = await run([{"type": "cost_budget_micros", "max_micros": 1000}], ctx(cost_micros=None))
    assert not v.passed
    assert "no price" in v.outcomes[0].detail.lower()


async def test_cost_budget_passes_under_budget():
    v = await run([{"type": "cost_budget_micros", "max_micros": 1000}], ctx(cost_micros=999))
    assert v.passed


# ─── composition rules ───────────────────────────────────────────────────────


async def test_case_fails_if_any_assertion_fails_even_with_a_high_score():
    v = await run(
        [
            {"type": "contains", "value": "hello", "weight": 9},
            {"type": "contains", "value": "goodbye", "weight": 1},
        ],
        ctx(),
    )
    assert v.score == pytest.approx(0.9)
    assert not v.passed, "a 0.9 score with one hard failure is still a failing case"


async def test_all_assertions_run_even_after_the_first_failure():
    v = await run(
        [{"type": "contains", "value": "nope"}, {"type": "contains", "value": "world"}],
        ctx(),
    )
    assert len(v.outcomes) == 2
    assert [o.passed for o in v.outcomes] == [False, True]


async def test_weights_shift_the_score_not_the_verdict():
    heavy = await run(
        [{"type": "contains", "value": "world", "weight": 99}, {"type": "contains", "value": "nope"}], ctx()
    )
    assert heavy.score > 0.98 and not heavy.passed


async def test_empty_assertion_list_passes():
    """A suite with no assertions is a smoke test: did the agent produce output without erroring."""
    v = await run([], ctx())
    assert v.passed and v.score == 1.0


# ─── judge assertions ────────────────────────────────────────────────────────


async def test_llm_judge_uses_the_injected_judge_and_records_its_cost():
    calls: list[JudgeCall] = []

    async def judge(call: JudgeCall) -> JudgeResponse:
        calls.append(call)
        return JudgeResponse(verdict=JudgeVerdict(score=0.8, reasoning="mostly right"), cost_micros=42, latency_ms=7)

    v = await run(
        [{"type": "llm_judge", "criteria": "is it polite?", "threshold": 0.7}],
        EvalContext(output={"text": "hi", "parsed": None}, input={}, judge=judge),
    )
    assert v.passed and v.score == pytest.approx(0.8)
    assert v.judge_cost_micros == 42
    assert calls and "polite" in calls[0].criteria


async def test_judge_assertion_fails_closed_when_no_judge_is_configured():
    """Silently skipping a judge assertion would turn a graded suite into a smoke test without
    anyone noticing, so a missing judge is a failure, not a pass."""
    v = await run([{"type": "llm_judge", "criteria": "is it polite?"}], ctx())
    assert not v.passed


# ─── parsing ─────────────────────────────────────────────────────────────────


def test_unknown_assertion_type_is_rejected_at_parse_time():
    with pytest.raises(ValidationError):
        parse_assertions([{"type": "vibes", "value": "good"}])


def test_parse_and_dump_round_trip():
    """A run snapshots its assertions as JSON and later re-parses them, so the round trip has to be
    lossless or a re-read of an old run would evaluate differently than the run itself did."""
    raw = [{"type": "equals", "value": "x", "weight": 2.0}, {"type": "latency_budget_ms", "max_ms": 500}]
    dumped = dump_assertions(parse_assertions(raw))
    assert dump_assertions(parse_assertions(dumped)) == dumped
    for original, out in zip(raw, dumped, strict=True):
        assert out.items() >= original.items()


def test_json_schema_alias_round_trips():
    """`schema` shadows a pydantic attribute, so it is aliased internally; the wire format must
    still be plain `schema` or every config file breaks."""
    spec = parse_assertions([{"type": "json_schema", "schema": {"type": "object"}}])[0]
    assert dump_assertions([spec])[0]["schema"] == {"type": "object"}
