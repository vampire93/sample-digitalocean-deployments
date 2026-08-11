# The 3-Hour Playbook

> Read this on the way in. It is a schedule, a deploy runbook, and a script for the review
> sessions. Nothing here requires you to remember anything under pressure.

---

## Part 1 — The timebox

The failure mode is not "can't code." It's **finishing the code at 2:58 with nothing deployed
and no tests.** Defend the schedule.

| Time | Do | Non-negotiable output |
|---|---|---|
| **0:00–0:10** | Write `SPEC.md` (RCTFC). Commit it. | Scope + out-of-scope written down |
| **0:10–0:25** | Schema + API contract. Migration written. | Tables + routes decided |
| **0:25–0:40** | Skeleton: `/healthz`, one real endpoint, Dockerfile, compose | App runs locally |
| **0:40–0:55** | **DEPLOY THE SKELETON** | Public HTTPS URL, live |
| **0:55–1:45** | Backend vertical slice: submit → queue → worker → status | Core loop works |
| **1:45–2:20** | Frontend: list + submit + live updates, all 3 states | Dashboard usable |
| **2:20–2:35** | Tests: pure domain units + 2–3 API tests | `pytest` green |
| **2:35–2:45** | **REDEPLOY** the real thing | Public URL shows the real app |
| **2:45–3:00** | README, cleanup, buffer | Reviewable repo |

### The single highest-value habit: deploy at 0:40, not 2:40

Deploy a **hello-world skeleton** to DigitalOcean in the first hour, while fixing it is cheap and
you're calm. Then every later deploy is a redeploy of a proven pipeline.

Candidates who leave deployment to the end discover their port binding is wrong, or the DB URL
scheme is incompatible, or the build image lacks a dependency — at minute 165, with no time.
You will have already paid that cost at minute 45.

**Corollary: commit constantly.** `git commit` after every working increment. A reviewer reading
your history sees the shape of your thinking, and you always have a working state to fall back to.

### When you fall behind (you will)

Cut in this order — and **say out loud that you're cutting, and why**:
1. Frontend polish (styling, animations) — never frontend *states*
2. The `SHOULD` items in your spec
3. Breadth of tests — but **never all tests**; 3 meaningful tests beat 0, and 0 reads as
   "doesn't test"
4. **Never cut:** the deploy, the error envelope, the loading/empty/error states, the README

---

## Part 2 — Deploy runbook

*Fill the timings in tonight after the rehearsal; then tomorrow it's copy-paste.*

```bash
# Sanity — confirm you're on the credited account
doctl account get && doctl balance get

# Validate before spending
doctl apps propose --spec .do/app.yaml

# Ship
doctl apps create --spec .do/app.yaml --wait
APP=$(doctl apps list --format ID --no-header | head -1)

# Watch the build — this is where it fails
doctl apps logs $APP --type build --follow

# URL
doctl apps get $APP --format DefaultIngress --no-header

# Redeploy after pushing
git push && doctl apps create-deployment $APP --wait

# Runtime logs
doctl apps logs $APP api --type run --follow
```

**If App Platform fights you for more than 10 minutes, switch to the Droplet fallback.**
Don't debug a PaaS build on the clock — you have a scripted alternative:
```bash
doctl compute droplet create demo --region blr1 --size s-2vcpu-4gb \
  --image docker-20-04 --ssh-keys <fp> --user-data-file deploy/cloud-init.yaml --wait
```
Deciding to switch is itself a good engineering signal. Say why.

### Pre-flight checklist (30 seconds, saves 30 minutes)
- [ ] App binds `0.0.0.0`, port from `$PORT`
- [ ] `/healthz` is dumb (no DB); `/readyz` checks DB
- [ ] `DATABASE_URL` scheme rewritten for the async driver; `sslmode=require`
- [ ] Migrations in a `PRE_DEPLOY` job, not app startup
- [ ] SSE sets `Cache-Control: no-cache` + `X-Accel-Buffering: no`
- [ ] API routed at `/api`, SPA at `/` → no CORS
- [ ] No secrets committed; env vars via spec

---

## Part 3 — Rehearsed answers

Each is ~60–90 seconds. Structure: **decision → why → what it costs → when I'd change it.**
That last clause is what separates IC3 from IC2.

### SSE vs WebSocket vs polling
> I chose SSE. The data flow is one-directional — server pushes job status, the client never
> streams back — and SSE gives that over plain HTTP/1.1 with automatic browser reconnection and
> `Last-Event-ID` resume built in. WebSockets would mean a second protocol, sticky sessions, and
> my own reconnect logic for a bidirectional channel I don't need. Polling is simplest and I'd
> genuinely ship it if update latency tolerance were 30s+, but at 500 jobs it's a lot of wasted
> requests. The cost of SSE is a held connection per client and the ~6-per-domain HTTP/1.1
> connection cap — fine at operator-dashboard scale, and HTTP/2 removes the cap. I'd move to
> WebSockets the moment the client needs to send a continuous stream back.

### Normalization vs materialized views
> The core is normalized to 3NF because it's the write path and I want correctness enforced by
> the schema — foreign keys, unique constraints, no update anomalies. But the dashboard asks
> aggregate questions across the whole job table, which is an expensive scan on every page load.
> So I denormalize *deliberately* into a materialized view keyed by type × status × hour bucket.
> It has a unique index specifically so I can `REFRESH MATERIALIZED VIEW CONCURRENTLY` without
> blocking readers. The trade-off is bounded staleness — so the API returns an `as_of` timestamp
> and the UI displays it. Staleness you can see is a trade-off; staleness you can't is a bug.
> Under heavier load I'd move to incremental rollups on write rather than periodic full refresh.

### Concurrent state management — why `SKIP LOCKED` and not a queue
> Multiple workers pull from one job table, so the risk is two workers claiming the same job.
> I claim with `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT n) RETURNING *`.
> `FOR UPDATE` locks the candidate rows; `SKIP LOCKED` makes concurrent workers step over locked
> rows instead of blocking, so N workers get N disjoint batches with no contention and no
> external broker. It's one atomic statement, so there's no claim-then-crash window.
>
> I didn't add Redis or RabbitMQ because Postgres was already a required dependency and adding a
> broker means another thing to deploy, monitor, and reason about during failures. That holds to
> roughly thousands of jobs/minute. Past that the polling load on Postgres becomes the problem
> and I'd move to a real broker — DO Managed Valkey, then Kafka if I need replay and consumer
> groups.

### The failure case they will probe: worker dies mid-job
> That's why claiming sets a **lease**: `locked_by` and `lease_expires_at`. Workers heartbeat to
> extend it. A reaper sweeps rows where the lease expired and returns them to `queued`. So a
> hard-killed worker's job is reclaimed within one lease interval rather than being stranded in
> `running` forever.
>
> The honest consequence is that this is **at-least-once, not exactly-once** — if a worker
> completes the side effect and dies before committing, the job re-runs. Exactly-once across an
> external API isn't achievable, so instead I make the effects idempotent: an idempotency key on
> submission, and attempts recorded per-attempt so a retry is detectable. I'd rather have honest
> at-least-once with idempotent effects than a system that claims exactly-once and quietly
> drops work.

### How would you scale this?
Walk the **bottleneck chain**, in order, with "I'd measure first":
> API is stateless, so replicas scale horizontally — App Platform instance count, or a Droplet
> autoscale pool behind a Load Balancer. First real bottleneck is Postgres connections: N
> replicas × pool size exhausts a managed plan quickly, so a transaction-mode PgBouncer pool
> goes in. Then dashboard reads go to a read replica. Then SSE fan-out — every replica holds a
> `LISTEN/NOTIFY` connection, which doesn't scale indefinitely, so fan-out moves to Valkey
> pub/sub. Then the event log itself to Kafka for retention and consumer groups. I wouldn't do
> any of it speculatively — each step is a response to a measurement.

### Stateless services, and where statefulness sneaks in
> Both the API and the workers hold no state in memory — everything lives in Postgres. That's
> what lets me run N replicas with no session affinity and no sticky routing, and restart any
> instance at any time. The one place statefulness sneaks in is the SSE connection itself, which
> is inherently bound to one replica. I handle that by making every replica subscribe to the
> same `LISTEN/NOTIFY` channel, so it doesn't matter which one a client lands on — and by
> supporting `Last-Event-ID` so a reconnect to a *different* replica resumes without gaps.

### What would you do with another day?
Have this ready — it proves you know what's missing:
> Auth and per-operator audit trail; OpenTelemetry tracing across the API→worker→dependency
> hop, since structured logs alone don't give me latency attribution; a real dead-letter
> management UI; load tests to find the actual bottleneck instead of my guess; incremental
> rollups instead of full matview refresh; and moving workers to a separate deployable so they
> scale independently of the API.

### Frontend-round specifics
- **Structure:** typed API client in one module, feature-based folders, no `fetch` inside
  components. Server state and view state kept separate.
- **The three states, per surface:** loading (skeleton, not a spinner-on-blank), empty (with the
  action that resolves it), error (with a retry, and the `request_id` so it's traceable).
- **SSE robustness:** exponential-backoff reconnect, resume via `Last-Event-ID`, and a visible
  connection indicator so the operator knows if data is live or stale.
- **Optimistic vs pessimistic updates:** I stayed pessimistic — the server is the truth and SSE
  delivers the update within ~100ms. Optimistic UI would mean reconciliation logic for a
  latency win the operator won't perceive.
- **Accessibility/perf if asked:** semantic table markup, labelled controls, virtualize the list
  past ~1000 rows.

---

## Part 4 — Behavioral (45 min)

Prepare **5 STAR stories** you can retell in 2 minutes each. Reuse across questions.

| Story | Covers |
|---|---|
| A production incident you debugged/mitigated | Ownership, calm under pressure, systems thinking |
| A technical disagreement you resolved | Collaboration, ego-free engineering, influence |
| Something you shipped end-to-end | Autonomy, scope management |
| A time you were wrong / it failed | Humility, learning — **do not skip this one** |
| Mentoring or lifting a teammate | Seniority beyond code |

For each: **S**ituation (1 line) → **T**ask (your responsibility) → **A**ction (what *you* did,
first person, specific) → **R**esult (a number if possible, plus what you'd do differently).

**DigitalOcean's values skew toward simplicity, developer empathy, and customer focus** — their
entire product thesis is "cloud without the complexity." Frame stories toward *making things
simpler for the people who use them*, not toward maximal cleverness.

Questions to ask them (have 3 ready — this is graded):
- What does the path from IC3 to IC4 actually look like here?
- What's the current biggest source of operational pain for this team?
- How do you decide what to build in-house vs. adopt?

---

## Part 5 — Morning of

- **Reporting time 8:45 AM IST**, DigitalOcean Office, 22nd Floor, Orbit, Hyderabad Knowledge City.
- **Get the QR code** from the "Aurobindo Orbit"/"iSprout" SMS **before you leave** — building
  entry depends on it.
- Emergency contact: **Soumya** — number is in the confirmation email. Save it to your
  phone the night before; don't rely on finding the email on the day.
- Bring: ID, laptop charger (even though they provide a laptop), water, something to eat —
  it's a long day.
- On the provided laptop, first 5 minutes: `doctl auth init` with their credits, confirm
  `docker`/Orbstack runs, confirm `git` identity, and **verify you can reach the internet from a
  container**. Find environment problems before the clock matters.

**Last thing: narrate your thinking as you build.** They said explicitly they care about *how*
you think, not only what you produce. A quiet three hours followed by a good demo scores worse
than a running commentary of the trade-offs you're making as you make them.
