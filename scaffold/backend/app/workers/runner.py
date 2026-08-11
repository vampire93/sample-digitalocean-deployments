"""Worker: claim -> execute -> settle, plus heartbeat and reaper loops.

Runs in-process with the API here (one deployable, simpler to demo) but holds no
in-memory state, so lifting it into a separate App Platform `worker` component is a
config change, not a rewrite.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid

from app.core.config import settings
from app.core.logging import get_logger
from app.domain.job import JobStatus, RetryPolicy
from app.repositories import jobs as repo
from app.services.executors import DependencyError, PermanentError, get_executor

log = get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(self) -> None:
        self.policy = RetryPolicy(
            max_attempts=settings.max_attempts,
            base_seconds=settings.backoff_base_s,
            max_seconds=settings.backoff_max_s,
        )
        self._inflight: set[uuid.UUID] = set()
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._sem = asyncio.Semaphore(settings.worker_concurrency)

    # ------------------------------------------------------------- lifecycle --
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._claim_loop(), name="claim"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._reaper_loop(), name="reaper"),
            asyncio.create_task(self._refresh_loop(), name="matview-refresh"),
        ]
        log.info("worker.started", worker_id=WORKER_ID,
                 concurrency=settings.worker_concurrency)

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("worker.stopped", worker_id=WORKER_ID)

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep so shutdown isn't blocked by a poll interval."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------- claim loop --
    async def _claim_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                free = settings.worker_concurrency - len(self._inflight)
                if free <= 0:
                    await self._sleep(0.2)
                    continue

                claimed = await repo.claim_jobs(
                    worker_id=WORKER_ID,
                    batch=min(settings.worker_batch_size, free),
                    lease_seconds=settings.lease_seconds,
                )
                if not claimed:
                    await self._sleep(settings.worker_poll_interval_s)
                    continue

                for job in claimed:
                    self._inflight.add(job["id"])
                    asyncio.create_task(self._run(job))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient DB error must not kill the loop.
                log.exception("worker.claim_loop_error")
                await self._sleep(settings.worker_poll_interval_s)

    # --------------------------------------------------------------- execute --
    async def _run(self, job: dict) -> None:
        job_id, attempt = job["id"], job["attempts"]
        started = time.monotonic()
        bound = log.bind(job_id=str(job_id), job_type=job["job_type"], attempt=attempt)

        async with self._sem:
            try:
                executor = get_executor(job["job_type"])
                result = await executor(job)
                await repo.complete_job(
                    job_id=job_id, worker_id=WORKER_ID, attempt_no=attempt,
                    result=result, duration_ms=int((time.monotonic() - started) * 1000),
                )
                bound.info("job.succeeded")

            except PermanentError as exc:
                # Retrying a bad request just burns attempts — go straight to terminal.
                await repo.fail_job(
                    job_id=job_id, worker_id=WORKER_ID, attempt_no=attempt,
                    error=f"permanent: {exc}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    next_status=JobStatus.DEAD_LETTER, delay_seconds=0,
                )
                bound.warning("job.dead_lettered", reason="permanent_error", error=str(exc))

            except (DependencyError, Exception) as exc:  # noqa: B014 - explicit intent
                next_status = self.policy.outcome_after_failure(attempt)
                delay = (
                    self.policy.next_delay_seconds(attempt)
                    if next_status == JobStatus.FAILED
                    else 0.0
                )
                await repo.fail_job(
                    job_id=job_id, worker_id=WORKER_ID, attempt_no=attempt,
                    error=str(exc),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    next_status=next_status, delay_seconds=delay,
                )
                bound.warning("job.failed", next_status=str(next_status),
                              retry_in_s=round(delay, 2), error=str(exc))
            finally:
                self._inflight.discard(job_id)

    # ----------------------------------------------------------- heartbeat --
    async def _heartbeat_loop(self) -> None:
        """Extend leases for still-running jobs.

        Without this, any job slower than lease_seconds gets reaped out from under a
        perfectly healthy worker and runs twice.
        """
        while not self._stopping.is_set():
            try:
                if self._inflight:
                    await repo.heartbeat(
                        worker_id=WORKER_ID,
                        job_ids=list(self._inflight),
                        lease_seconds=settings.lease_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker.heartbeat_error")
            await self._sleep(settings.heartbeat_interval_s)

    # --------------------------------------------------------------- reaper --
    async def _reaper_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                n = await repo.reap_expired_leases()
                if n:
                    log.warning("worker.reaped_expired_leases", count=n)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker.reaper_error")
            await self._sleep(settings.reaper_interval_s)

    # ------------------------------------------------------- read model refresh --
    async def _refresh_loop(self) -> None:
        while not self._stopping.is_set():
            await self._sleep(settings.matview_refresh_interval_s)
            try:
                await repo.refresh_stats()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker.matview_refresh_error")


worker = Worker()
