# Job Orchestrator

An async job orchestration service with a live operations console.
FastAPI + Postgres + React, deployable to DigitalOcean App Platform.

**This is a rehearsal scaffold** for a 3-hour build exercise: the shape it demonstrates
(async processing, external API integration, concurrent state management) is meant to be
adapted, not shipped as-is.

---

## Run it

```bash
docker compose up --build          # http://localhost:5173
docker compose run --rm -v "$(pwd)/backend:/srv" api python -m pytest -q
```

`docker compose up` starts Postgres, the API (with an in-process worker), a **second
worker-only replica**, and nginx serving the built SPA.

## Demo script

1. Submit a job from the form — the row appears **without a refresh** (SSE).
2. Watch attempts climb as the simulated dependency fails ~30% of the time; retries use
   exponential backoff with jitter.
3. Add `"force_permanent_failure": true` to the payload — it dead-letters immediately
   rather than burning retries on a request that can never succeed.
4. Click a row for the per-attempt timeline (worker id, duration, error).
5. `docker compose up -d --scale worker=3` — no job is ever processed twice.

---

## Design decisions

### Concurrency: `FOR UPDATE SKIP LOCKED`
Workers claim jobs with a single atomic statement ([`repositories/jobs.py`](backend/app/repositories/jobs.py)):

```sql
WITH claimed AS (
    SELECT id FROM jobs WHERE status='queued' AND run_after <= now()
    ORDER BY priority DESC, created_at
    FOR UPDATE SKIP LOCKED LIMIT $1
)
UPDATE jobs j SET status='running', locked_by=$2, ... FROM claimed c WHERE j.id=c.id
RETURNING ...
```

`SKIP LOCKED` makes concurrent workers step *over* locked rows rather than block, so N
workers get N disjoint batches with no coordinator and no broker. Because it is one
statement, there is no claim-then-crash window.

**Why not Redis/RabbitMQ:** Postgres was already a required dependency. A broker means
another thing to deploy, monitor and reason about during failures. This holds to roughly
thousands of jobs/minute; past that, polling load becomes the problem and Valkey → Kafka
is the path.

### Failure handling: lease + heartbeat + reaper
A claim sets `lease_expires_at`. Workers heartbeat to extend it. A reaper returns
expired-lease jobs to the queue, so a hard-killed worker's job is reclaimed rather than
stranded in `running` forever. *(Verified: an orphaned job was reclaimed and completed
in 12s.)*

This is honestly **at-least-once, not exactly-once** — a worker can complete a side
effect and die before committing. Exactly-once across an external API isn't achievable,
so effects are made idempotent instead: a unique `idempotency_key` on submission, and
per-attempt records so a retry is detectable.

### Data model: normalised core + denormalised read model
`job_types · jobs · job_attempts · job_events · workers` in 3NF — the write path, with
correctness enforced by constraints.

`mv_job_stats` is a materialised view for dashboard aggregates, with a UNIQUE index so
it refreshes `CONCURRENTLY` without blocking readers. The cost is bounded staleness, so
`/api/stats` returns `as_of` and the UI displays it. **Staleness you can see is a
trade-off; staleness you can't see is a bug.**

### Live updates: SSE over the event log
`job_events` is append-only with a monotonic `seq`. A Postgres trigger `pg_notify`s on
insert; every API replica holds a `LISTEN`, so an update from any replica reaches every
client with no sticky sessions. Clients resume via `Last-Event-ID`, and the server
re-reads from the log by sequence rather than trusting the notification payload — so a
dropped NOTIFY cannot create a gap.

**Why SSE over WebSockets:** the flow is one-directional; SSE gives auto-reconnect and
resume over plain HTTP. WebSockets would add a protocol and reconnect logic for a
bidirectional channel this app doesn't need.

### Statelessness
API and workers hold nothing in memory. Replicas scale horizontally with no affinity and
restart freely. Setting `WORKER_ENABLED=false` and adding a `workers:` component splits
them into independently-scaled deployables with no code change.

---

## Layout

```
backend/app/
  api/           routers, schemas, one error envelope
  domain/        status machine + retry policy — pure, no I/O, no fixtures needed
  repositories/  the only layer that speaks SQL
  services/      SSE broker, executors (the external-dependency seam)
  workers/       claim loop, heartbeat, reaper, matview refresh
  core/          settings, structured logging, pool + migrations
frontend/src/
  api/           typed client; no fetch inside components
  components/    States.tsx holds loading/empty/error as shared components
  hooks/         useEventStream — native EventSource, resume-aware
```

## Known gaps — deliberate, not overlooked

| Gap | Why | Where it would hook in |
|---|---|---|
| No authn/authz | Single trusted operator; expensive and low-signal here | Middleware at the router layer |
| No multi-tenancy | Not required | `tenant_id` + row-level security |
| Migrations run at startup | Simplicity; advisory-locked so replicas can't race | App Platform `PRE_DEPLOY` job |
| Full matview refresh | Fine at this volume | Incremental rollups on write |
| Logs, no metrics export | Structured logs carry the same data | `/metrics` + OpenTelemetry spans |
| No frontend tests | Time; the domain logic that matters is tested server-side | Vitest + Testing Library |

## Configuration

All via environment (`app/core/config.py`). Nothing secret is committed.
Notable: `WORKER_ENABLED`, `WORKER_CONCURRENCY`, `LEASE_SECONDS`, `MAX_ATTEMPTS`,
`BACKOFF_BASE_S`, `DEPENDENCY_FAILURE_RATE` (set `0` for a deterministic demo),
`MATVIEW_REFRESH_INTERVAL_S`.

## Deploy

See [`../prep/04-deploy-runbook.md`](../prep/04-deploy-runbook.md) — measured timings and
the gotchas hit for real.
