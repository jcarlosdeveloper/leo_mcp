#!/usr/bin/env python3
"""
Tests for background job execution (long-running skill early-return path).

Covers:
- run_job records success content + resolved UUID.
- run_job records error on timeout/exception and always releases the slot.
- acquire_inflight is atomic: second acquire fails while first holds the slot.
- registry round-trip: register_get / unknown_job.
"""

import asyncio
import time

import pytest

from leo_chat import jobs


@pytest.mark.asyncio
async def test_acquire_inflight_atomic():
    """A held slot rejects a second acquire; release restores availability."""
    assert await jobs.acquire_inflight() is True, "first acquire should succeed"
    assert await jobs.acquire_inflight() is False, "second acquire must fail (busy)"
    await jobs.release_inflight()
    assert await jobs.acquire_inflight() is True, "slot must be free after release"
    await jobs.release_inflight()


@pytest.mark.asyncio
async def test_run_job_success_records_content_and_uuid():
    """run_job stores the (content, uuid) tuple from the flow return."""
    timestamp = int(time.time())

    async def fake_flow(**kwargs):
        await asyncio.sleep(0.01)
        return f"plan result ts={timestamp}", "abc12345"

    job_id = jobs._new_job_id()
    task = asyncio.create_task(
        jobs.run_job(
            job_id,
            execute_fn=fake_flow,
            execute_kwargs={},
        )
    )
    jobs.register_job(job_id, task, "senior_planner", conversation_uuid=None)
    await task

    entry = jobs.get_job(job_id)
    assert entry is not None, "job must remain registered after completion"
    assert entry["content"] == f"plan result ts={timestamp}"
    assert entry["uuid"] == "abc12345"
    assert entry["error"] is None
    jobs.unregister_job(job_id)


@pytest.mark.asyncio
async def test_run_job_timeout_marks_error_and_releases():
    """A wedged generation is bounded and the slot is freed on timeout."""
    async def slow_flow(**kwargs):
        await asyncio.sleep(30)  # >> patched timeout

    # Patch the timeout to a small value for a fast test.
    original = jobs._JOB_TIMEOUT_SEC
    jobs._JOB_TIMEOUT_SEC = 0.05
    try:
        # Ensure a free slot first.
        job_id = jobs._new_job_id()
        task = asyncio.create_task(
            jobs.run_job(job_id, execute_fn=slow_flow, execute_kwargs={})
        )
        jobs.register_job(job_id, task, "senior_planner", conversation_uuid=None)
        await task

        entry = jobs.get_job(job_id)
        assert entry["content"] is None
        assert entry["error"] == "generation exceeded 0.05s"

        # The slot must be free again.
        assert await jobs.acquire_inflight() is True, "slot must be released on timeout"
        await jobs.release_inflight()
    finally:
        jobs._JOB_TIMEOUT_SEC = original
    jobs.unregister_job(job_id)


@pytest.mark.asyncio
async def test_run_job_exception_marks_error_and_releases():
    """A flow exception is recorded and the slot released."""

    async def boom(**kwargs):
        raise RuntimeError("brave wedged")

    job_id = jobs._new_job_id()
    task = asyncio.create_task(
        jobs.run_job(job_id, execute_fn=boom, execute_kwargs={})
    )
    jobs.register_job(job_id, task, "senior_planner", conversation_uuid=None)
    await task

    entry = jobs.get_job(job_id)
    assert entry["content"] is None
    assert entry["error"] == "brave wedged"

    assert await jobs.acquire_inflight() is True, "slot must be released on error"
    await jobs.release_inflight()
    jobs.unregister_job(job_id)


def test_get_job_unknown_returns_none():
    """Unknown job ids return None (mapped to an error by the poll tool)."""
    assert jobs.get_job("deadbeef") is None


def test_new_job_id_unique_shape():
    """Job ids are 8 hex chars and distinct."""
    ids = {jobs._new_job_id() for _ in range(100)}
    assert len(ids) == 100, "job ids must be unique"
    for jid in ids:
        assert len(jid) == 8 and all(c in "0123456789abcdef" for c in jid)
