# Deploy Runbook — Measured, Not Guessed

> Every command below was executed against a real DigitalOcean account on 11 Aug 2026.
> Timings are wall-clock from that run. Nothing here is theoretical.

**Live result:** https://job-orchestrator-ihshm.ondigitalocean.app
(2 components + managed Postgres, HTTP/2 + TLS, SSE streaming, $10/mo.)

---

## Measured timings

| Step | Time | Note |
|---|---|---|
| `docker build` both images (amd64, cold) | ~90s | Cached rebuild is ~10s |
| `doctl registry create` | ~3s | |
| `docker push` both images | ~35s | api 61MB, web 21MB |
| `doctl apps propose` | ~4s | Validates + prices, spends nothing |
| **`doctl apps create --wait`** | **434s (7m14s)** | Includes provisioning the managed Postgres |
| First request served | immediately after | |

**Budget 10 minutes for the first deploy, 3–4 for a redeploy.** That is why the playbook
says deploy a skeleton at 0:40 — at 2:40 this would be the difference between shipping
and not.

---

## The exact sequence

```bash
# 0 — confirm you're on the credited account
doctl account get

# 1 — registry (once). Name must be globally unique; region blr1 for Hyderabad.
doctl registry create <unique-name> --subscription-tier basic --region blr1
doctl registry login

REG=$(doctl registry get --format Endpoint --no-header)

# 2 — build for the RIGHT ARCHITECTURE and push
#     On an Apple Silicon laptop a plain `docker build` produces arm64, which
#     App Platform cannot run. This flag is not optional.
docker build --platform linux/amd64 -t $REG/jobs-api:v1 ./backend
docker build --platform linux/amd64 -t $REG/jobs-web:v1 ./frontend
docker push $REG/jobs-api:v1
docker push $REG/jobs-web:v1

# 3 — validate before spending
doctl apps propose --spec .do/app.yaml

# 4 — ship
doctl apps create --spec .do/app.yaml --wait

APP=$(doctl apps list --format ID --no-header | head -1)
doctl apps get $APP --format DefaultIngress --no-header

# 5 — when it misbehaves
doctl apps logs $APP api --type run --follow
doctl apps logs $APP --type deploy --follow

# 6 — redeploy after a code change
docker build --platform linux/amd64 -t $REG/jobs-api:v1 ./backend && docker push $REG/jobs-api:v1
doctl apps create-deployment $APP --wait
```

---

## Gotchas actually hit during this rehearsal

**1. `blr` vs `blr1`.** App Platform regions are `blr`; registry regions are `blr1`.
Using the wrong one returns `422 invalid or unsupported region`. They are different
namespaces — do not assume one from the other.

**2. Architecture mismatch.** On Apple Silicon, `docker build` defaults to arm64 and
App Platform silently fails to run it. Always `--platform linux/amd64`.
*(Tomorrow's laptop may be Intel or Apple Silicon — pass the flag regardless; it costs
nothing when it's already correct.)*

**3. `preserve_path_prefix: true` is mandatory here.** The API's routes already start
with `/api`. Without this flag App Platform strips the prefix and the service receives
`/jobs`, so every request 404s while looking perfectly deployed. This is the kind of
failure that eats 20 minutes.

**4. Health-check paths are internal, not external.** `/healthz` is polled by the
platform against the api component directly. Externally, `/healthz` matches the `web`
component's `/` route and returns the SPA's HTML. That is *correct* — but if you curl
`/healthz` from outside expecting JSON, you'll think the API is broken when it isn't.
Test the API at `/api/...`.

**5. Concurrent replicas racing to migrate.** `CREATE TABLE IF NOT EXISTS` is **not**
atomic in Postgres. Two replicas booting simultaneously raced on an internal catalogue
index and one died with `duplicate key value violates unique constraint
"pg_type_typname_nsp_index"`. Fix: take `pg_advisory_xact_lock` **before** any DDL,
including the bookkeeping table, and hold it for the whole migration. Better still on
App Platform, run migrations as a `PRE_DEPLOY` job so exactly one process ever migrates.
*(Hit for real, in this rehearsal. Good story for the design round.)*

**6. `doctl apps create --format ActiveDeployment.Phase` is not a valid column.** Cosmetic,
but it exits non-zero and looks alarming mid-deploy. Use `doctl apps list` after.

**7. Free static-site slots.** App Platform includes 3 free static sites. This deploy
serves the SPA from an nginx *service* ($5/mo) because it came from a container image.
Deploying the frontend as a `static_site` from git would be free — worth mentioning as
a cost trade-off you noticed.

---

## Fallback: Droplet + Docker Compose

If App Platform fights you for more than ~10 minutes tomorrow, switch rather than debug.

```bash
doctl compute droplet create demo \
  --region blr1 --size s-2vcpu-4gb --image docker-20-04 \
  --ssh-keys $(doctl compute ssh-key list --format ID --no-header | head -1) \
  --user-data-file deploy/cloud-init.yaml --wait

doctl compute droplet list --format Name,PublicIPv4
```

Up in ~4 minutes, no registry and no git remote needed. Trade-off to say out loud:
you get a bare HTTP IP with no TLS, and you own patching and restarts. Choosing it
deliberately, and saying why, reads better than silently fighting a build.

---

## Teardown — do this when you're done

Costs are trivial (~$0.02 for one night) but nothing should bill silently.

```bash
doctl apps delete <app-id> -f          # also removes the attached dev database
doctl registry delete -f               # the $5/mo line item
```

Find yours with:
```bash
doctl apps list --format ID,Spec.Name
doctl registry get --format Name,Endpoint
```
