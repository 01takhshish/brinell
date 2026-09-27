# Example — order-support agent

A small, complete Crucible setup: an agent, two datasets, two suites, and the config that binds
them. It is also what this repository's own CI evaluates on every pull request, so it is kept
working rather than kept as a snippet.

```
crucible.yaml            the registry: targets, datasets, suites
evals/golden.jsonl       8 everyday support questions
evals/adversarial.jsonl  4 requests the agent is supposed to refuse
```

## Run it offline, with no API key

```bash
crucible seed --slug support-agent        # mints a local project and key
export CRUCIBLE_API_KEY=crk_...
crucible push
crucible run --suite default  --target support-react-offline
crucible run --suite refusals --target support-react-offline
```

`support-react-offline` is the same ReAct architecture as the real target, pointed at Crucible's
deterministic provider. Its prompt template ends in `[[expect:{{demo_answer}}]]`, which is how the
simulator is told what a correct answer would be. That line is the only difference between the two
targets, and it is written in the config rather than hidden behind a flag so that nobody mistakes a
simulated pass rate for a measured one.

## Run it against a real model

```bash
export ANTHROPIC_API_KEY=sk-ant-...
crucible run --suite default --target support-react
```

Now the pass rate means something, and the cost line is real money.

## What the suites are for

`default` gates correctness *and* budget: a case that answers correctly but costs five times what it
used to still fails, because `cost_budget_micros` is an assertion like any other. `refusals` grades
the adversarial set on refusing rather than on wording — a regression there is worth more attention
than a regression in the golden set, which is why it is a separate suite with its own history.

Tool calls never reach a real system. Each case carries `tool_fixtures` describing what its tools
return, so a run is reproducible and can never mutate a real order.
