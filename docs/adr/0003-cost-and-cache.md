# 3. Integer micro-dollars, and an unpriced model is `null` rather than zero

**Status:** accepted · **Date:** 2026-09-12

## Context

Cost is a first-class result here, not telemetry. A budget is an assertion — `cost_budget_micros`
fails a case the same way a wrong answer does — and the comparison view reports spend against a
baseline. So the number has to be one people will act on, which puts two requirements on it that
are easy to get wrong.

It has to **add up**. A run is thousands of spans, each a few millionths of a dollar. It has to be
**honest about what it does not know**: providers change prices, and this repository has no way to
learn about it.

## Decision

**Money is `int` micro-dollars (1e-6 USD) from the provider response to the database column to the
API response.** Conversion to a display string happens once, at the edge, in `format_usd`.

**A model with no entry in `pricing.json` yields `None`, and the column stays null.** The UI renders
"unpriced". It does not render `$0.00`.

**`pricing.json` ships with no real vendor prices.** The mock provider's prices are there, marked
synthetic. The Anthropic and OpenAI blocks are empty, with a note saying they are filled by copying
from the vendor's own page.

## Why

**Floats drift, and the drift lands in the wrong place.** A per-span cost of `2.985e-6` summed
across 8,000 spans is a float addition chain 8,000 long. The error is tiny in absolute terms and
completely invisible until two runs that cost the same amount report different totals, and someone
spends an afternoon on a "cost regression" that is binary representation. Integers do not have this
property. Micro-dollars was chosen because a millionth of a dollar is below the granularity of any
real provider's pricing, so no rounding happens at the point of measurement — only at the point of
display.

**Zero is a claim, and it is usually false.** An unpriced model did not cost nothing; it cost an
unknown amount. Defaulting to zero makes a budget assertion pass silently, which is the single
worst behaviour available: the feature whose entire purpose is to catch runaway spend quietly stops
working the moment someone evaluates a model nobody has priced yet. `None` propagates, the
assertion cannot evaluate, and the case says so.

**Prices in a repository go stale invisibly.** A number copied from a pricing page in September is
wrong in November, and nothing in CI will ever notice. Shipping empty blocks with a note means the
first person to need real prices has to go and look at the current page, which is the only
mechanism that keeps them current. Every entry carries `as_of` and `source` for the same reason.

## The cache is keyed on everything that changes the answer

The response cache lives in Redis and is keyed on `request_fingerprint(provider, model, payload)` —
a hash over the model, messages, system prompt, tools, and sampling parameters, and deliberately
*not* over request ids, timestamps or run ids.

Both directions of getting this wrong are expensive, and asymmetrically so. Too loose and the cache
serves an answer produced by a different prompt, which is a correctness bug that looks like a model
regression. Too tight and every run pays full price, which is merely money. It is built to fail
toward the second: anything that could plausibly change the output is in the key.

The practical effect is that re-running an unchanged target costs `$0.00` and the run reports its
cache hits, so the zero is explained rather than suspicious.

## Consequences

**Every consumer formats for itself.** `RunOut.cost_usd` is a plain property, not a serialised
field, so the API returns `total_cost_micros` and the CLI and web UI each format it. They are
required to agree — a run that reads `$0.0011` in the terminal and `$0.00` in the browser is a bug
report — so `formatUsd` in the frontend is a deliberate port of `format_usd`, and it says so.

**Null costs must be handled everywhere, not just displayed.** Sums skip them, comparisons cannot
compute a delta against them, and budget assertions cannot evaluate. This is more branching than
`0` would have needed. It is the correct amount of branching.

**The benchmark's numbers are synthetic and labelled as such.** `docs/BENCHMARK.md` measures the
*structural* cost of an agent architecture — calls made, tokens resent — at mock prices. It is a
statement about ReAct versus plan-then-execute, not a forecast of anyone's invoice.
