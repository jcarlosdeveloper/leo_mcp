"""Background job registry for long-running Leo generations.

Long-running (stream-detected, non-patch) skill requests return a "working"
job handle to the MCP caller immediately and run the actual Leo round-trip in
the background. This module owns the in-memory registry, the single-slot
in-flight semaphore (Leo/Brave share one profile, so only one generation runs
at a time), and the runner that executes the flow and records its terminal
state.

SQLite is NOT the source of truth here: the runner captures the final
``(content, resolved_uuid)`` tuple from the flow and stores it on the registry
entry, so the poll tool only ever reads the in-memory entry.
"""

import asyncio
import time
import uuid as _uuid
from typing import Any, Dict, Optional

from leo_chat.helpers import _log_json

# Hard safety ceiling for a background generation. This is NOT a tidiness TTL;
# it exists purely for semaphore liveness — a wedged Leo/Brave must not hold the
# single in-flight slot forever and turn every future request into "busy".
_JOB_TIMEOUT_SEC = 900

# Single generation at a time (one Brave profile, one Leo conversation).
# An asyncio.Lock is used instead of a Semaphore because acquiring an unlocked
# Lock does not yield to the event loop, so the check-then-acquire sequence is
# atomic within single-threaded asyncio (no other task can interleave).
_inflight = asyncio.Lock()

# job_id -> {uuid, content, error, task, skill, started_at}
_jobs: Dict[str, Dict[str, Any]] = {}


def _new_job_id() -> str:
    """Return a short, unique job id (first 8 chars of a uuid4)."""
    return _uuid.uuid4().hex[:8]


def register_job(
    job_id: str,
    task: asyncio.Task,
    skill: str,
    conversation_uuid: Optional[str],
    prompt: str = "",
) -> None:
    """Record a running job with a strong reference to its task."""
    _jobs[job_id] = {
        "uuid": conversation_uuid,
        "content": None,
        "error": None,
        "task": task,  # strong ref: prevent GC of a running coroutine
        "skill": skill,
        "prompt": prompt,
        "started_at": time.time(),
    }


def get_job(job_id: str, *, prune_done: bool = True) -> Optional[Dict[str, Any]]:
    """Return the registry entry for a job, or None if unknown.

    ``prune_done=True`` lazily drops finished jobs older than a small window so
    the registry cannot grow unboundedly on a long-lived server.
    """
    entry = _jobs.get(job_id)
    if entry is None:
        return None
    return entry


def unregister_job(job_id: str) -> None:
    """Drop a job entry. Safe to call on unknown ids."""
    _jobs.pop(job_id, None)


async def acquire_inflight() -> bool:
    """Atomically try to take the single in-flight slot.

    Returns True if the slot was acquired (the caller MUST release it), False if
    a generation is already running. Acquiring an unlocked ``asyncio.Lock`` does
    not yield to the event loop, so the check-and-take is atomic and executes on
    the caller's own coroutine — never inside a freshly spawned task (which
    would turn a busy response into a hidden queue).
    """
    if _inflight.locked():
        return False
    await _inflight.acquire()
    return True


async def release_inflight() -> None:
    """Release the single in-flight slot. Idempotent."""
    try:
        _inflight.release()
    except RuntimeError:
        # Released while not locked; ignore.
        pass


async def run_job(
    job_id: str,
    *,
    execute_fn,
    execute_kwargs: Dict[str, Any],
) -> None:
    """Run a Leo flow in the background and record its terminal state.

    The coroutine closes over ONLY plain values (``execute_fn`` + args); it never
    touches the FastMCP request ``Context``, so it survives the originating
    request scope being torn down when the caller times out and disconnects.

    Args:
        job_id: Registry key for this job.
        execute_fn: The flow coroutine (e.g.
            ``execute_leo_flow_with_robust_patches``).
        execute_kwargs: Keyword args forwarded to ``execute_fn``.

    The semaphore is ALWAYS released, including on timeout/error/cancel, so a
    single wedged generation cannot brick the tool.
    """
    try:
        result = await asyncio.wait_for(
            execute_fn(**execute_kwargs),
            timeout=_JOB_TIMEOUT_SEC,
        )
        # execute_fn returns (response_text, resolved_uuid).
        content, resolved_uuid = result[0], result[1]
        # Re-resolve the entry at write-time (not cached up front) so this is
        # robust to register_job() being called immediately after create_task().
        entry = get_job(job_id, prune_done=False)
        if entry is not None:
            entry["uuid"] = resolved_uuid or entry.get("uuid")
            entry["content"] = content
        _log_json(
            "INFO", "leo_job_done", job_id=job_id,
            uuid=(resolved_uuid or "")[:8],
            content_len=len(content or ""),
        )
    except asyncio.TimeoutError:
        # Bound a wedged generation so the semaphore slot is freed. Hard-cancel
        # is acceptable: the flow uses read-only coherent SQLite reads
        # (immutable=1), so no half-written entry is left behind; the only cost
        # is a cosmetic dangling Brave conversation (same as the prior 600s
        # synchronous abandon behavior).
        entry = get_job(job_id, prune_done=False)
        if entry is not None:
            entry["error"] = f"generation exceeded {_JOB_TIMEOUT_SEC}s"
        _log_json("ERROR", "leo_job_timeout", job_id=job_id, timeout=_JOB_TIMEOUT_SEC)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - record any flow failure
        entry = get_job(job_id, prune_done=False)
        if entry is not None:
            entry["error"] = str(exc)
        _log_json("ERROR", "leo_job_error", job_id=job_id, error=str(exc))
    finally:
        await release_inflight()
