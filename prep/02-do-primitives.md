# DigitalOcean Primitives — Interview Reference

> Grounded against **your actual account's API surface** (`doctl` v1.166, verified — not recalled).
> Verified against a live account with a droplet limit of 3 and no pre-existing resources.

**How to use this doc:** Part 1 is the decision table — read it last thing before the design
rounds. Part 2 is the primitive-by-primitive reference. Part 3 is the deploy commands you'll
actually type tomorrow.

---

# Part 1 — The decision table

This is the *form* a good design answer takes: constraint → primitive → because → trade-off.
Don't recite features. Answer with this shape.

| If they say… | Reach for | Because | Trade-off to voice |
|---|---|---|---|
| "Ship this fast, minimal ops" | **App Platform** | Git/registry-driven, managed TLS + CDN, zero server admin | Less control; opinionated build; per-component cost adds up |
| "We need full control / custom daemon" | **Droplet** + Docker | Root access, any runtime | You own patching, TLS, monitoring, backups |
| "It must scale with traffic" | App Platform autoscaling, or **Droplet autoscale pool + Load Balancer** | Horizontal scale on stateless services | Requires statelessness; DB becomes the bottleneck next |
| "We're a platform team running many services" | **DOKS** (Kubernetes) | Standard orchestration, ecosystem | Real operational overhead; overkill under ~5 services |
| "Event-driven, spiky, near-zero baseline" | **Functions** | Scale-to-zero, per-invocation billing | Cold starts; time limits; awkward for long jobs |
| "Relational data, don't manage a DB" | **Managed Postgres** (`pg`/`advanced_pg`) | Backups, failover, PITR, patching handled | Cost; less tuning control |
| "Cache / session store / pub-sub fan-out" | **Managed Valkey** | Redis-compatible, managed | In-memory = durability trade-off; another moving part |
| "Durable event log, replay, multi-consumer" | **Managed Kafka** | Retention + consumer groups + replay | Heavyweight; only past a real throughput bar |
| "Full-text / log search / analytics" | **Managed OpenSearch** | Inverted index, aggregations | Not a source of truth; sync/lag to manage |
| "Semantic / similarity search, RAG" | **Vector database** or Postgres + `pgvector` | Embedding search as a first-class primitive | Start with pgvector; separate service only when it hurts |
| "Store user uploads / large files / static assets" | **Spaces** (S3-compatible) + Spaces CDN | Cheap, durable, offloads bytes from your app | Eventual consistency on some ops; egress cost |
| "Serve assets fast, globally" | **CDN** (Spaces CDN or in front of LB) | Edge caching, TLS | Cache invalidation is your problem |
| "Private service-to-service traffic" | **VPC** + private DB networking | Traffic never traverses public internet | Regional; cross-region needs explicit plumbing |
| "Lock down access" | **Cloud Firewall** + DB **trusted sources** | Defense in depth, tag-based rules | Easy to lock yourself out — always keep SSH open |
| "Ship containers" | **DOCR** (Container Registry) | Native App Platform + DOKS integration | Registry tiers have storage limits |
| "Stable IP for failover / allowlisting" | **Reserved IP** | Remap to another Droplet in seconds | Regional; not a load balancer substitute |
| "Shared filesystem across nodes" | **Block Storage Volume** (single-attach) or **NFS** (multi-attach) | Persistence beyond Droplet lifecycle | Block volumes attach to one Droplet — the classic gotcha |
| "Egress from private nodes via fixed IP" | **VPC NAT Gateway** | Stable outbound IP for third-party allowlists | Extra hop, extra cost |
| "Secrets shouldn't be in the repo" | **Secrets Manager**, or App Platform `SECRET`-type env vars | Encrypted at rest, injected at runtime | App Platform secrets are write-only once set |
| "How do we know it broke?" | **Monitoring** + alert policies | Free host/app metrics, alert destinations | Not APM — no distributed tracing |
| "LLM features" | **Gradient** (agents, KBs, models) + serverless/dedicated inference | Managed inference on-platform | Model selection narrower than a raw provider |

**Meta-answer for the design rounds.** When asked "how would you scale this?", walk the
bottleneck chain rather than listing products:

> *Stateless API replicas scale trivially — App Platform instance count or a Droplet autoscale
> pool behind a Load Balancer. The next bottleneck is Postgres write throughput, so I'd add a
> connection pool (PgBouncer, managed by DO), then read replicas for the dashboard's read path.
> After that the bottleneck is SSE fan-out, because every replica currently holds a
> `LISTEN/NOTIFY` connection — I'd move fan-out to Managed Valkey pub/sub. Past that, the event
> log itself wants Kafka with consumer groups. I wouldn't do any of it until measurements say so.*

That answer scores because it's **ordered, causal, and measured** — not a product list.

---

# Part 2 — Primitive reference

## Compute

**Droplets** — VMs. Sizes `s-1vcpu-1gb` (~$6/mo) upward; shared vs dedicated CPU.
```bash
doctl compute droplet create api-1 --region blr1 --size s-1vcpu-2gb \
  --image docker-20-04 --ssh-keys <fp> --vpc-uuid <uuid> \
  --user-data-file cloud-init.yaml --wait
doctl compute droplet list
doctl compute ssh api-1
```
`--user-data-file` (cloud-init) is the trick that makes a Droplet deploy reproducible and fast —
it installs and starts your stack on first boot. Note `blr1` = Bangalore, the closest region.

**Droplet autoscale pools** — `doctl compute droplet-autoscale`. Target CPU/memory utilization,
min/max. Pair with a Load Balancer. Only works if your service is stateless.

**Load Balancers** — managed L4/L7, health checks, TLS termination, sticky sessions (which you
should *not* need if you're stateless — a good point to make out loud).

**Reserved IPs (v4/v6)** — static IP remappable between Droplets. The poor-man's failover:
health check fails → remap → DNS unchanged, no propagation delay.

**Block Storage Volumes** — network block devices, resizable, snapshot-able. **Attach to exactly
one Droplet at a time** — the standard interview gotcha when someone proposes shared state.

**NFS** — the multi-attach answer when you genuinely need a shared POSIX filesystem. Usually a
smell; prefer Spaces for blobs.

**Snapshots / Images / Custom images** — golden images to cut boot time; `doctl compute snapshot`.

**VPC** — private network per region. Managed DBs, Droplets, LBs join it. Private traffic is
free and doesn't traverse the public internet.

**VPC NAT Gateway** — stable egress IP for nodes without public IPs; needed when a third party
allowlists your outbound address.

**Cloud Firewalls** — stateful, **tag-based** (`--tag-names web`), so new Droplets inherit rules
automatically. Inbound-deny by default.

**Domains / DNS / Certificates / CDN** — `doctl compute domain records create`,
Let's Encrypt certs managed for you, CDN in front of Spaces or an LB.

**Tags & Projects** — tags drive firewall/LB membership; projects group resources for billing
and organization. Mentioning projects signals you've run this at more than toy scale.

## Platform

**App Platform** — the PaaS. This is your primary deploy target tomorrow.

Component types, and knowing these distinctions is worth points:
- `service` — long-running, receives HTTP traffic, gets a public route
- `static_site` — built assets on CDN, **free-ish and fast**; your React build goes here
- `worker` — long-running, **no public route** — the correct home for a background job processor
- `job` — runs to completion; `PRE_DEPLOY` / `POST_DEPLOY` / `FAILED_DEPLOY` hooks.
  **`PRE_DEPLOY` is the right place for database migrations** — a strong detail to volunteer.
- `function` — serverless within an app

Build from **buildpack** (auto-detected, zero config) or **Dockerfile** (explicit, reproducible).
Say why you chose: buildpacks are faster to ship, Dockerfiles give parity with local dev.

```bash
doctl apps propose --spec .do/app.yaml       # validate/price BEFORE creating
doctl apps create --spec .do/app.yaml --wait
doctl apps list
doctl apps logs <app-id> --type build --follow
doctl apps logs <app-id> <component> --type run --follow
doctl apps update <app-id> --spec .do/app.yaml
doctl apps create-deployment <app-id> --force-rebuild
doctl apps spec get <app-id> > current.yaml
```
Gives you HTTPS on `*.ondigitalocean.app` automatically — no cert work.
`doctl apps propose` is underused: it validates the spec and shows cost before you spend.

**DOKS (Kubernetes)** — `doctl kubernetes cluster create --count 3 --size s-2vcpu-4gb`,
then `doctl kubernetes cluster kubeconfig save <name>`. Node pools, autoscaling, DO CSI driver
maps PVCs to Block Storage, and Service type=LoadBalancer provisions a real DO LB.
**Correct interview take:** right answer for a platform team with many services; overkill for
a single app on a 3-hour clock, and saying so is better than reaching for it.

**Functions (serverless)** — `doctl serverless connect`, `deploy`. Scale-to-zero, event-driven.
Poor fit for long-running orchestration; good for webhooks and cron-ish glue.

## Data

**Managed Databases** — engines available on your account: `pg`, `advanced_pg`, `mysql`,
`advanced_mysql`, `mongodb`, **`valkey`**, **`kafka`**, `opensearch`.

```bash
doctl databases create jobs-db --engine pg --version 16 \
  --size db-s-1vcpu-1gb --num-nodes 1 --region blr1
doctl databases connection <id> --format URI
doctl databases pool create <id> --name pool --mode transaction --size 20 --db jobs
doctl databases firewalls append <id> --rule app:<app-uuid>
doctl databases replica create <id> --name read-1 --size db-s-1vcpu-1gb
```

Features worth naming: automated daily backups + **PITR**, standby nodes for HA failover,
**read replicas** (including cross-region), **connection pooling** (PgBouncer — `transaction`
mode is the one you want for many short-lived app connections), and **trusted sources**
(firewall the DB to only your app/droplet/tag, not the internet).

> **The connection-pool point is a real design-round scorer.** Managed Postgres on a small plan
> caps at a modest connection count. N stateless API replicas × M pool connections exhausts it
> fast. The fix is a transaction-mode pooler in front. Volunteering this shows you've actually
> operated a scaled service.

**Valkey** — Redis-compatible. Cache, rate limiter, distributed lock, and **pub/sub for SSE
fan-out across replicas**. This is your stated upgrade path.

**Kafka** — durable partitioned log, consumer groups, replay. The upgrade past pub/sub when you
need durability and multiple independent consumers.

**OpenSearch** — search and log analytics. Never the source of truth.

**Vector databases** — managed vector store for embeddings/RAG. For most workloads, start with
`pgvector` inside the Postgres you already have; graduate when scale demands.

## Storage

**Spaces** — S3-compatible object storage. **This is why `s3cmd` is pre-installed in your
environment** — treat that as a hint that uploads/assets may feature tomorrow.
```bash
s3cmd --configure                     # endpoint: blr1.digitaloceanspaces.com
s3cmd put file s3://bucket/key
doctl spaces keys create my-key       # access/secret pair
```
Works with `boto3` by overriding `endpoint_url` — no code rewrite from S3.
Pattern to cite: **presigned URLs** so clients upload directly to Spaces, keeping bytes out of
your API entirely. Add **Spaces CDN** for read-heavy assets.

**Container Registry (DOCR)** — `doctl registry create`, `doctl registry login`,
`doctl registry kubernetes-manifest`. Integrates natively with App Platform and DOKS, so you can
deploy an image without a public registry.

## AI — Gradient

`doctl gradient agent` · `knowledge-base` · `list-models` · plus serverless and dedicated
inference endpoints, and OpenAI-compatible keys. Not needed tomorrow, but knowing DO has a
first-party AI platform is good awareness if it comes up.

## Ops

**Monitoring** — `doctl monitoring alert` — CPU/memory/disk/bandwidth alert policies with email
and Slack destinations. Free. Not APM: no distributed tracing, so pair with structured logs.

**Secrets Manager** — `doctl secrets`. Or App Platform `SECRET`-type env vars (encrypted,
write-only after being set).

**CSPM security scans** — `doctl security` — posture management. Nice awareness point.

**Billing** — `doctl balance get`, `doctl invoice list`. Being able to talk about cost is a
genuine seniority signal.

---

# Part 3 — Commands you'll actually type tomorrow

```bash
# 0. Confirm the credits/account you were given
doctl account get
doctl balance get

# 1. Validate the spec BEFORE spending anything
doctl apps propose --spec .do/app.yaml

# 2. Deploy
doctl apps create --spec .do/app.yaml --wait

# 3. Watch the build (this is where failures surface)
APP=$(doctl apps list --format ID --no-header | head -1)
doctl apps logs $APP --type build --follow

# 4. Get the public URL
doctl apps get $APP --format DefaultIngress --no-header

# 5. Runtime logs when something 500s
doctl apps logs $APP api --type run --follow

# 6. Redeploy after a push
doctl apps create-deployment $APP --wait

# --- Fallback: Droplet path, ~4 minutes, no GitHub dependency ---
doctl compute droplet create demo --region blr1 --size s-2vcpu-4gb \
  --image docker-20-04 --ssh-keys $(doctl compute ssh-key list --format ID --no-header | head -1) \
  --user-data-file deploy/cloud-init.yaml --wait
doctl compute droplet list --format Name,PublicIPv4

# --- Teardown (do not forget) ---
doctl apps delete $APP -f
doctl databases delete <db-id> -f
doctl compute droplet delete demo -f
```

## Gotchas that cost real time

1. **Bind to `0.0.0.0`, not `127.0.0.1`.** In a container, localhost-bound = health check fails =
   deploy loops forever. The single most common App Platform failure.
2. **Respect `$PORT`.** App Platform injects it; hardcoding 8000 while the spec says 8080 fails
   health checks silently.
3. **Health check path must exist and be cheap.** If `/healthz` touches the DB and the DB isn't
   ready, your app never goes healthy. Keep liveness dumb; put DB checks in readiness.
4. **Managed PG requires TLS** (`sslmode=require`) and gives a `postgresql://` URI —
   SQLAlchemy's async driver needs `postgresql+asyncpg://`. Rewrite the scheme in config.
5. **`${db.DATABASE_URL}` binds the DB into the app spec** — don't paste credentials.
6. **SSE through a proxy needs buffering off** — `X-Accel-Buffering: no` and
   `Cache-Control: no-cache`, or events arrive in batches and your "live" demo looks broken.
7. **Static site + API on one app** — route the API at `/api` and the SPA at `/`, so there's no
   CORS at all. Simpler than configuring CORS under time pressure.
8. **Migrations belong in a `PRE_DEPLOY` job**, not in app startup — otherwise N replicas race
   to migrate simultaneously.
9. **Block Storage attaches to one Droplet.** If you propose shared state on a volume, expect
   to be challenged.
10. **Firewalls: always keep SSH (22) open** before applying, or you lock yourself out.
