"""Request/response contracts. Validation lives here, not in handlers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.job import JobStatus


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typo in a field name is an error, not a silent drop

    job_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("payload")
    @classmethod
    def payload_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Cheap guard against someone posting a megabyte of JSON into a queue row.
        if len(str(v)) > 16_000:
            raise ValueError("payload too large (max ~16KB)")
        return v


class AttemptOut(BaseModel):
    attempt_no: int
    worker_id: str
    status: str
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None


class JobOut(BaseModel):
    id: uuid.UUID
    job_type: str
    status: JobStatus
    priority: int
    payload: dict[str, Any]
    idempotency_key: str | None = None
    attempts: int
    max_attempts: int
    run_after: datetime
    last_error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobDetailOut(JobOut):
    attempts_log: list[AttemptOut] = Field(default_factory=list)


class JobListOut(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = None


class JobTypeOut(BaseModel):
    id: int
    name: str
    description: str
    max_attempts: int


class StatsRow(BaseModel):
    job_type: str
    status: str
    job_count: int
    p95_duration_ms: int
    total_attempts: int


class StatsOut(BaseModel):
    by_type: list[StatsRow]
    total: int
    # Surfaced deliberately: the read model is a materialised view refreshed on a
    # cadence, so it is allowed to lag. Staleness you can see is a trade-off;
    # staleness you can't see is a bug.
    as_of: datetime
    stale_after_seconds: float
