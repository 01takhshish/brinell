#!/usr/bin/env python
"""A reproducible benchmark of the three agent architectures.

**What this measures, and what it does not.** Every model call here goes to Crucible's built-in
deterministic provider, not to a real model. That makes the benchmark free, offline, and identical
on every machine — and it means the only honest questions it can answer are structural ones:

* how many model calls does each architecture make per case,
* how many tokens does it resend,
* what does that cost, and how long does it take.

It cannot answer "which architecture is more accurate", because accuracy is a property of the model
and the mock's correctness rate is a fixed knob. Pass rate is therefore held constant on purpose and
reported only so that a change in it shows up as the bug it would be.

The numbers are real measurements of real executions through `CaseExecutor` — the same code path a
worker uses for every case — with a synthetic provider underneath. Every table this script writes
says so in its own header, because a benchmark that does not state its provider is a benchmark that
will eventually be quoted as if it used a real one.

    python benchmarks/run_benchmark.py                       # print the table
    python benchmarks/run_benchmark.py --out ../docs/BENCHMARK.md
    python benchmarks/run_benchmark.py --check ../docs/BENCHMARK.md   # CI: is it still reproducible?
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crucible.domain.assertions import parse_assertions
from crucible.domain.cost import format_usd
from crucible.providers.mock import MockBehaviour, MockProvider
from crucible.providers.registry import ProviderRegistry
from crucible.runner.executor import CaseExecutor

# Spelled out rather than inlined: ruff flags the multiplication sign as an ambiguous character
# in source, and "2.03x" in a published table reads like a typo.
TIMES = "\u00d7"

ARCHITECTURES = ("single_shot", "react", "planner_executor")
MODELS = ("mock-small", "mock-large")

TOOLS = [
    {
        "name": "search",
        "description": "Look up a fact in the knowledge base.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "calculator",
        "description": "Evaluate an arithmetic expression.",
        "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}},
    },
]


def build_cases(count: int) -> list[dict[str, Any]]:
    """A mixed workload: two thirds need a tool, one third is answerable directly.

    `[[expect:…]]` is the mock's directive for "the correct answer to this case is …", so
    correctness is a property of the dataset rather than of a random draw.
    """
    cases = []
    for index in range(count):
        answer = f"ANS-{index:03d}"
        needs_tool = index % 3 != 0
        question = (
            f"Look up record {index} and report its code. [[expect:{answer}]]"
            if needs_tool
            else f"What is the code for record {index}? [[expect:{answer}]]"
        )
        cases.append(
            {
                "external_id": f"case-{index:03d}",
                "input": {
                    "question": question,
                    "tool_fixtures": {"search": {"record": index, "code": answer}} if needs_tool else {},
                },
                "expected": {"answer": answer},
            }
        )
    return cases


@dataclass
class Result:
    architecture: str
    model: str
    cases: int
    passed: int
    calls: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latencies: list[int]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.cases if self.cases else 0.0

    @property
    def calls_per_case(self) -> float:
        return self.calls / self.cases if self.cases else 0.0

    def percentile(self, fraction: float) -> int:
        if not self.latencies:
            return 0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, int(fraction * len(ordered)))
        return ordered[index]


async def measure(
    architecture: str, model: str, cases: list[dict[str, Any]], *, latency_ms: int, concurrency: int
) -> Result:
    registry = ProviderRegistry()
    # latency_ms is per model call, so an architecture that makes three calls pays it three times.
    # That is the point: wall-clock cost compounds with call count.
    registry.register(MockProvider(MockBehaviour(latency_ms=latency_ms)))
    # No cache: a benchmark that measures cache hits measures the benchmark's own repetition.
    executor = CaseExecutor(registry)

    target_config = {
        "architecture": architecture,
        "model": model,
        "prompt_template": "{{question}}",
        "max_tokens": 256,
        "max_steps": 4,
        "tools": TOOLS,
    }
    assertions = parse_assertions([{"type": "equals", "value_from": "expected.answer"}])
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case: dict[str, Any]) -> Any:
        async with semaphore:
            return await executor.execute(
                target_config=target_config,
                case_input=case["input"],
                expected=case["expected"],
                assertions=assertions,
            )

    outcomes = await asyncio.gather(*(one(case) for case in cases))
    return Result(
        architecture=architecture,
        model=model,
        cases=len(outcomes),
        passed=sum(1 for o in outcomes if o.status == "passed"),
        calls=sum(len([s for s in o.spans if s.kind == "llm"]) for o in outcomes),
        input_tokens=sum(o.input_tokens for o in outcomes),
        output_tokens=sum(o.output_tokens for o in outcomes),
        cost_micros=sum(o.cost_micros for o in outcomes),
        latencies=[o.latency_ms for o in outcomes],
    )


def render(results: list[Result], *, cases: int, latency_ms: int) -> str:
    baseline = next(r for r in results if r.architecture == "single_shot" and r.model == MODELS[0])
    lines = [
        "# Benchmark — agent architectures",
        "",
        "> **Simulated.** Every model call below was served by Crucible's deterministic mock",
        "> provider, at synthetic prices, so this table is free to reproduce and identical on every",
        "> machine. It measures the *structural* cost of each architecture — calls made and tokens",
        "> resent — not the quality of any real model. Pass rate is held constant by construction;",
        "> it is shown so that a change in it is visible as the regression it would be.",
        "",
        f"Workload: {cases} cases, two thirds of which require a tool call. "
        f"Reproduce with `make bench`; every number below is byte-identical on a re-run, which is "
        f"what `--check` in CI enforces.",
        "",
        "| architecture | model | pass | calls/case | input tokens | output tokens | cost | vs baseline |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        multiple = result.cost_micros / baseline.cost_micros if baseline.cost_micros else 0.0
        lines.append(
            f"| {result.architecture} | {result.model} | {result.passed}/{result.cases} "
            f"| {result.calls_per_case:.2f} | {result.input_tokens:,} | {result.output_tokens:,} "
            f"| {format_usd(result.cost_micros)} | {multiple:.2f}{TIMES} |"
        )

    react = next(r for r in results if r.architecture == "react" and r.model == MODELS[0])
    planner = next(r for r in results if r.architecture == "planner_executor" and r.model == MODELS[0])
    lines += [
        "",
        "## What the numbers say",
        "",
        f"* ReAct resends the transcript on every step, so its input tokens are "
        f"{react.input_tokens / baseline.input_tokens:.1f}{TIMES} single-shot's while it makes "
        f"{react.calls_per_case:.2f} calls per case. That ratio, not the call count, is what makes "
        "the loop expensive.",
        f"* Plan-then-execute makes {planner.calls_per_case:.2f} calls per case at "
        f"{planner.cost_micros / baseline.cost_micros:.2f}{TIMES} the cost — a fixed, predictable premium, "
        "because the number of calls does not depend on how long the model chooses to keep going.",
        "* The larger model multiplies cost by exactly its price ratio at identical token counts, "
        "which is the sanity check that the cost accounting is doing arithmetic and not guessing.",
        "",
        "## Caveats",
        "",
        "* A real model's tool-calling behaviour varies with the prompt; the mock's does not. Treat "
        "the call counts as the shape of each architecture, not as a forecast for your workload.",
        "* Latency is deliberately absent. The mock's latency is a constant this script sets, so "
        "reporting it would measure this laptop rather than the architecture. Calls per case is the "
        "honest proxy: a real provider charges wall-clock time per call, and that column is exact.",
        "* Prices for real providers are deliberately absent from `pricing.json` until someone copies "
        "them from the vendor's page; only the mock has prices, and they are marked synthetic.",
    ]
    return "\n".join(lines) + "\n"


TABLE_ROW = re.compile(r"^\| (single_shot|react|planner_executor) \|")


def numeric_rows(markdown: str) -> list[str]:
    return [line.strip() for line in markdown.splitlines() if TABLE_ROW.match(line)]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=int, default=60)
    parser.add_argument("--latency-ms", type=int, default=5, help="Simulated provider latency per model call")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--out", type=Path, help="Write the report here instead of printing it")
    parser.add_argument(
        "--check",
        type=Path,
        nargs="?",
        const=Path("../docs/BENCHMARK.md"),
        help="Re-measure and fail if the committed report's numbers have changed",
    )
    parser.add_argument("--json", action="store_true", help="Emit raw measurements as JSON")
    args = parser.parse_args()

    cases = build_cases(args.cases)
    results = [
        await measure(architecture, model, cases, latency_ms=args.latency_ms, concurrency=args.concurrency)
        for model in MODELS
        for architecture in ARCHITECTURES
    ]

    if args.json:
        print(
            json.dumps(
                [
                    {
                        **{k: v for k, v in r.__dict__.items() if k != "latencies"},
                        "pass_rate": r.pass_rate,
                        "p50_ms": r.percentile(0.5),
                        "p95_ms": r.percentile(0.95),
                        "mean_ms": round(statistics.fmean(r.latencies), 1),
                    }
                    for r in results
                ],
                indent=2,
            )
        )
        return 0

    report = render(results, cases=args.cases, latency_ms=args.latency_ms)

    if args.check:
        if not args.check.is_file():
            print(f"error: {args.check} does not exist; generate it with --out first", file=sys.stderr)
            return 1
        committed, fresh = numeric_rows(args.check.read_text()), numeric_rows(report)
        if committed != fresh:
            print(f"{args.check} is out of date. Re-measured:", file=sys.stderr)
            for expected, actual in zip(committed, fresh, strict=False):
                if expected != actual:
                    print(f"  committed: {expected}\n  measured : {actual}", file=sys.stderr)
            return 1
        print(f"{args.check} still reproduces exactly ({len(fresh)} rows).")
        return 0

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
