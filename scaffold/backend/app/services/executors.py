"""Job executors — the 'external API integration' seam.

Each job type maps to a coroutine. The default executor simulates a flaky third-party
dependency so failure handling is demonstrable without depending on a real service
being up; swap in an httpx call and nothing else in the system changes.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

Executor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class DependencyError(RuntimeError):
    """Retryable failure from a downstream dependency."""


class PermanentError(RuntimeError):
    """Non-retryable: the request itself is bad, so retrying cannot help."""


async def simulated_external_call(job: dict[str, Any]) -> dict[str, Any]:
    """Stands in for a rate-limited, occasionally-failing third-party API.

    Failure rate is env-configurable so the demo can be made deterministic
    (set DEPENDENCY_FAILURE_RATE=0) or hostile (set it to 0.8).
    """
    await asyncio.sleep(settings.dependency_latency_ms / 1000)

    payload = job.get("payload") or {}
    if payload.get("force_permanent_failure"):
        raise PermanentError("payload rejected by dependency: unsupported format")

    if random.random() < settings.dependency_failure_rate:
        raise DependencyError("dependency returned 503 Service Unavailable")

    return {
        "ok": True,
        "job_type": job["job_type"],
        "attempt": job["attempts"],
        "output_ref": f"s3://results/{job['id']}.json",
    }


_REGISTRY: dict[str, Executor] = {
    "image.transform": simulated_external_call,
    "report.generate": simulated_external_call,
    "webhook.deliver": simulated_external_call,
}


def get_executor(job_type: str) -> Executor:
    executor = _REGISTRY.get(job_type)
    if executor is None:
        raise PermanentError(f"no executor registered for job type {job_type!r}")
    return executor
