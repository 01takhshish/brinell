# Benchmark — agent architectures

> **Simulated.** Every model call below was served by Crucible's deterministic mock
> provider, at synthetic prices, so this table is free to reproduce and identical on every
> machine. It measures the *structural* cost of each architecture — calls made and tokens
> resent — not the quality of any real model. Pass rate is held constant by construction;
> it is shown so that a change in it is visible as the regression it would be.

Workload: 60 cases, two thirds of which require a tool call. Reproduce with `make bench`; every number below is byte-identical on a re-run, which is what `--check` in CI enforces.

| architecture | model | pass | calls/case | input tokens | output tokens | cost | vs baseline |
|---|---|---|---|---|---|---|---|
| single_shot | mock-small | 60/60 | 1.00 | 800 | 60 | $0.0011 | 1.00× |
| react | mock-small | 60/60 | 2.03 | 5,567 | 122 | $0.006177 | 5.62× |
| planner_executor | mock-small | 60/60 | 3.00 | 6,343 | 180 | $0.007243 | 6.58× |
| single_shot | mock-large | 60/60 | 1.00 | 800 | 60 | $0.0055 | 5.00× |
| react | mock-large | 60/60 | 2.03 | 5,567 | 122 | $0.0309 | 28.08× |
| planner_executor | mock-large | 60/60 | 3.00 | 6,343 | 180 | $0.0362 | 32.92× |

## What the numbers say

* ReAct resends the transcript on every step, so its input tokens are 7.0× single-shot's while it makes 2.03 calls per case. That ratio, not the call count, is what makes the loop expensive.
* Plan-then-execute makes 3.00 calls per case at 6.58× the cost — a fixed, predictable premium, because the number of calls does not depend on how long the model chooses to keep going.
* The larger model multiplies cost by exactly its price ratio at identical token counts, which is the sanity check that the cost accounting is doing arithmetic and not guessing.

## Caveats

* A real model's tool-calling behaviour varies with the prompt; the mock's does not. Treat the call counts as the shape of each architecture, not as a forecast for your workload.
* Latency is deliberately absent. The mock's latency is a constant this script sets, so reporting it would measure this laptop rather than the architecture. Calls per case is the honest proxy: a real provider charges wall-clock time per call, and that column is exact.
* Prices for real providers are deliberately absent from `pricing.json` until someone copies them from the vendor's page; only the mock has prices, and they are marked synthetic.
