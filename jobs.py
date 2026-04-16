"""
In-memory job store for async /investigate runs.

Single-replica RCA deployment — no Redis, no persistence. Each job tracks
status transitions (queued → running → done/failed) and holds the final
result or error. Finished jobs are garbage-collected after a TTL.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any | None = None
    error: str | None = None


_JOBS: dict[str, Job] = {}
_TTL = timedelta(hours=1)
_LOCK = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create() -> Job:
    """Create a fresh queued job and store it."""
    async with _LOCK:
        await _purge_expired_locked()
        job_id = uuid.uuid4().hex
        job = Job(id=job_id, status=JobStatus.QUEUED, created_at=_now())
        _JOBS[job_id] = job
        return job


async def set_running(job_id: str) -> None:
    async with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = JobStatus.RUNNING
        job.started_at = _now()


async def set_done(job_id: str, result: Any) -> None:
    async with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = JobStatus.DONE
        job.result = result
        job.finished_at = _now()


async def set_failed(job_id: str, error: str) -> None:
    async with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED
        job.error = error
        job.finished_at = _now()


async def get(job_id: str) -> Job | None:
    """Return the job, or None if missing or expired."""
    async with _LOCK:
        await _purge_expired_locked()
        return _JOBS.get(job_id)


async def purge_expired() -> int:
    """Public entry — returns number of jobs removed."""
    async with _LOCK:
        return await _purge_expired_locked()


async def _purge_expired_locked() -> int:
    """Caller must hold _LOCK."""
    cutoff = _now() - _TTL
    to_remove = [
        jid for jid, j in _JOBS.items()
        if j.finished_at is not None and j.finished_at < cutoff
    ]
    for jid in to_remove:
        del _JOBS[jid]
    return len(to_remove)
