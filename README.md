# sample-digitalocean-deployments

Interview-prep material and a working reference application for a DigitalOcean
full-stack build exercise: **build a REST orchestration service with a dashboard,
and deploy it to DigitalOcean.**

Everything here was executed against a live DigitalOcean account, not written from
memory. Timings, failure modes and gotchas are measured.

---

## `scaffold/` — a working reference app

FastAPI + Postgres + React, deployed to App Platform in **7 min 14 s**.

```bash
cd scaffold
docker compose up --build          # http://localhost:5173
docker compose run --rm -v "$(pwd)/backend:/srv" api python -m pytest -q
```

It models an async job orchestrator — the intersection of the three problem types
these exercises tend to use: *asynchronous processing, external API integration,
concurrent state management.*

**What it demonstrates**

| Concern | Approach |
|---|---|
| Concurrent claim, no broker | `SELECT … FOR UPDATE SKIP LOCKED` in one statement |
| Dead worker recovery | Lease + heartbeat + reaper (verified: reclaimed in 12 s) |
| Live updates | SSE over an append-only event log, Postgres `LISTEN/NOTIFY`, `Last-Event-ID` resume |
| Read performance | Normalised 3NF core + a materialised view with a UNIQUE index for `REFRESH … CONCURRENTLY` |
| Safe retries | Unique `idempotency_key`; replay returns the original job |
| Statelessness | All state in Postgres; replicas need no affinity |
| Separation of concerns | `domain/` is pure and I/O-free, so the rules are testable with zero fixtures |

Verified end to end: two worker replicas processed 33 attempts each with **zero**
double-processing; 17 tests pass in 0.11 s.

## `prep/` — the written material

| File | Contents |
|---|---|
| `01-rctfc-spec.md` | Turning an ambiguous prompt into a written spec in ten minutes |
| `02-do-primitives.md` | Every DigitalOcean primitive, when to reach for it, and the trade-off to voice |
| `03-playbook.md` | Three-hour timebox, rehearsed design answers, deploy runbook |
| `04-deploy-runbook.md` | Measured deploy timings and the gotchas actually hit |

---

## Gotchas worth the repo on their own

**`CREATE TABLE IF NOT EXISTS` is not atomic in Postgres.** Two replicas booting
together raced on an internal catalogue index and one died with a duplicate-key
error. Take `pg_advisory_xact_lock` *before* any DDL — including the bookkeeping
table — and hold it for the whole migration.

**Rewriting a response body in middleware silently kills SSE.** Collecting the body
with `b"".join([chunk async for chunk in response.body_iterator])` waits for the
iterator to exhaust, which for an event stream means never. Measured: first frame
arrived at 5.0 s instead of 0.0 s. Guard on `content-type`.

**Blocking the event loop freezes every concurrent request.** `time.sleep(3)` inside
an `async def` made an unrelated endpoint 88× slower — 2.63 s versus 0.03 s. Nothing
errors; the service just gets mysteriously slow under load.

**App Platform regions are `blr`; container registry regions are `blr1`.** Different
namespaces. The wrong one returns a bare `422`.

**On Apple Silicon, always `--platform linux/amd64`.** A plain `docker build` produces
arm64, which App Platform cannot run, and the failure doesn't mention architecture.

**`preserve_path_prefix: true`** — if your routes already start with `/api`, App
Platform strips the prefix without it and every request 404s while looking healthy.

## Deploying

```bash
doctl registry create <globally-unique> --subscription-tier basic --region blr1
doctl registry login
REG=$(doctl registry get --format Endpoint --no-header)

docker build --platform linux/amd64 -t $REG/jobs-api:v1 ./scaffold/backend
docker push $REG/jobs-api:v1        # DOCR creates the repository implicitly

doctl apps propose --spec scaffold/.do/app.yaml   # validate + price, spends nothing
doctl apps create  --spec scaffold/.do/app.yaml --wait
```

Set `deploy_on_push: {enabled: true}` under each `image:` block and `docker push`
becomes the deploy — measured to fire ~10 s after a push.

**Teardown**, so nothing bills quietly:
```bash
doctl apps delete <app-id> -f      # also removes the attached dev database
doctl registry delete -f
```

## Licence

MIT. Use it, fork it, no attribution needed.
