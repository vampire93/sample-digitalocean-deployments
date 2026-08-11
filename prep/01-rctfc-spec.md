# RCTFC — Turning an Ambiguous Prompt into a Spec in 10 Minutes

> **Use this at minute 0 of the 3-hour build.** Ten minutes spent here buys back an hour later,
> and the artifact it produces is the thing you read from in the review session.

---

## Why bother when the clock is running

The prompt you get tomorrow will be **deliberately underspecified**. That is the test. There is
no hidden "correct" requirement you're supposed to guess — they want to see whether you can take
an ambiguous problem, make defensible choices, write them down, and move.

Three things go wrong for candidates who skip this step:

1. **They build the wrong thing confidently.** No written scope means scope drifts for 3 hours.
2. **They can't answer "what did you leave out?"** — a *stated* review-session question. Without
   a written out-of-scope list, every gap looks like an oversight instead of a decision.
3. **They run out of time and have nothing coherent.** A spec lets you cut the right things.

RCTFC is just a checklist that guarantees you've thought about all five dimensions before you
type any code. Fill it in a `SPEC.md` at the repo root and **commit it first**. Your first commit
being a spec is itself a signal.

---

## The five fields

### R — Role
*Who is this for, and what quality bar am I building to?*

Name the actual user and the standard. This one line silently decides a hundred later
micro-choices (do I need auth? pagination? audit trail?).

> *"An internal operations engineer who needs to submit and monitor bulk processing jobs.
> Production-grade internal service: correctness and observability over feature count.
> Single trusted tenant — no multi-tenancy, no public exposure."*

### C — Context
*Where does this sit in the world?*

Upstreams, downstreams, what exists vs. what I'm adding, what I must not break. For an
interview prompt this is mostly: what external systems am I orchestrating, and what do I
assume about them?

> *"Orchestrates calls to a third-party API that is rate-limited, occasionally returns 5xx,
> and has p99 latency of several seconds. I control neither its availability nor its latency.
> Greenfield — nothing to preserve."*

The **assumptions about the dependency** are the valuable part. "It can fail and I must survive
that" is the sentence that generates your entire retry/idempotency design.

### T — Task
*What am I building, as numbered, independently verifiable capabilities?*

Not prose. A numbered list where each item becomes an endpoint, a screen, or a test. If you
can't state how you'd verify it, it's not written clearly enough yet.

> 1. Submit a job with a typed payload; reject invalid payloads with field-level errors.
> 2. Job executes asynchronously; caller is not blocked.
> 3. Transient failures retry with backoff, up to N attempts, then land in a dead-letter state.
> 4. Duplicate submissions with the same idempotency key do not create duplicate work.
> 5. Dashboard lists jobs with live status updates, no manual refresh.
> 6. Operator can inspect per-attempt history and manually retry a dead-lettered job.

Now **rank them**. Mark each `MUST` / `SHOULD` / `WON'T`. You will not finish everything —
decide *now* which ones you sacrifice, while you're calm, rather than at minute 150 in a panic.

### F — Format
*The interface contract, written before implementation.*

This is where you stop hand-waving. Nail down:

- **Resources & routes** — `POST /api/jobs`, `GET /api/jobs?status=&cursor=`, `GET /api/jobs/{id}`
- **Status codes** — `202` for accepted-async (not `200`), `409` for idempotency conflict,
  `422` for validation, `404`, `503` for dependency-down
- **Error envelope** — one shape, everywhere:
  ```json
  { "error": { "code": "VALIDATION_FAILED", "message": "...", "details": [...], "request_id": "..." } }
  ```
- **Event/stream shape** — SSE event names, payload, resume semantics
- **Screen inventory** — list view, detail view, submit form; and for each, its
  **loading / empty / error** state (the brief names these explicitly — treat as a checklist)

Writing the error envelope *first* is disproportionately valuable: it's the difference between
"sensible validation and meaningful error handling" being designed vs. bolted on.

### C — Constraints & Criteria
*Non-functionals, acceptance criteria, and the out-of-scope list.*

- **Constraints:** 3-hour budget · must deploy to DigitalOcean · Python + React ·
  expected scale (state a number, even a made-up one — it justifies your choices) ·
  consistency requirements
- **Acceptance criteria:** the concrete demo you'll perform. *"Submit 50 jobs, kill a worker
  mid-flight, show every job still reaches a terminal state exactly once."*
- **Out of scope — write this down explicitly:**
  > *No authn/authz (single trusted operator). No multi-tenancy. No horizontal autoscaling.
  > In-process worker rather than a separate deployable. Metrics logged, not exported to
  > Prometheus. These are deliberate: each is a known extension point, noted in the README.*

That paragraph is worth real points. It converts every missing feature from a gap into a
decision — and it's the direct answer to "what did you choose to leave out?"

---

## Fill-in template

Copy this into `SPEC.md` as your first commit.

```markdown
# SPEC — <service name>
*Written at T+0. Assumptions stated, not asked.*

## R — Role
User: ...
Quality bar: ...

## C — Context
Upstream / downstream: ...
Assumptions about dependencies: ...
Failure modes I must survive: ...

## T — Task
| # | Capability | Priority | Verified by |
|---|---|---|---|
| 1 |  | MUST |  |
| 2 |  | SHOULD |  |
| 3 |  | WON'T |  |

## F — Format
### Routes
| Method | Path | Success | Errors |
|---|---|---|---|

### Error envelope
```json
{ "error": { "code": "", "message": "", "details": [], "request_id": "" } }
```

### Events (SSE)
Event names, payload shape, resume semantics.

### Screens
| Screen | Loading | Empty | Error |
|---|---|---|---|

## C — Constraints & Criteria
Constraints: ...
Acceptance criteria (the demo I will perform): ...

### Out of scope — deliberate
- ... (and the extension point it would hook into)

## Open questions & assumptions taken
| Ambiguity | Assumption I chose | Why | Cost if wrong |
|---|---|---|---|
```

---

## Worked example

**Prompt (realistic DO-style):** *"Build a service that accepts image-processing requests,
processes them asynchronously against an external API, and provides a dashboard to monitor
progress. Deploy it."*

### R — Role
Internal ops engineer at a media company submitting batches of images for transformation.
Production-grade internal tool: correctness, observability, graceful degradation over breadth.
Single trusted tenant.

### C — Context
Orchestrates an external image-transform API. Assumed: rate-limited (429 with `Retry-After`),
intermittently 5xx, seconds-scale latency, **not idempotent** on its side. Results land in
object storage (DO Spaces). Greenfield. Must survive: dependency down, dependency slow,
dependency duplicating work, my own process dying mid-job.

### T — Task
| # | Capability | Priority | Verified by |
|---|---|---|---|
| 1 | Submit job (URL + transform params), validated | MUST | 422 on bad params w/ field errors |
| 2 | Async execution, caller gets `202` + job id | MUST | POST returns immediately |
| 3 | Retry w/ exp backoff + jitter, honor `Retry-After`, dead-letter after N | MUST | Fault-injected 429/500 |
| 4 | Idempotency key dedupes submissions | MUST | Double POST → one job, `409` |
| 5 | Dashboard: live job list, no refresh | MUST | SSE visibly updates |
| 6 | Job detail w/ per-attempt timeline | SHOULD | Detail screen |
| 7 | Manual retry of dead-lettered job | SHOULD | Button works |
| 8 | Bulk CSV upload | WON'T | — |

### F — Format
`POST /api/jobs` → `202 {id, status}` · `Idempotency-Key` header · `409` on key reuse w/ different body
`GET /api/jobs?status=&cursor=&limit=` → cursor-paginated
`GET /api/jobs/{id}` → job + attempts
`POST /api/jobs/{id}/retry` → `202`, `409` if not terminal
`GET /api/events?since=<seq>` → SSE, honors `Last-Event-ID`
`GET /healthz` (liveness) · `GET /readyz` (checks DB)

Errors: single envelope, always carries `request_id`.
Screens: **List** (spinner / "No jobs yet — submit one" / retryable error banner) ·
**Detail** (skeleton / n-a / not-found state) · **Submit** (inline field errors, disabled-while-submitting).

### C — Constraints & Criteria
3h · deploy to DO App Platform + Managed Postgres · Python/FastAPI + React/TS ·
target 10k jobs/day, ~10 concurrent workers · at-least-once execution with idempotent effects
(**not** exactly-once — stated deliberately).

**Acceptance demo:** submit 50 jobs against a fault-injecting fake dependency (30% failure),
kill a worker mid-flight, show all 50 reach a terminal state, none processed twice, dashboard
live throughout.

**Out of scope (deliberate):** no auth (single trusted operator; would add JWT middleware at the
router layer) · no multi-tenancy (would add `tenant_id` + RLS) · workers run as a separate
compose service but not autoscaled (would move to App Platform worker component) · no
Prometheus export (structured logs carry the same data; would add `/metrics`).

### Assumptions taken
| Ambiguity | Assumption | Why | Cost if wrong |
|---|---|---|---|
| Exactly-once required? | At-least-once + idempotency keys | Exactly-once across an external API is not achievable; honest design | If they wanted dedupe *at the dependency*, need a reservation table |
| Auth needed? | None, single trusted operator | Not mentioned; auth is time-expensive and low-signal here | 20 min to add middleware |
| Result storage? | Store URL reference, not bytes | Keeps Postgres small, Spaces is the right primitive | — |

---

## The three habits this encodes

1. **State assumptions out loud, then proceed.** Do not stall waiting for clarification. In the
   review say: *"The prompt didn't specify X. I assumed Y because Z. If it were actually W,
   the change is localized to this layer."* That is a senior answer.
2. **Decide what you're cutting at minute 10**, not minute 150.
3. **Your spec is the review-session script.** You'll be nervous and tired after 3 hours of
   building. Reading from a document you wrote while calm is a real advantage.
