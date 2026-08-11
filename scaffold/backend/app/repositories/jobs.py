"""The only layer that speaks SQL.

Everything above this file deals in dicts and domain types; swapping Postgres for
something else would touch this module alone.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from app.core.db import pool
from app.domain.job import JobStatus

# Columns returned to callers. Explicit rather than SELECT * so a schema change
# can't silently widen the API surface.
_BASE_COLS = """
    j.id, j.job_type_id, j.status, j.priority, j.payload,
    j.idempotency_key, j.attempts, j.max_attempts, j.run_after, j.locked_by,
    j.lease_expires_at, j.last_error, j.result, j.created_at, j.updated_at,
    j.started_at, j.finished_at
"""
# For SELECTs, which join job_types directly.
_JOB_COLS = f"{_BASE_COLS}, jt.name AS job_type"
# For the claim UPDATE, where adding a second FROM-item would change the update's
# row set. A correlated subquery keeps the semantics obvious.
_CLAIM_COLS = f"{_BASE_COLS}, (SELECT name FROM job_types WHERE id = j.job_type_id) AS job_type"


def _row(r: asyncpg.Record | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for key in ("payload", "result"):
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    return d


# --------------------------------------------------------------------- writes --
async def get_job_type(name: str) -> dict[str, Any] | None:
    return _row(
        await pool().fetchrow("SELECT id, name, max_attempts FROM job_types WHERE name = $1", name)
    )


async def list_job_types() -> list[dict[str, Any]]:
    rows = await pool().fetch(
        "SELECT id, name, description, max_attempts FROM job_types ORDER BY name"
    )
    return [dict(r) for r in rows]


async def create_job(
    *,
    job_type_id: int,
    payload: dict[str, Any],
    max_attempts: int,
    priority: int = 0,
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Insert a job. Returns ``(job, created)``.

    ``created=False`` means the idempotency key already existed — the caller gets the
    original job back rather than a duplicate. This is what makes client retries safe.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO jobs (job_type_id, status, priority, payload,
                                  idempotency_key, max_attempts)
                VALUES ($1, 'queued', $2, $3::jsonb, $4, $5)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                job_type_id,
                priority,
                json.dumps(payload),
                idempotency_key,
                max_attempts,
            )
            if row is None:
                existing = await conn.fetchrow(
                    f"SELECT {_JOB_COLS} FROM jobs j "
                    "JOIN job_types jt ON jt.id = j.job_type_id "
                    "WHERE j.idempotency_key = $1",
                    idempotency_key,
                )
                return _row(existing), False

            await _append_event(conn, row["id"], "job.created", {})
            created = await conn.fetchrow(
                f"SELECT {_JOB_COLS} FROM jobs j "
                "JOIN job_types jt ON jt.id = j.job_type_id WHERE j.id = $1",
                row["id"],
            )
            return _row(created), True


# ---------------------------------------------------------------------- reads --
async def get_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    return _row(
        await pool().fetchrow(
            f"SELECT {_JOB_COLS} FROM jobs j "
            "JOIN job_types jt ON jt.id = j.job_type_id WHERE j.id = $1",
            job_id,
        )
    )


async def list_jobs(
    *,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 50,
    cursor_created_at: datetime | None = None,
    cursor_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Keyset pagination on (created_at, id).

    Keyset rather than OFFSET: OFFSET degrades linearly with depth and can skip or
    repeat rows when the table is being written to concurrently — which it always is.
    """
    where, args = ["TRUE"], []

    if status:
        args.append(status)
        where.append(f"j.status = ${len(args)}")
    if job_type:
        args.append(job_type)
        where.append(f"jt.name = ${len(args)}")
    if cursor_created_at is not None and cursor_id is not None:
        args.extend([cursor_created_at, cursor_id])
        where.append(f"(j.created_at, j.id) < (${len(args)-1}, ${len(args)})")

    args.append(limit)
    rows = await pool().fetch(
        f"""
        SELECT {_JOB_COLS} FROM jobs j
        JOIN job_types jt ON jt.id = j.job_type_id
        WHERE {' AND '.join(where)}
        ORDER BY j.created_at DESC, j.id DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    return [_row(r) for r in rows]


async def get_attempts(job_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """
        SELECT id, attempt_no, worker_id, status, error, started_at, finished_at, duration_ms
        FROM job_attempts WHERE job_id = $1 ORDER BY attempt_no
        """,
        job_id,
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------- the claim (centrepiece) --
async def claim_jobs(*, worker_id: str, batch: int, lease_seconds: int) -> list[dict[str, Any]]:
    """Atomically claim up to ``batch`` runnable jobs for this worker.

    FOR UPDATE SKIP LOCKED is what makes this safe under concurrency: the inner
    SELECT row-locks its candidates, and any *other* worker running the same
    statement steps over those locked rows instead of blocking on them. So N workers
    get N disjoint batches, with no coordinator and no external broker.

    It is one statement, so there is no claim-then-crash window: either the rows are
    marked running and returned, or nothing happened at all.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                f"""
                WITH claimed AS (
                    SELECT id FROM jobs
                    WHERE status = 'queued' AND run_after <= now()
                    ORDER BY priority DESC, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE jobs j
                SET status           = 'running',
                    locked_by        = $2,
                    lease_expires_at = now() + make_interval(secs => $3),
                    attempts         = j.attempts + 1,
                    started_at       = COALESCE(j.started_at, now()),
                    updated_at       = now()
                FROM claimed c
                WHERE j.id = c.id
                RETURNING {_CLAIM_COLS}
                """,
                batch,
                worker_id,
                float(lease_seconds),
            )
            jobs = [_row(r) for r in rows]
            for job in jobs:
                await conn.execute(
                    """
                    INSERT INTO job_attempts (job_id, attempt_no, worker_id, status)
                    VALUES ($1, $2, $3, 'running')
                    ON CONFLICT (job_id, attempt_no) DO NOTHING
                    """,
                    job["id"],
                    job["attempts"],
                    worker_id,
                )
                await _append_event(
                    conn, job["id"], "job.started",
                    {"attempt": job["attempts"], "worker_id": worker_id},
                )
            return jobs


async def heartbeat(*, worker_id: str, job_ids: list[uuid.UUID], lease_seconds: int) -> int:
    """Extend leases for jobs this worker still holds.

    Scoped by locked_by so a worker can never extend a lease the reaper already
    took away from it.
    """
    if not job_ids:
        return 0
    result = await pool().execute(
        """
        UPDATE jobs SET lease_expires_at = now() + make_interval(secs => $3), updated_at = now()
        WHERE id = ANY($1::uuid[]) AND locked_by = $2 AND status = 'running'
        """,
        job_ids,
        worker_id,
        float(lease_seconds),
    )
    return int(result.split()[-1])


# ------------------------------------------------------------------ completion --
async def complete_job(
    *, job_id: uuid.UUID, worker_id: str, attempt_no: int, result: dict[str, Any], duration_ms: int
) -> bool:
    async with pool().acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE jobs
                SET status = 'succeeded', result = $3::jsonb, finished_at = now(),
                    updated_at = now(), locked_by = NULL, lease_expires_at = NULL,
                    last_error = NULL
                WHERE id = $1 AND locked_by = $2 AND status = 'running'
                RETURNING id
                """,
                job_id,
                worker_id,
                json.dumps(result),
            )
            if updated is None:
                # Lease was reaped mid-flight; another worker owns this now.
                return False
            await conn.execute(
                """
                UPDATE job_attempts SET status = 'succeeded', finished_at = now(),
                       duration_ms = $3
                WHERE job_id = $1 AND attempt_no = $2
                """,
                job_id,
                attempt_no,
                duration_ms,
            )
            await _append_event(conn, job_id, "job.succeeded", {"attempt": attempt_no})
            return True


async def fail_job(
    *,
    job_id: uuid.UUID,
    worker_id: str,
    attempt_no: int,
    error: str,
    duration_ms: int,
    next_status: JobStatus,
    delay_seconds: float,
) -> bool:
    async with pool().acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE jobs
                -- A retryable failure goes straight back to 'queued' with run_after in
                -- the future; the backoff *is* the delay. Only exhausted jobs park in
                -- a terminal state.
                SET status     = CASE WHEN $3::text = 'failed' THEN 'queued' ELSE $3::text END,
                    last_error = $4,
                    run_after  = now() + make_interval(secs => $5),
                    finished_at = CASE WHEN $3::text = 'dead_letter' THEN now() ELSE NULL END,
                    updated_at = now(),
                    locked_by  = NULL,
                    lease_expires_at = NULL
                WHERE id = $1 AND locked_by = $2 AND status = 'running'
                RETURNING id
                """,
                job_id,
                worker_id,
                str(next_status),
                error[:2000],
                float(delay_seconds),
            )
            if updated is None:
                return False
            await conn.execute(
                """
                UPDATE job_attempts SET status = 'failed', error = $3, finished_at = now(),
                       duration_ms = $4
                WHERE job_id = $1 AND attempt_no = $2
                """,
                job_id,
                attempt_no,
                error[:2000],
                duration_ms,
            )
            event = "job.dead_lettered" if next_status == JobStatus.DEAD_LETTER else "job.failed"
            await _append_event(
                conn, job_id, event,
                {"attempt": attempt_no, "error": error[:500], "retry_in_s": round(delay_seconds, 2)},
            )
            return True


# ---------------------------------------------------------------------- reaper --
async def reap_expired_leases() -> int:
    """Return jobs whose worker died mid-flight to the queue.

    Without this a hard-killed worker strands its jobs in 'running' forever. This is
    the mechanism that turns "at-most-once but lossy" into "at-least-once".
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= max_attempts THEN 'dead_letter' ELSE 'queued' END,
                    locked_by = NULL,
                    lease_expires_at = NULL,
                    last_error = 'lease expired: worker presumed dead',
                    updated_at = now()
                WHERE status = 'running' AND lease_expires_at < now()
                RETURNING id, attempts, status
                """
            )
            for r in rows:
                await conn.execute(
                    """
                    UPDATE job_attempts SET status = 'failed', finished_at = now(),
                           error = 'lease expired'
                    WHERE job_id = $1 AND attempt_no = $2 AND status = 'running'
                    """,
                    r["id"],
                    r["attempts"],
                )
                await _append_event(
                    conn, r["id"], "job.lease_expired", {"attempt": r["attempts"]}
                )
            return len(rows)


# ------------------------------------------------------------ operator actions --
async def retry_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    """Operator-initiated retry of a dead-lettered job."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE jobs
                SET status = 'queued', run_after = now(), max_attempts = max_attempts + 1,
                    last_error = NULL, finished_at = NULL, updated_at = now()
                WHERE id = $1 AND status = 'dead_letter'
                RETURNING id
                """,
                job_id,
            )
            if row is None:
                return None
            await _append_event(conn, job_id, "job.retry_requested", {})
    return await get_job(job_id)


async def cancel_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE jobs
                SET status = 'cancelled', finished_at = now(), updated_at = now(),
                    locked_by = NULL, lease_expires_at = NULL
                WHERE id = $1 AND status IN ('queued', 'failed')
                RETURNING id
                """,
                job_id,
            )
            if row is None:
                return None
            await _append_event(conn, job_id, "job.cancelled", {})
    return await get_job(job_id)


# ------------------------------------------------------------------ read model --
async def refresh_stats() -> None:
    # CONCURRENTLY needs the unique index created in 001_init.sql. Without it this
    # takes an exclusive lock and every dashboard read blocks behind the refresh.
    await pool().execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_job_stats")


async def get_stats() -> dict[str, Any]:
    rows = await pool().fetch(
        """
        SELECT job_type, status, sum(job_count)::bigint AS job_count,
               max(p95_duration_ms)::bigint AS p95_duration_ms,
               sum(total_attempts)::bigint  AS total_attempts
        FROM mv_job_stats
        GROUP BY job_type, status
        ORDER BY job_type, status
        """
    )
    totals = await pool().fetchrow(
        "SELECT coalesce(sum(job_count), 0)::bigint AS total FROM mv_job_stats"
    )
    return {"by_type": [dict(r) for r in rows], "total": totals["total"]}


# ---------------------------------------------------------------------- events --
async def _append_event(conn, job_id, event_type: str, data: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO job_events (job_id, event_type, data) VALUES ($1, $2, $3::jsonb)",
        job_id,
        event_type,
        json.dumps(data),
    )


async def events_since(seq: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """
        SELECT e.seq, e.job_id, e.event_type, e.data, e.created_at, j.status, jt.name AS job_type
        FROM job_events e
        JOIN jobs j       ON j.id = e.job_id
        JOIN job_types jt ON jt.id = j.job_type_id
        WHERE e.seq > $1 ORDER BY e.seq LIMIT $2
        """,
        seq,
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("data"), str):
            d["data"] = json.loads(d["data"])
        out.append(d)
    return out


async def latest_seq() -> int:
    return await pool().fetchval("SELECT coalesce(max(seq), 0) FROM job_events")
