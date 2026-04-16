"""Unit tests for jobs.py — offline, no FastAPI."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import jobs
from jobs import JobStatus


@pytest.fixture(autouse=True)
def _reset_store():
    jobs._JOBS.clear()
    # Reset the lock so it re-attaches to the event loop created by this test
    jobs._LOCK = asyncio.Lock()
    yield
    jobs._JOBS.clear()


def _run(coro):
    return asyncio.run(coro)


def test_create_returns_unique_queued_job():
    a = _run(jobs.create())
    b = _run(jobs.create())
    assert a.id != b.id
    assert a.status == JobStatus.QUEUED
    assert a.created_at is not None
    assert a.started_at is None and a.finished_at is None
    assert a.result is None and a.error is None


def test_set_running_transitions_status_and_started_at():
    j = _run(jobs.create())
    _run(jobs.set_running(j.id))
    got = _run(jobs.get(j.id))
    assert got.status == JobStatus.RUNNING
    assert got.started_at is not None
    assert got.finished_at is None


def test_set_done_stores_result_and_finished_at():
    j = _run(jobs.create())
    _run(jobs.set_running(j.id))
    sentinel = {"stub": "result"}
    _run(jobs.set_done(j.id, sentinel))
    got = _run(jobs.get(j.id))
    assert got.status == JobStatus.DONE
    assert got.result == sentinel
    assert got.error is None
    assert got.finished_at is not None


def test_set_failed_stores_error_and_finished_at():
    j = _run(jobs.create())
    _run(jobs.set_running(j.id))
    _run(jobs.set_failed(j.id, "kaboom"))
    got = _run(jobs.get(j.id))
    assert got.status == JobStatus.FAILED
    assert got.error == "kaboom"
    assert got.result is None


def test_get_unknown_returns_none():
    assert _run(jobs.get("does-not-exist")) is None


def test_finished_job_expires_after_ttl():
    j = _run(jobs.create())
    _run(jobs.set_done(j.id, {"x": 1}))
    # Simulate old finished timestamp
    jobs._JOBS[j.id].finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
    assert _run(jobs.get(j.id)) is None
    # And is pruned from the dict after the get
    assert j.id not in jobs._JOBS


def test_unfinished_job_does_not_expire():
    """Running jobs older than TTL should still be returned (they haven't finished yet)."""
    j = _run(jobs.create())
    _run(jobs.set_running(j.id))
    jobs._JOBS[j.id].started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    got = _run(jobs.get(j.id))
    assert got is not None
    assert got.status == JobStatus.RUNNING


def test_purge_expired_returns_count():
    j1 = _run(jobs.create())
    j2 = _run(jobs.create())
    _run(jobs.set_done(j1.id, {}))
    _run(jobs.set_done(j2.id, {}))
    jobs._JOBS[j1.id].finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
    # j2 remains recent
    removed = _run(jobs.purge_expired())
    assert removed == 1
    assert j1.id not in jobs._JOBS
    assert j2.id in jobs._JOBS
