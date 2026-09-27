# 2. Exactly-once counting via conditional updates, not exactly-once delivery

**Status:** accepted · **Date:** 2026-09-11

## Context

Two things in this system retry on their own, and neither can be talked out of it.

**CI retries jobs.** A GitHub Actions run that fails on a flaky network step gets re-run by a
human clicking a button. If that job's `crucible run` starts a second run, the project pays
twice for the same evaluation and the comparison against baseline is computed against the
wrong thing.

**Workers come back from the dead.** A worker claims a case, starts a 20-second model call, and
its pod is evicted. The lease expires, a second worker claims the same case and completes it.
Then the first worker's process — which was only paused, not killed — finishes its call and
tries to write a result for a case that already has one.

Both are the same problem wearing different clothes: work can be attempted more than once, and
the system must still arrive at one answer and one bill.

## Decision

Do not attempt exactly-once delivery. Make the *effects* idempotent instead, with two
mechanisms.

**At the run level: a partial unique index.**

```sql
CREATE UNIQUE INDEX uq_runs_idempotency ON runs (project_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

The CLI derives the key from the things that identify the attempt — project, target version,
suite, dataset version, git sha. A retried CI job computes the same key, the insert violates
the index, and the handler returns the existing run instead of creating a second one. The
`WHERE` clause is what makes this workable: runs created from the UI have no key, and a plain
unique index would let exactly one of them exist.

**At the case level: conditional updates.**

Every write that completes a case carries `WHERE status = 'claimed'`, and every run counter is
incremented in the same statement. A worker whose lease was reassigned finds zero rows matched
and discards its own result. The counter moves exactly once because only the update that
actually performed the transition gets to move it.

Leases make the reassignment safe to do at all: a case is claimable when
`lease_expires_at IS NULL OR lease_expires_at < now()`, so recovery needs no reaper process
and no liveness protocol — just a clock and a query.

## Why not the alternatives

**A worker heartbeat with a supervisor** turns a query into a distributed-systems problem: the
supervisor can be partitioned from the worker it is judging, and now two components disagree
about who is alive. The lease has the same outcome with no second opinion involved — and it
*is* renewed by a long-running case, so a slow case is not mistaken for a dead worker.

**Advisory locks** are held by a session. A worker that dies holds nothing, which is the good
part, but the lock is invisible to a `SELECT` — so "which cases are stuck" stops being
answerable in SQL. The lease is a column. Anyone can query it.

**Checking before inserting** (`SELECT` then `INSERT`) is the bug this index exists to prevent.
Two concurrent CI jobs both find nothing and both insert. The uniqueness has to be enforced by
the database or it is not enforced.

## Consequences

**A concurrent duplicate gets a 409, not the winner's row.** Two creates racing on the same key
means one wins and the other is told a run with this key is already being created. That is a
retriable answer rather than a correct one, and it is the only case where the caller has to do
something. Sequential retries — the overwhelmingly common shape — read the existing run.

**Idempotency keys are the CLI's job to get right.** A key that includes too little makes two
genuinely different runs collide; too much and a retry starts a second run. The key includes
the git sha, which means re-running CI on the *same commit* joins the existing run and
re-running after a push does not.

**A case can be executed more than once and charged for it.** A provider call made by a worker
that then loses its lease still cost money; only the *recorded* result is deduplicated. Making
the spend exactly-once would require a two-phase commit with the provider, which is not on
offer. The lease is sized well above the provider timeout so this stays rare.

**`WHERE status = 'claimed'` is load-bearing and easy to drop.** A future statement that
updates a case without it would reintroduce double-counting silently — the number would just
be wrong. `tests/integration/test_queue_idempotency.py` exists to catch that.
