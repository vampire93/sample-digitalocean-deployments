from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.errors import Conflict, NotFound
from app.api.schemas import (
    JobCreate, JobDetailOut, JobListOut, JobOut, JobTypeOut, StatsOut,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.repositories import jobs as repo
from app.services.events import broker

log = get_logger(__name__)
router = APIRouter(prefix="/api")


def _json_default(o):
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    return str(o)


def _encode_cursor(job: dict) -> str:
    raw = json.dumps({"c": job["created_at"].isoformat(), "i": str(job["id"])})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        d = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(d["c"]), uuid.UUID(d["i"])
    except Exception as exc:
        raise Conflict("invalid cursor") from exc


# ------------------------------------------------------------------- job types --
@router.get("/job-types", response_model=list[JobTypeOut])
async def get_job_types():
    return await repo.list_job_types()


# ------------------------------------------------------------------------ jobs --
@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    body: JobCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """202, not 200: the work is accepted, not done. The caller polls or subscribes."""
    job_type = await repo.get_job_type(body.job_type)
    if job_type is None:
        raise NotFound(f"job type {body.job_type!r}")

    key = idempotency_key or body.idempotency_key
    job, created = await repo.create_job(
        job_type_id=job_type["id"],
        payload=body.payload,
        max_attempts=job_type["max_attempts"],
        priority=body.priority,
        idempotency_key=key,
    )
    if not created:
        # Same key replayed: hand back the original job rather than duplicating work.
        response.status_code = status.HTTP_200_OK
        log.info("job.idempotent_replay", job_id=str(job["id"]), idempotency_key=key)
    else:
        log.info("job.submitted", job_id=str(job["id"]), job_type=body.job_type)
    return job


@router.get("/jobs", response_model=JobListOut)
async def list_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    job_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    c_created, c_id = (None, None)
    if cursor:
        c_created, c_id = _decode_cursor(cursor)

    # Fetch one extra to determine whether another page exists.
    items = await repo.list_jobs(
        status=status_filter, job_type=job_type, limit=limit + 1,
        cursor_created_at=c_created, cursor_id=c_id,
    )
    next_cursor = _encode_cursor(items[limit - 1]) if len(items) > limit else None
    return {"items": items[:limit], "next_cursor": next_cursor}


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
async def get_job(job_id: uuid.UUID):
    job = await repo.get_job(job_id)
    if job is None:
        raise NotFound("job")
    job["attempts_log"] = await repo.get_attempts(job_id)
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def retry_job(job_id: uuid.UUID):
    job = await repo.retry_job(job_id)
    if job is None:
        existing = await repo.get_job(job_id)
        if existing is None:
            raise NotFound("job")
        raise Conflict(
            f"job is {existing['status']}; only dead_letter jobs can be retried",
            [{"field": "status", "message": existing["status"]}],
        )
    log.info("job.retry_requested", job_id=str(job_id))
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: uuid.UUID):
    job = await repo.cancel_job(job_id)
    if job is None:
        existing = await repo.get_job(job_id)
        if existing is None:
            raise NotFound("job")
        raise Conflict(
            f"job is {existing['status']}; only queued jobs can be cancelled",
            [{"field": "status", "message": existing["status"]}],
        )
    return job


# ----------------------------------------------------------------------- stats --
@router.get("/stats", response_model=StatsOut)
async def stats():
    data = await repo.get_stats()
    return {
        **data,
        "as_of": datetime.now(timezone.utc),
        "stale_after_seconds": settings.matview_refresh_interval_s,
    }


# ------------------------------------------------------------------------- SSE --
@router.get("/events")
async def events(
    request: Request,
    since: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Server-Sent Events over the append-only job_events log.

    Resume semantics: the browser resends Last-Event-ID automatically on reconnect, so
    a dropped connection replays exactly the events it missed. Absent both that header
    and ?since, we start from 'now' rather than replaying all history.

    Note we re-read events from the log by sequence number rather than trusting the
    NOTIFY payload. NOTIFY is only a wakeup; the log is the ordering authority. That
    keeps the stream gap-free even if a notification is dropped.
    """
    if last_event_id is not None and last_event_id.isdigit():
        cursor = int(last_event_id)
    elif since is not None:
        cursor = since
    else:
        cursor = await repo.latest_seq()

    queue = broker.subscribe()

    async def stream():
        nonlocal cursor
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break

                batch = await repo.events_since(cursor)
                for ev in batch:
                    cursor = ev["seq"]
                    payload = json.dumps(
                        {
                            "seq": ev["seq"],
                            "job_id": str(ev["job_id"]),
                            "job_type": ev["job_type"],
                            "event_type": ev["event_type"],
                            "status": ev["status"],
                            "data": ev["data"],
                            "created_at": ev["created_at"],
                        },
                        default=_json_default,
                    )
                    yield f"id: {ev['seq']}\nevent: {ev['event_type']}\ndata: {payload}\n\n"

                try:
                    # Wake on NOTIFY, or fall through to emit a keepalive comment.
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Comment frame: keeps proxies and load balancers from reaping an
                    # idle connection, and costs one line.
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this, buffering proxies hold events back and 'live' looks broken.
            "X-Accel-Buffering": "no",
        },
    )
