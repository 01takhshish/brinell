# 1. The work queue is Postgres, not Celery

**Status:** accepted · **Date:** 2026-09-10

## Context

Evaluation runs fan out into per-case work. A run of 200 cases is 200 independent units, each
of which calls a model provider, takes seconds, and can fail in ways worth retrying. Several
workers should be able to drain that work in parallel, and a worker that dies mid-case must
not take the case with it.

The obvious answer is a broker — Celery on Redis, or RQ, or arq. It is the answer most Python
services reach for, and it is a reasonable one.

## Decision

Cases are rows in Postgres. Workers claim them with `SELECT … FOR UPDATE SKIP LOCKED`, take a
lease with an expiry, and release the lease when the case reaches a terminal state.

## Why

**Two datastores means two truths.** Run state — how many cases are done, what they cost,
what the pass rate is — lives in Postgres either way, because that is what the API reads. If
the queue lives in Redis, then every state transition spans two systems with no transaction
across them. Enqueue succeeds and the row insert fails, and there is a job for a case that
does not exist. The row insert succeeds and the enqueue fails, and there is a case nobody will
ever run. Every failure mode of this system is a variant of those two, and none of them exist
if claiming a case and updating it are the same transaction.

**`SKIP LOCKED` is the feature.** It was added to Postgres 9.5 for exactly this: N consumers
pulling disjoint rows off a table without blocking each other. It is not a clever workaround;
it is the supported mechanism, and it has the semantics we want without a second process to
operate.

**Leases beat acknowledgements for this shape of work.** A case is slow — seconds, sometimes
tens of seconds. A worker that is killed is common enough to design for (a redeploy does it).
With a lease, recovery is a query: any case whose lease expired is available again, and the
attempt counter on the row decides whether it is retried or marked failed. There is no
separate visibility-timeout setting to tune in a different system's config.

**Redis is still here, and this is not an argument against it.** It caches provider responses,
where losing the data costs money but not correctness. That is the right job for a store whose
durability story is "mostly".

## Consequences

**We pay for it in throughput.** Claiming a case is a write transaction, so the queue's
ceiling is Postgres's write ceiling — thousands per second, not millions. For a system whose
unit of work takes seconds and costs real money in provider spend, the queue will never be the
bottleneck. If it ever is, that is a good problem and a different ADR.

**Polling, not push.** Workers poll on an interval rather than being woken, which puts a floor
on latency between a case being queued and a worker picking it up. The interval is short and
the work is long, so this rounds to nothing.

**We own the semantics.** No broker means no broker's retry policy, dead-letter handling, or
prefetch behaviour — we implement what we need. That is more code, but it is code that lives
next to the schema it operates on, and it is readable without knowing Celery.

**Operationally this is one less thing.** A deployment is Postgres and a process. Nobody has
to reason about what happens when the broker and the database disagree, because they cannot.
