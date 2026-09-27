# Crucible

**Evaluation and observability for LLM agents.** Define a suite once, run it on every pull
request, and get a verdict per case instead of a vibe per release.

Crucible answers the question that blocks most teams from shipping agent changes with any
confidence: *did this prompt edit make things better, worse, or just different?* It runs your
agent against a versioned dataset, grades every case against explicit assertions, prices the
run in real dollars, and diffs it against the suite's last run — then fails CI if something
regressed.

```
$ crucible run --suite default --fail-on-regression

  run 14da5657   support-react-offline   suite default

  ✗ 0/8 passed        pass rate    0.0%   (baseline 100.0%)
    cost  $0.009965   vs baseline  +234%
    p95   6 ms        cache hits   0/8

  8 broken · 0 fixed · 0 flaky

  broken   multi-item        expected '3', got 'mock response to: how many…'
  broken   refund-window     expected '30 days', got 'mock response to: what…'
  …

$ echo $?
1
```

---

## Why it exists

Three things go wrong when a team evaluates agents with a script and a spreadsheet:

1. **Nothing is versioned.** The prompt changed, the dataset changed, and the numbers
   changed, but nobody can say which caused which. Crucible content-addresses targets and
   datasets, so a run names the exact prompt and the exact cases it graded — and re-pushing
   an unchanged config creates nothing.
2. **A pass rate is not a diff.** "94%" hides the two cases that used to pass and now don't.
   Crucible compares case-by-case against a baseline and classifies each one — `broken`,
   `fixed`, `regressed`, `improved`, `flaky` — so a regression is a named case, not a
   moved decimal point.
3. **Cost is invisible until the invoice.** An agent that got 3% better and 10× more
   expensive is usually a bad trade. Crucible tracks tokens and cost per case, and a budget
   is an assertion like any other: exceed it and the case fails.

## How it works

```
  crucible push ──▶  API  ──▶  Postgres  ──▶  worker ──▶ provider
   (content-addressed)     (FOR UPDATE SKIP LOCKED)    (Anthropic / OpenAI / mock)
                              │
                              └──▶ assertions ──▶ comparison ──▶ failure clusters
```

* **The queue is Postgres**, claimed with `FOR UPDATE SKIP LOCKED` under a lease. A worker
  that dies mid-case does not lose the case; its lease expires and another worker picks it
  up. There is no second datastore to keep consistent with the first, which is the whole
  reason for the choice. *(→ [ADR 0001](docs/adr/0001-postgres-queue.md))*
* **Runs are idempotent.** A partial unique index on the idempotency key means a retried CI
  job joins the existing run instead of starting a second one and doubling the bill.
  *(→ [ADR 0002](docs/adr/0002-idempotency-and-leases.md))*
* **Assertions are snapshotted onto the run.** Editing a suite does not retroactively change
  what last month's run was graded against.
* **The cache is keyed on the full call.** Model, prompt, tools, temperature — change any of
  them and it misses. A re-run of an unchanged target costs `$0.00`.
* **Money is integer micro-dollars** end to end. Floats that represent currency eventually
  disagree with each other; these don't. An unpriced model reports `null`, never `$0.00`.
  *(→ [ADR 0003](docs/adr/0003-cost-and-cache.md))*

## Quickstart

Requires Docker, Python 3.13+ with [uv](https://docs.astral.sh/uv/), and Node 20+.

```bash
make up          # Postgres on :5434, Redis on :6380
make install     # backend venv
make migrate     # schema
make seed        # owner account, project, and an API key it prints once

make api         # http://127.0.0.1:8000  (docs at /docs)
make worker      # in a second shell
make web         # http://localhost:3200  (in a third)
```

Then push the worked example and run it — no API key, no network, no spend, because the
`-offline` target is wired to a deterministic mock provider:

```bash
export CRUCIBLE_API_KEY=crk_…          # printed by `make seed`
cd examples/support-agent
crucible push
crucible run --suite default --target support-react-offline --wait
```

The ports are deliberately not the defaults — `5434`, `6380`, `3200` — so this stack can
run alongside whatever else you already have bound to `5432` and `3000`.

## Defining a suite

A suite lives next to the prompt it grades, so a pull request that changes the agent also
changes the thing that measures it. From
[`examples/support-agent/crucible.yaml`](examples/support-agent/crucible.yaml):

```yaml
suites:
  - name: default
    dataset: golden
    pass_threshold: 1.0
    assertions:
      - type: equals
        value_from: expected.answer
        ignore_case: true
      # A suite that only grades correctness will happily accept an agent that became ten
      # times more expensive overnight. Budgets fail loudly, for the same reason latency does.
      - type: cost_budget_micros
        max_micros: 5000
      - type: latency_budget_ms
        max_ms: 30000
```

Available assertions: `equals`, `contains`, `not_contains`, `fuzzy`, `numeric_within`,
`json_path_equals`, `json_schema`, `cost_budget_micros`, `latency_budget_ms`, `llm_judge`.

## In CI

The composite action mints nothing and leaks nothing — inputs reach the shell through
`env:` rather than `${{ }}`, so a branch name cannot inject a command:

```yaml
- uses: ./.github/actions/crucible-eval
  with:
    api-url: ${{ vars.CRUCIBLE_API_URL }}
    api-key: ${{ secrets.CRUCIBLE_API_KEY }}
    suite: default
    target: support-react-offline
    fail-on-regression: true
```

The run exits non-zero on a regression, so it gates a merge like any other check.

## The web UI

A Next.js app at `:3200`. The browser never calls the Crucible API directly: the session is
an HttpOnly cookie, every read happens in a server component that forwards it
server-to-server, and CORS stays closed to exactly one origin. What the browser gets is HTML.

Runs list → run detail → case detail with a trace waterfall, a baseline comparison, and
failure clusters that group the cases that broke for the same reason — computed locally, with
no embedding API and no model call. *(→ [ADR 0004](docs/adr/0004-failure-clustering.md))*

## Cost and architecture

`make bench` runs a deterministic, offline benchmark of three agent architectures. Every
number is byte-identical on re-run, which is what CI's `--check` enforces:

| architecture | calls/case | input tokens | cost | vs baseline |
|---|---|---|---|---|
| single_shot | 1.00 | 800 | $0.0011 | 1.00× |
| react | 2.03 | 5,567 | $0.006177 | 5.62× |
| planner_executor | 3.00 | 6,343 | $0.007243 | 6.58× |

ReAct resends the transcript on every step, so its input tokens are 7× single-shot's — and
*that*, not the call count, is what makes the loop expensive. Full table and caveats in
[docs/BENCHMARK.md](docs/BENCHMARK.md). These are simulated prices from the mock provider;
`pricing.json` carries no real vendor prices until someone copies them from the vendor's
own page.

## Layout

```
backend/src/crucible/
  api/        FastAPI app, routers, auth        cli/      the `crucible` command
  runner/     worker, execution, cache          domain/   assertions, cost, hashing
  providers/  Anthropic, OpenAI, mock           analysis/ comparison, clustering
frontend/src/ Next.js app (App Router, server components)
examples/     a worked order-support agent
docs/adr/     why the load-bearing decisions went the way they did
```

The four decisions worth arguing with:

1. [The work queue is Postgres, not Celery](docs/adr/0001-postgres-queue.md)
2. [Exactly-once counting via conditional updates, not exactly-once delivery](docs/adr/0002-idempotency-and-leases.md)
3. [Integer micro-dollars, and an unpriced model is `null` rather than zero](docs/adr/0003-cost-and-cache.md)
4. [Deterministic hashing embedder and agglomerative linkage for failure clustering](docs/adr/0004-failure-clustering.md)

## Development

```bash
make test        # 88 unit tests, no containers needed
make test-all    # + integration tests (requires `make up`)
make lint        # ruff + mypy
make bench       # the table above
```

Integration tests run against a real Postgres because the parts worth testing — lease
expiry, `SKIP LOCKED` contention, the idempotency index — are exactly the parts a fake
database would get wrong.

## Deploying

Both halves ship as containers: `backend/Dockerfile` is one image serving as both the API (default
command) and the worker (`crucible worker`); `frontend/Dockerfile` is the dashboard. Migrations run
as a one-off task from the backend image, never at container start. [RUNBOOK.md](RUNBOOK.md) has
the full topology, the environment variables production refuses to start without, and a
symptom-indexed troubleshooting section.

## Status

Working end to end: push, queue, execute, assert, compare, cluster, and a UI over all of it.
Provider prices in `pricing.json` are real, dated, and sourced from the providers' pricing pages.
Not yet: a hosted instance.

## License

[MIT](LICENSE).
