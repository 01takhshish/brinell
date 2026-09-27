# Runbook

How to deploy Crucible, what it needs, and what to do when something breaks. The audience is the
person on the hook at 2am, so this is organised by symptom where possible.

## Topology

Four processes, two stores:

```
browser ──► frontend (Next.js, :3200) ──► API (FastAPI, :8000) ──► Postgres (+pgvector)
                                              │                        ▲
                                              └──► Redis ◄── worker ───┘  (1..N replicas)
```

- The browser never talks to the API. The frontend is a BFF: it forwards the `crucible_session`
  cookie server-to-server, so the API can stay on a private network.
- Workers are independent processes claiming cases from Postgres (`FOR UPDATE SKIP LOCKED`).
  Scale them by adding replicas; no coordinator exists and none is needed.
- Redis holds the response cache, the provider rate-limit buckets, and the auth throttle counters.
  All three **fail open**: Crucible degrades without Redis, it does not stop.

## Requirements

| Component | Requirement | Why |
|---|---|---|
| Postgres  | 16+, with the `vector` and `pgcrypto` extensions available | `case_embeddings.embedding` is a real `vector` column; ids default to `gen_random_uuid()` |
| Redis     | 7+ | the throttle uses `EXPIRE` semantics tested against 7 |
| API/worker | the `backend/Dockerfile` image | same image, different command |
| Frontend  | the `frontend/Dockerfile` image | needs `CRUCIBLE_API_URL` pointing at the API |

**Managed Postgres note:** the API uses asyncpg, which prepares statements. Pooled endpoints in
*transaction* mode (PgBouncer, Neon's pooled URL, Supabase's port 6543) break prepared statements
intermittently. Use a session-mode/direct endpoint, or set `"statement_cache_size": 0` in the
connect args if you must pool in transaction mode.

## Deploying

Order matters:

1. **Migrate** — one-off task, same image as the API:
   `docker run --rm -e DATABASE_URL=... crucible-backend alembic upgrade head`
   Never run migrations from container start-up: two replicas racing `alembic upgrade` on one
   database is a coin flip, and a failed migration should fail the deploy, not crash-loop it.
2. **API** — the image's default command. Health: `GET /healthz` (liveness, no dependencies),
   `GET /readyz` (checks the database; wire this one to the load balancer).
3. **Worker** — same image, command `crucible worker`. No ports. Scale horizontally at will.
4. **Frontend** — `CRUCIBLE_API_URL` set to the API's internal address.

### Environment variables that matter in production

| Variable | Set it to | What happens if you don't |
|---|---|---|
| `ENV` | `prod` | the boot-time guard below never runs |
| `JWT_SECRET` | 32+ random chars (`openssl rand -hex 32`) | **the process refuses to start** — the default is committed to this repo, so every session would be forgeable |
| `COOKIE_SECURE` | `true` when `APP_URL` is https | **the process refuses to start** with an https `APP_URL` |
| `APP_URL` | the dashboard's public origin | CORS rejects the browser |
| `DATABASE_URL` / `REDIS_URL` | your stores | — |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | real keys | only the deterministic `mock` provider works |

The refusals are deliberate: a warning at boot scrolls past in a deploy log; a process that will
not start gets fixed.

### The proxy-headers contract

The API image starts uvicorn with `--proxy-headers --forwarded-allow-ips '*'`. That means: **the
API container must not be directly reachable from the internet.** It trusts `X-Forwarded-For`
from anything that can reach it, because in the intended topology only your load balancer can.
If you expose the API port publicly, an attacker can spoof their address and reduce the login
throttle's per-IP dimension to a shared bucket — change `*` to the proxy's address if your
platform gives you one.

## Symptoms

**The API container exits immediately at deploy.**
Read the first traceback line: if it says `refusing to start with env=prod`, a secret or cookie
setting is still a development default — the message names the variable. This is the guard doing
its job; set the variable, don't work around the guard.

**Logins fail with 429 for a real user.**
The auth throttle: 10 attempts per email per 5 minutes, 30 per IP. If one user is locked out,
they (or someone at their address) burned the budget; it resets within 5 minutes — there is no
manual unlock to run, though `DEL` on `crucible:throttle:*` keys in Redis clears everything at
once. If *everyone* is 429ing, your proxy is not forwarding client addresses and all traffic
shares one IP bucket — see the proxy-headers contract above.

**`throttle_unavailable` warnings in API logs.**
Redis is unreachable; logins are being allowed *unthrottled* (fail-open). Treat as an incident of
its own: restore Redis, then check for a brute-force burst in the same window
(`grep request_rejected` for repeated 401s per email).

**Runs sit in `queued` forever.**
No worker is claiming cases. Check worker replicas exist and their logs show `worker_serving`;
then check the worker can reach Postgres (it needs the same `DATABASE_URL` as the API). Cases
held by a crashed worker are reclaimed automatically when their lease expires
(`CASE_LEASE_SECONDS`, default 120).

**Every provider call errors with `PROVIDER_ERROR`.**
Check the provider key is set in the *worker's* environment (the API never calls providers).
Cached responses still serve during a provider outage — a re-run of an already-evaluated suite
can succeed while the provider is down; that is the cache working, not a miracle.

**A 500 with a request id.**
`grep <request_id>` in the API logs; the traceback is logged at the moment the envelope was
returned. The caller's copy of the id is in the `X-Request-ID` response header.

**Costs look wrong / everything costs $0.00.**
`$0.00` with `cache_hit=true` is correct (avoided spend is reported separately). `null` cost
means the model has no entry in `pricing.json` — deliberately unpriced rather than silently
zero-priced; add the model's real prices to fix it.

## Backups

State worth backing up lives in Postgres only. Redis is a cache and two kinds of counters — all
reconstructible or expirable; do not bother backing it up. `pg_dump` and the pgvector extension
coexist fine; restore requires `CREATE EXTENSION vector` to have run first (the initial migration
does this).
