"""Pure domain: status machine and retry policy.

Deliberately free of I/O so it is unit-testable with zero fixtures. This is the layer
that encodes the *rules*; the repository layer only persists their outcome.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"          # retryable, awaiting next attempt
    DEAD_LETTER = "dead_letter"  # terminal, attempts exhausted
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED}
)

# Explicit allow-list. An invalid transition is a bug, not a silent no-op.
_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTER,
            JobStatus.QUEUED,  # lease expired -> reaped back to the queue
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.DEAD_LETTER: frozenset({JobStatus.QUEUED}),  # operator-initiated manual retry
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransition(ValueError):
    def __init__(self, src: JobStatus, dst: JobStatus) -> None:
        super().__init__(f"cannot transition {src} -> {dst}")
        self.src, self.dst = src, dst


def can_transition(src: JobStatus, dst: JobStatus) -> bool:
    return dst in _ALLOWED[src]


def assert_transition(src: JobStatus, dst: JobStatus) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATUSES


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter.

    Full jitter (rather than fixed backoff) matters because a downstream outage fails
    every in-flight job at once; without jitter they all retry in lockstep and stampede
    the dependency the moment it recovers.
    """

    max_attempts: int = 4
    base_seconds: float = 2.0
    max_seconds: float = 60.0

    def should_retry(self, attempt: int) -> bool:
        """``attempt`` is the number of attempts already completed."""
        return attempt < self.max_attempts

    def next_delay_seconds(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Delay before attempt number ``attempt + 1``."""
        if attempt < 1:
            attempt = 1
        exponential = min(self.base_seconds * (2 ** (attempt - 1)), self.max_seconds)
        r = rng or random
        return r.uniform(0.0, exponential)

    def outcome_after_failure(self, attempt: int) -> JobStatus:
        return JobStatus.FAILED if self.should_retry(attempt) else JobStatus.DEAD_LETTER
