#!/usr/bin/env python3
"""Leo Model Context Protocol (MCP) Server.

Exposes Leo (Brave's in-browser AI) and Brave AI Search as MCP tools. Manages a
Chrome DevTools Protocol (CDP) connection to a running Brave instance, injects
prompts silently into Leo, and reads responses from Brave's SQLite database.

Tools:
    ask_leo_skill      - code/planning/analysis via Leo, with file injection and
                         optional single-file write-to-disk (feature-flagged).
    ask_leo_result     - poll a background generation started by ask_leo_skill.
    ask_leo_quick      - fast Brave AI web search.
    ask_leo_extensive  - deep Brave AI research.
    get_conversation_history - recent interaction telemetry.
"""

import asyncio
import errno
import atexit
import datetime as _dt
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from typing import List, Optional, Dict, Any

from mcp.server.fastmcp import FastMCP

# ── Single-Instance Guard ────────────────────────────────────────────────────
# Enforce a single running server via POSIX file locking on a persistent FD.
LOCK_FILE = os.path.expanduser("~/.leo_mcp_server.lock")
_LOCK_FD = None


def _acquire_lock():
    """Acquire an exclusive lock, reporting the owning PID on conflict.

    Uses flock as the source of truth so a SIGKILL'd owner releases the lock
    automatically. The PID written to the file is informational only.
    """
    global _LOCK_FD
    # O_CREAT without O_TRUNC so a live owner's PID is not wiped.
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError) as e:
        if e.errno in (errno.EAGAIN, errno.EACCES):
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                existing_pid = os.read(fd, 32).decode(errors="ignore").strip()
            except Exception:
                existing_pid = "unknown"
            print(
                f"❌ Another instance of leo_mcp_server is already running "
                f"(pid={existing_pid}).",
                file=sys.stderr,
            )
            print(f"   Lock file: {LOCK_FILE}", file=sys.stderr)
            os.close(fd)
            sys.exit(1)
        raise

    # Lock held: write our PID with the lock held.
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
    _LOCK_FD = fd


def _release_lock():
    """Release the lock without deleting the file to avoid the inode race.

    The file persists empty; the lock is released when the FD is closed.
    """
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
            os.close(_LOCK_FD)
        except Exception:
            pass
        _LOCK_FD = None


atexit.register(_release_lock)

# ── Environment & Configuration ──────────────────────────────────────────────
BRAVE_PROFILE: str = os.environ.get("BRAVE_PROFILE", "Default")
HERMES_DIR: str = os.path.expanduser("~/.hermes")
BRAVE_BASE_PATH: str = "~/Library/Application Support/BraveSoftware/Brave-Browser"
BRAVE_DB_PATH: str = os.path.expanduser(f"{BRAVE_BASE_PATH}/{BRAVE_PROFILE}/AIChat")

# Pipeline integrations (CDP engine components).
from leo_chat.execution import (
    execute_leo_flow_with_robust_patches,
    _INCOMPLETE_RESPONSE_SENTINEL,
)
from leo_chat.skills.skill_factory import SkillFactory
from leo_chat.context.prompt_builder import assemble_context
from leo_chat.helpers import (
    _cache_get,
    _cache_set,
    _cache_key,
    _log_json,
    _conv_log,
    _conv_get_recent,
    _metrics_log,
    _detect_file_required,
    _detect_copy_block,
    _detect_plan_block,
    _build_file_context,
)

# Brave AI Search imports.
from brave_search.factory import SearchFactory
from playwright.async_api import async_playwright

# Write-to-disk feature (per-request root resolution; global policy only).
from leo_chat.config import resolve_write_mode, WriteMode
from leo_chat.patch_writer import apply_single_patch
from leo_chat.skills.skill_support import skill_supports_patch
from leo_chat import jobs as leo_jobs

# Instantiate FastMCP engine context.
mcp = FastMCP("leo-bridge", host="127.0.0.1", port=9223)

# Resolve the disk-write policy once at startup, injected via MCP setup env.
WRITE_MODE = resolve_write_mode()

# ── Browser Pool ─────────────────────────────────────────────────────────────
# A single headless Chromium instance is shared across web-search tools; each
# request gets a fresh context/page while the browser stays alive.
_playwright_instance = None
_browser_instance = None


async def _get_shared_browser():
    """Return a shared headless Chromium browser, launching it on first use.

    Relaunches transparently if a previous instance was disconnected.
    """
    global _playwright_instance, _browser_instance

    if _browser_instance is None:
        _playwright_instance = await async_playwright().start()
        _browser_instance = await _playwright_instance.chromium.launch(headless=True)
    elif not _browser_instance.is_connected():
        _browser_instance = await _playwright_instance.chromium.launch(headless=True)

    return _browser_instance


async def _close_shared_browser():
    """Close the shared browser and stop the Playwright driver.

    Safe to call multiple times; resets the module-level singletons.
    """
    global _playwright_instance, _browser_instance
    if _browser_instance:
        await _browser_instance.close()
        _browser_instance = None
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None


# ── Auto-Bootstrapper ────────────────────────────────────────────────────────

def bootstrap_brave_cdp(port: int = 9222, profile: str = "Default") -> bool:
    """Ensure Brave is running with its CDP endpoint ready.

    Verifies the CDP TCP port, restarting Brave with remote-debugging flags if
    needed, then polls until both the port is open and the CDP HTTP endpoint
    responds, avoiding the port-bound-but-DevTools-not-ready race.
    """
    separator: str = "=" * 75
    print(separator, file=sys.stderr)
    print(" LEO MCP SYSTEM INIT - AUTO-BOOTSTRAPPER", file=sys.stderr)
    print(separator, file=sys.stderr)

    print(f"[BOOTSTRAP] Checking CDP Port {port}...", file=sys.stderr)

    # Return early if the CDP port is already serving.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        if s.connect_ex(('127.0.0.1', port)) == 0:
            print(f"   ✅ Brave is already running and listening on port {port}.", file=sys.stderr)
            print(separator, file=sys.stderr)
            return True

    print(f"   ⚠️ Port {port} is closed. Initiating graceful restart sequence...", file=sys.stderr)

    # Gracefully quit Brave via macOS AppleScript to release the profile lock.
    try:
        subprocess.run(
            ['osascript', '-e', 'quit app "Brave Browser"'],
            check=True,
            capture_output=True
        )
        print("   🛑 Sent native quit signal to Brave. Waiting for profile lock release...", file=sys.stderr)
        time.sleep(3)
    except subprocess.CalledProcessError:
        print("   ⚠️ Brave was not running, skipping quit command.", file=sys.stderr)

    # Relaunch Brave detached with remote debugging enabled.
    print("   🚀 Relaunching Brave with remote debugging flags...", file=sys.stderr)
    brave_path = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

    if not os.path.exists(brave_path):
        print(f"   ❌ CRITICAL: Brave binary not found at {brave_path}", file=sys.stderr)
        print(separator, file=sys.stderr)
        return False

    subprocess.Popen([
        brave_path,
        f"--remote-debugging-port={port}",
        f"--profile-directory={profile}"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    # Poll for both the TCP socket and the CDP HTTP endpoint to be ready.
    for attempt in range(1, 11):
        time.sleep(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=1
                    ) as resp:
                        if resp.status == 200:
                            print(f"   ✅ CDP endpoint ready on attempt {attempt}!", file=sys.stderr)
                            print(separator, file=sys.stderr)
                            return True
                except Exception:
                    pass  # Port open but CDP not ready yet; keep polling.
                print(f"   ⚠️ Port {port} open but CDP not ready (attempt {attempt})...", file=sys.stderr)

    print("   ❌ Timeout: Failed to bind to Brave CDP port after 10 seconds.", file=sys.stderr)
    print(separator, file=sys.stderr)
    return False


# ── Cache Key ────────────────────────────────────────────────────────────────

def _make_cache_key(skill_name: str, prompt: str, filepaths: List[str]) -> str:
    """Build a cache key from skill, prompt, and per-file modification times.

    Identical prompts over unchanged files map to the same key; any file edit
    changes the key so stale responses are not served after code changes.
    """
    import hashlib
    from pathlib import Path

    parts = [skill_name, prompt]
    for fp in sorted(filepaths or []):
        try:
            p = Path(fp).expanduser().resolve()
            # Nanosecond precision so two edits within the same wall-clock
            # second (likely under fast automated delegation) produce distinct
            # keys and a stale cached response is never served.
            mtime = str(p.stat().st_mtime_ns) if p.exists() else "MISSING"
        except Exception:
            mtime = "ERROR"
        parts.append(f"{fp}:{mtime}")

    combined = "::".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


# ── Streaming Auto-Detection ─────────────────────────────────────────────────

# Skills whose responses are long by nature and benefit from SQLite streaming.
_STREAMING_SKILLS = {"senior_planner", "code_refiner"}

# Prompt keywords that suggest a long, multi-part answer.
_STREAMING_KEYWORDS = [
    "explain", "refactor", "review", "plan", "design", "architect",
    "analyze", "document", "compare", "evaluate", "summarize",
    "optimize", "restructure", "migrate",
]

# Prompt keywords that suggest a short, direct answer where DOM polling suffices.
_SHORT_KEYWORDS = [
    "fix", "find", "where", "what", "how do i", "show me",
    "locate", "identify", "quick", "one line", "syntax",
]


def _auto_detect_stream(skill_name: str, prompt: str, filepaths: List[str]) -> bool:
    """Decide whether to use SQLite streaming for a request.

    Long-form skills, long-form prompt keywords, or heavy file context favor
    streaming; short lookups fall back to faster DOM polling.
    """
    prompt_lower = prompt.lower()

    # Long-form skills always stream.
    if skill_name in _STREAMING_SKILLS:
        return True

    # Long-form keywords stream unless a short-lookup keyword is also present.
    if any(kw in prompt_lower for kw in _STREAMING_KEYWORDS):
        if not any(sk in prompt_lower for sk in _SHORT_KEYWORDS):
            return True

    # Heavy file context tends to need streaming.
    if filepaths and len(filepaths) >= 3:
        return True

    return False


def _resolve_write_root(filepaths: List[str]) -> Optional[str]:
    """Resolve the directory under which disk writes are permitted for this request.

    Policy: any path is allowed except those blocked by the denylist in
    patch_writer._is_forbidden (credential stores, system dirs, bare $HOME, etc.).
    The root returned is the target's parent directory — containment is therefore
    trivially satisfied and the denylist in apply_single_patch is the only gate.

    Returns the parent directory as a string, or None for empty input or
    unresolvable paths.
    """
    from pathlib import Path

    if not filepaths:
        return None

    try:
        resolved = Path(filepaths[0]).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None

    parent = resolved.parent
    # Return the parent even if it doesn't exist yet (create actions need this).
    return str(parent)


# ── Core Production MCP Tools ────────────────────────────────────────────────

@mcp.tool()
async def ask_leo_skill(
    skill_name: str,
    prompt: str,
    filepaths: List[str] = [],
    stream: bool = False,
    conversation_uuid: str = "",
    structured_plan: bool = False,
) -> str:
    """Execute a Leo skill over files YOU MUST NOT READ FIRST.

    DO NOT read, open, cat, or view the target file before calling this tool.
    DO NOT paste file contents into `prompt`. This server reads and injects the
    file itself from `filepaths`. Reading it first duplicates it into your
    context and wastes the tokens this tool exists to save. You do NOT need the
    file contents to call this tool; the absolute path is sufficient. A prompt
    that appears to contain pasted source is rejected with status "error" before
    any round-trip.

    HARD REQUIREMENT (write-patch skills, e.g. code_refiner, code_editor):
    code_refiner (whole-file) and code_editor (str_replace edit) write to disk
    by default, so they REQUIRE exactly one absolute path in filepaths.
    Omitting filepaths, or passing more than one, is rejected with status
    "error" BEFORE any Leo round-trip. The non-writing skill senior_planner
    does not require filepaths — they are optional injected context. If in
    doubt, treat filepaths as MANDATORY for code_refiner and code_editor only.

    Two independent structured behaviors:
    - Patch (disk write): active for write-patch skills (code_refiner,
      code_editor) when the global write mode is not DISABLED. code_refiner
      modifies a whole file; code_editor applies one str_replace edit. A compact
      summary is returned instead of the file contents.
    - Plan (no disk write): senior_planner returns a compact JSON plan when
      structured_plan is True. Nothing is written to disk.

    senior_planner specifics (important for callers):
    - It is in the auto-streaming skill set, so it ALWAYS runs in the
      background: the first response is status "working" with a job_id, not the
      plan itself. Poll ask_leo_result(job_id) until status is "success".
    - The quality of its plan depends on file context. Passing filepaths grounds
      the plan in real files; with no filepaths the plan is often degraded: it
      returns an empty "steps" array and populates "needs_user_decision" with
      clarifying questions instead. Treat filepaths as strongly recommended (not
      merely optional) when you want an actionable plan.
    - conversation_uuid resume preserves prior context (including previously
      injected files), so a follow-up can proceed without re-passing filepaths.

    Args:
        skill_name: One of "code_refiner", "code_editor", "senior_planner".
        prompt: Task description. Do NOT embed file contents; use filepaths.
        filepaths: REQUIRED (exactly one absolute path) for code_refiner and
            code_editor write mode; optional injected context for all other
            skills ([] if none). For senior_planner, provide filepaths to get
            an actionable plan rather than a clarifying-question response.
        stream: Force SQLite streaming; auto-enabled for structured output.
        conversation_uuid: Resume an existing conversation and send only the new
            prompt as a follow-up (no history re-injected).
        structured_plan: For senior_planner, request the compact JSON plan
            instead of prose. Ignored for other skills.

    Returns:
        JSON string with a "status" field. Possible statuses:
        - "working": background generation started; contains job_id. Poll
          ask_leo_result(job_id). Returned for auto-streamed skills such as
          senior_planner (non-patch, no conversation_uuid).
        - "busy": another generation already in progress (single-slot
          constraint). Retry shortly or poll ask_leo_result for the in-flight job.
        - "success": terminal result. Patch mode: status, mode, file, action,
          risk, summary, bytes, skill_used, conversation_uuid. Normal/plan
          mode: status, skill_used, content, conversation_uuid,
          has_copy_block, has_plan_block, files_injected.
        - "still_working": dynamic timeout reached before completion; contains
          conversation_uuid to resume and retrieve the final answer.
        - "error": status, error, skill.
    """
    _log_json("INFO", "ask_leo_skill_start", skill=skill_name, prompt=prompt[:60], files=len(filepaths))

    _paste_reason = _looks_like_pasted_file(prompt, filepaths)
    if _paste_reason:
        _log_json(
            "WARN",
            "ask_leo_skill_rejected_paste",
            skill=skill_name,
            prompt_len=len(prompt),
            had_filepaths=bool(filepaths),
        )
        _metrics_log(
            skill=skill_name,
            prompt_len=len(prompt),
            duration_ms=0,
            status="rejected_paste",
        )
        return json.dumps(
            {
                "status": "error",
                "error": _paste_reason,
                "skill": skill_name,
                "fix": {
                    "prompt": "<short task description only>",
                    "filepaths": ["<absolute path to the file>"],
                },
            },
            ensure_ascii=False,
        )

    # Disk write is the narrowest condition: global flag + patch skill + files.
    should_write = (
        WRITE_MODE != WriteMode.DISABLED
        and skill_supports_patch(skill_name)
        and bool(filepaths)
    )

    # Structured output covers BOTH the file patch and the JSON plan. It only
    # controls the output contract, never disk side effects.
    structured = should_write or (skill_name == "senior_planner" and structured_plan)

    # Fase D: a patch-capable skill in a write mode with no filepaths is a
    # misuse — it would silently fall through to prose. Fail explicitly instead.
    if (
        WRITE_MODE != WriteMode.DISABLED
        and skill_supports_patch(skill_name)
        and not filepaths
    ):
        return json.dumps({
            "status": "error",
            "error": (
                "patch mode requires exactly one filepath; none provided. "
                "Pass the absolute path of the file to modify."
            ),
            "skill": skill_name,
        }, ensure_ascii=False)

    # Enforce the single-file-per-request contract for disk writes.
    if should_write and len(filepaths) != 1:
        return json.dumps({
            "status": "error",
            "error": "patch mode writes one file per request; pass exactly one path",
            "skill": skill_name,
            "files_provided": len(filepaths),
        }, ensure_ascii=False)

    # Resolve the write root up front so an unresolvable root is rejected
    # before spending a Leo round-trip on a request that will fail anyway.
    allowed_root: Optional[str] = None
    if should_write:
        allowed_root = _resolve_write_root(filepaths)
        if not allowed_root:
            return json.dumps({
                "status": "error",
                "error": (
                    "no valid write root: filepaths must not be empty for patch mode"
                ),
                "skill": skill_name,
            }, ensure_ascii=False)

    # Fase D + anti-TOCTOU preflight (write mode only). Verify the target is
    # readable BEFORE spending a Leo round-trip on a phantom file, and capture
    # its modification time so a concurrent edit during the round-trip aborts
    # the write instead of clobbering newer content. A non-existent target is
    # pre-created as a one-line stub: Leo needs real file context to answer,
    # otherwise it treats the injected instructions as suspicious input (see
    # plan v11.2, "New File Pre-Creation Fix").
    expected_mtime_ns: Optional[int] = None
    precreated = False
    stub_text: Optional[str] = None
    created_dirs: List[Any] = []
    target_path = None
    if should_write:
        from pathlib import Path as _Path
        from leo_chat.context.prompt_builder import MAX_FILE_SIZE

        target_path = _Path(filepaths[0]).expanduser().resolve(strict=False)

        if not target_path.exists():
            probe = target_path.parent
            while not probe.exists():
                created_dirs.append(probe)
                probe = probe.parent
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                stub_text = f"# {target_path.name}\n"
                target_path.write_text(stub_text, encoding="utf-8")
            except OSError as exc:
                return json.dumps({
                    "status": "error",
                    "error": f"could not pre-create target for new file: {exc}",
                    "skill": skill_name,
                }, ensure_ascii=False)
            precreated = True

        if not target_path.is_file():
            return json.dumps({
                "status": "error",
                "error": f"patch target is not a file: {target_path}",
                "skill": skill_name,
            }, ensure_ascii=False)
        try:
            stat = target_path.stat()
            if stat.st_size > MAX_FILE_SIZE:
                return json.dumps({
                    "status": "error",
                    "error": (
                        f"patch target exceeds the {MAX_FILE_SIZE:,}-byte read "
                        f"limit; refusing to send a truncated file to Leo"
                    ),
                    "skill": skill_name,
                }, ensure_ascii=False)
            with open(target_path, "rb") as _fh:
                _fh.read(1)
            expected_mtime_ns = stat.st_mtime_ns
        except OSError as exc:
            return json.dumps({
                "status": "error",
                "error": f"patch target is not readable: {exc}",
                "skill": skill_name,
            }, ensure_ascii=False)

    def _discard_stub() -> None:
        """Undo pre-creation on failure or dry_run; never touch a real write.

        Only removes the file if it still holds exactly the stub text we
        wrote (a successful Leo write, or a file that already existed, is
        left untouched), then removes any parent directories we created,
        deepest first, while they remain empty.
        """
        if not precreated or target_path is None:
            return
        try:
            if target_path.exists() and target_path.read_text(encoding="utf-8") == stub_text:
                target_path.unlink()
        except OSError:
            pass
        for created_dir in created_dirs:
            try:
                created_dir.rmdir()
            except OSError:
                pass

    # Cache only non-writing, non-resumed, non-structured requests. Writes have
    # disk side effects; structured output is cheap to regenerate and prone to
    # staleness, so both bypass the cache.
    cache_key = (
        _make_cache_key(skill_name, prompt, filepaths)
        if (not conversation_uuid and not should_write and not structured) else None
    )
    cached = _cache_get(cache_key) if cache_key else None
    if cached:
        _log_json("INFO", "ask_leo_skill_cache_hit", skill=skill_name)
        return cached


    # Enable streaming for long-form skills, complex prompts, or structured out.
    auto_stream = _auto_detect_stream(skill_name, prompt, filepaths)
    use_stream = stream or auto_stream or structured
    if (auto_stream or structured) and not stream:
        _log_json("INFO", "ask_leo_skill_auto_stream", skill=skill_name, structured=structured)

    # ── Background (non-blocking) path ────────────────────────────────────────
    # Stream-detected, NON-patch skills (senior_planner and any keyword/file-
    # triggered long request) run the Leo round-trip in the background and return
    # a "working" handle immediately. A synchronous wait here would exceed the
    # MCP client's ~60s read deadline (the Python client does NOT reset its
    # timeout on progress notifications), producing the "request cancelled" /
    # reconnect signature. code_refiner stays synchronous: its disk-write path
    # (TOCTOU preflight → stub → apply_single_patch) needs request atomicity.
    if use_stream and not should_write and not conversation_uuid:
        if not await leo_jobs.acquire_inflight():
            _log_json("INFO", "ask_leo_skill_busy", skill=skill_name)
            return json.dumps({
                "status": "busy",
                "skill_used": skill_name,
                "message": (
                    "Another Leo generation is already in progress (single-slot "
                    "constraint). Retry shortly, or query it via ask_leo_result."
                ),
            }, ensure_ascii=False)

        job_id = leo_jobs._new_job_id()
        # Snapshot plain values only — the background coroutine must NOT close
        # over the FastMCP request Context.
        execute_kwargs = {
            "skill_name": skill_name,
            "user_prompt": prompt,
            "filepaths": filepaths if filepaths else None,
            "model_override": None,
            "stream": use_stream,
            "conversation_uuid": None,
            "structured": structured,
            "allowed_root": None,
        }
        task = asyncio.create_task(
            leo_jobs.run_job(
                job_id,
                execute_fn=execute_leo_flow_with_robust_patches,
                execute_kwargs=execute_kwargs,
            )
        )
        leo_jobs.register_job(
            job_id, task, skill_name, conversation_uuid=None, prompt=prompt,
        )
        _log_json("INFO", "ask_leo_skill_backgrounded", skill=skill_name, job_id=job_id)
        return json.dumps({
            "status": "working",
            "skill_used": skill_name,
            "job_id": job_id,
            "conversation_uuid": "",
            "message": (
                "Leo is generating this response in the background. Poll "
                "ask_leo_result(job_id) to retrieve the final answer."
            ),
        }, ensure_ascii=False)

    try:
        # Structured mode injects the skill's contract and skips prose
        # continuation downstream; disk writing is decided here, not in the flow.
        execution_payload, resolved_uuid = await execute_leo_flow_with_robust_patches(
            skill_name=skill_name,
            user_prompt=prompt,
            filepaths=filepaths if filepaths else None,
            model_override=None,
            stream=use_stream,
            conversation_uuid=conversation_uuid or None,
            structured=structured,
            allowed_root=allowed_root if should_write else None,
        )

        # Disk-writing path: parse the single patch and apply it (or simulate in
        # dry-run). Return only a compact summary to keep tokens minimal.
        if should_write:
            if skill_name == "code_editor":
                from leo_chat.patch_writer import apply_edit_patch
                result = apply_edit_patch(
                    execution_payload,
                    allowed_root=allowed_root,
                    dry_run=(WRITE_MODE == WriteMode.DRY_RUN),
                    expected_mtime_ns=expected_mtime_ns,
                )
            else:
                result = apply_single_patch(
                    execution_payload,
                    allowed_root=allowed_root,
                    dry_run=(WRITE_MODE == WriteMode.DRY_RUN),
                    expected_mtime_ns=expected_mtime_ns,
                    precreated=precreated,
                )
            if result.get("status") != "success" or WRITE_MODE == WriteMode.DRY_RUN:
                _discard_stub()
            result["skill_used"] = skill_name
            result["conversation_uuid"] = resolved_uuid
            _log_json(
                "INFO", "ask_leo_skill_patch_done",
                skill=skill_name, status=result.get("status"),
                mode=result.get("mode"), file=result.get("file", ""),
            )
            return json.dumps(result, ensure_ascii=False)

        # Normal and plan paths return Leo's response with block metadata. The
        # plan is already compact JSON; the orchestrating model consumes it.
        has_copy: bool = _detect_copy_block(execution_payload) is not None
        has_plan: bool = _detect_plan_block(execution_payload) is not None

        # A resolved UUID with no completed text means Leo is still generating
        # (our dynamic budget ran out BEFORE a timeout was actually hit). Report
        # "still_working" with the UUID so the caller can resume the conversation
        # instead of treating this as a hard failure or a client-side timeout.
        if execution_payload == _INCOMPLETE_RESPONSE_SENTINEL:
            _log_json(
                "INFO", "ask_leo_skill_still_working",
                skill=skill_name, uuid=resolved_uuid[:8] if resolved_uuid else "",
            )
            return json.dumps({
                "status": "still_working",
                "skill_used": skill_name,
                "conversation_uuid": resolved_uuid,
                "message": (
                    "Leo is still generating the response (no completion within "
                    "the dynamic timeout). Resume this conversation to retrieve "
                    "the final answer."
                ),
            }, ensure_ascii=False)

        output_map: Dict[str, Any] = {
            "status": "success",
            "skill_used": skill_name,
            "content": execution_payload,
            "conversation_uuid": resolved_uuid,
            "has_copy_block": has_copy,
            "has_plan_block": has_plan,
            "files_injected": filepaths,
        }

        _conv_log(skill_name, resolved_uuid or "", prompt)
        _log_json("INFO", "ask_leo_skill_success", skill=skill_name, uuid=resolved_uuid[:8] if resolved_uuid else "")

        if cache_key:
            _cache_set(cache_key, json.dumps(output_map, ensure_ascii=False))

        return json.dumps(output_map, ensure_ascii=False)

    except Exception as execution_error:
        _discard_stub()
        _log_json("ERROR", "ask_leo_skill_error", skill=skill_name, error=str(execution_error))
        return json.dumps({
            "status": "error",
            "error": str(execution_error),
            "skill": skill_name,
        }, ensure_ascii=False)


@mcp.tool()
async def ask_leo_result(job_id: str) -> str:
    """Poll a background Leo generation started by ask_leo_skill.

    ask_leo_skill returns a job_id immediately for long-running (stream-detected,
    non-patch) skills; call this tool with that job_id to retrieve the result
    when it finishes.

    Args:
        job_id: The job handle returned by ask_leo_skill's "working" response.

    Returns:
        JSON string. status is "working" (still generating), "success" (content
        present), or "error" (unknown job or generation failed).
    """
    _log_json("INFO", "ask_leo_result_start", job_id=job_id)

    entry = leo_jobs.get_job(job_id)
    if entry is None:
        _log_json("WARN", "ask_leo_result_unknown", job_id=job_id)
        return json.dumps({
            "status": "error",
            "error": f"unknown job_id: {job_id}",
        }, ensure_ascii=False)

    task = entry.get("task")
    task_done = task is not None and task.done()

    # Terminal error (timeout or flow failure) recorded by the runner.
    if entry.get("error"):
        _log_json("INFO", "ask_leo_result_error", job_id=job_id, error=entry["error"])
        return json.dumps({
            "status": "error",
            "job_id": job_id,
            "skill_used": entry.get("skill"),
            "conversation_uuid": entry.get("uuid") or "",
            "error": entry["error"],
        }, ensure_ascii=False)

    # Still running: no terminal state yet.
    if entry.get("content") is None and not task_done:
        _log_json("INFO", "ask_leo_result_working", job_id=job_id)
        return json.dumps({
            "status": "working",
            "job_id": job_id,
            "skill_used": entry.get("skill"),
            "conversation_uuid": entry.get("uuid") or "",
        }, ensure_ascii=False)

    # Finished with content.
    content = entry.get("content")
    resolved_uuid = entry.get("uuid") or ""
    _conv_log(entry.get("skill") or "skill", resolved_uuid, entry.get("prompt") or "")
    _log_json("INFO", "ask_leo_result_success", job_id=job_id, content_len=len(content or ""))
    return json.dumps({
        "status": "success",
        "skill_used": entry.get("skill"),
        "content": content,
        "conversation_uuid": resolved_uuid,
        "job_id": job_id,
    }, ensure_ascii=False)


async def ask_leo_quick(prompt: str) -> str:
    """Fast web search via Brave AI using the shared headless browser pool.

    Optimized for direct, single-shot informational answers. Not for code
    debugging or deep research; use ask_leo_skill or ask_leo_extensive instead.

    Returns:
        JSON string with status ("success"|"error") and content.
    """
    try:
        strategy = SearchFactory.get_strategy('fast')
        browser = await _get_shared_browser()
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            bypass_csp=True
        )
        page = await context.new_page()

        try:
            result = await strategy.execute(page, prompt)
            _conv_log("ask_leo_quick", "", prompt)
            return json.dumps({
                "status": "success",
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }, ensure_ascii=False)
        finally:
            # Close per-request resources but keep the shared browser alive.
            await page.close()
            await context.close()

    except Exception as execution_error:
        return json.dumps({
            "status": "error",
            "content": str(execution_error)
        }, ensure_ascii=False)


@mcp.tool()
async def ask_leo_extensive(prompt: str) -> str:
    """Deep Brave AI research using the shared headless browser pool.

    Runs an exhaustive research session for complex informational queries
    requiring deep search, fact-checking, or extensive scraping. Use
    ask_leo_quick for quick answers and ask_leo_skill for code tasks.

    Returns:
        JSON string with status ("success"|"error") and content.
    """
    try:
        strategy = SearchFactory.get_strategy('deep')
        browser = await _get_shared_browser()
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            bypass_csp=True
        )
        page = await context.new_page()

        try:
            result = await strategy.execute(page, prompt)
            _conv_log("ask_leo_extensive", "", prompt)
            return json.dumps({
                "status": "success",
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }, ensure_ascii=False)
        finally:
            # Close per-request resources but keep the shared browser alive.
            await page.close()
            await context.close()

    except Exception as execution_error:
        return json.dumps({
            "status": "error",
            "content": str(execution_error)
        }, ensure_ascii=False)


@mcp.tool()
async def get_conversation_history(limit: int = 20) -> str:
    """Retrieve recent Leo interaction history.

    Returns chronological conversation metadata: timestamp, skill used, UUID,
    and a short prompt preview. Useful for auditing or resuming prior work.

    Args:
        limit: Maximum number of recent conversation records to return.

    Returns:
        JSON array of records (ts, skill, uuid, prompt_preview). On error, a
        JSON object with an "error" key.
    """
    _log_json("INFO", "get_conversation_history_start", limit=limit)

    try:
        trace_history: List[Dict[str, Any]] = _conv_get_recent(limit)
        _log_json("INFO", "get_conversation_history_success", count=len(trace_history))
        return json.dumps(trace_history, ensure_ascii=False)

    except Exception as execution_error:
        _log_json("ERROR", "get_conversation_history_error", error=str(execution_error))
        return json.dumps({
            "error": str(execution_error)
        }, ensure_ascii=False)


# ── leo_new_project: bootstrap a new project from a vague prompt ─────────────
# Pure consumer of senior_planner's plan_v1 contract. Writes two artifacts to
# project_path/: AGENTS.md (the ONLY file Hermes auto-loads — kept minimal) and
# KANBAN.init.md (a pure manifest read by leo_decompose_plan). Never creates
# kanban cards, never runs setup.sh, never touches profiles or SOUL.md.

# MINIMAL AGENTS.md static core. Hardcoded; never generated, never padded.
_AGENTS_STATIC = """\
## Worker Contract
kanban_show() first; cd $HERMES_KANBAN_WORKSPACE; kanban_heartbeat() on long ops;
finish with kanban_complete or kanban_block. Implementers route completion through
kanban_request_review, never kanban_complete directly.

## Blocked Card Protocol
kanban_block(reason="<kind>: <one line>") where kind is one of:
needs_input | needs_decision | dependency | capability | transient.

## TDD Protocol (senior-coder, at implementation time)
RED: write the acceptance test first, confirm it fails. GREEN: minimum code to pass.
REFACTOR: clean up; full suite still passes.
"""

_RISK_TO_PRIORITY = {"HIGH": 5, "MEDIUM": 4, "LOW": 3}


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_slug() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _local_id(step_id: int) -> str:
    return f"task-{int(step_id)}"


def _build_factcheck_prompt(prompt: str) -> str:
    return (
        "Fact-check ONLY the external technical claims in this request. For each "
        "named library, framework, API, service, or version, confirm it exists in "
        "2026 and note compatibility/deprecation risk. Do NOT plan the "
        f"implementation. Concise findings only.\n\nRequest:\n{prompt}"
    )


def _build_planner_prompt(prompt: str, factcheck: str) -> str:
    # No JSON shape here — STRUCTURED_DIRECTIVE in the skill already pins plan_v1.
    return (
        f"Goal:\n{prompt}\n\n"
        f"Verified external facts (web search):\n{factcheck}\n\n"
        "Prefer one target file per step. If underspecified, populate "
        "needs_user_decision rather than guessing."
    )


def _extract_plan_json(content: str) -> Dict[str, Any]:
    if not content:
        return {}
    m = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL) \
        or re.search(r"(\{.*\})", content, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _plan_is_actionable(plan_v1: Dict[str, Any]) -> bool:
    """Actionable iff steps present AND no blocking user decisions."""
    return bool(plan_v1.get("steps")) and not plan_v1.get("needs_user_decision")


def _map_plan_v1_to_kanban(
    plan_v1: Dict[str, Any], board_slug: str, tenant: str, workspace: str,
) -> Dict[str, Any]:
    """Pure mapper (no LLM). Drops dangling depends_on edges, keeps the task."""
    step_ids = {s.get("id") for s in plan_v1.get("steps", [])}
    warnings: List[str] = []
    tasks = []

    for step in plan_v1.get("steps", []):
        sid = step.get("id")
        deps = step.get("depends_on", []) or []
        resolved, dangling = [], []
        for d in deps:
            (resolved if d in step_ids else dangling).append(d)
        if dangling:
            warnings.append(
                f"step {sid}: dropped unresolvable depends_on {dangling} "
                f"(no matching step id); parent edge omitted"
            )
        targets = step.get("targets") or []
        # A step may legitimately have ZERO or MULTIPLE targets. Preserve all of
        # them in `targets` (the manifest lists them verbatim); the title only
        # needs a human label, so list every target — never silently drop any.
        if targets:
            title = f"Implement {', '.join(str(t) for t in targets)}"
        else:
            title = step.get("description", "")[:72]
        tasks.append({
            "local_id": _local_id(sid),
            "title": title,
            "assignee": "senior-coder",
            "priority": _RISK_TO_PRIORITY.get(str(step.get("risk", "")).upper(), 4),
            "workspace": workspace,
            "parents": [_local_id(d) for d in resolved],
            "spec": step.get("description", ""),
            "targets": targets,
            "acceptance": [step["done_when"]] if step.get("done_when") else [],
            "risk": step.get("risk", ""),
        })

    return {
        "board_slug": board_slug,
        "tenant": tenant,
        "workspace_default": workspace,
        "idempotency_prefix": f"{board_slug}-{_date_slug()}",
        "goal": plan_v1.get("goal", ""),
        "assumptions": plan_v1.get("assumptions", []),
        "plan_risks": plan_v1.get("risks", []),
        "tasks": tasks,
        "warnings": warnings,
    }


def _render_agents_md(km: Dict[str, Any], factcheck: str) -> str:
    """Minimal AGENTS.md — the ONLY file Hermes auto-loads (docs-verified)."""
    decided = "\n".join(f"- {a}" for a in km.get("assumptions", [])) or "- (none yet)"
    risks = "\n".join(
        f"- {r.get('risk', '')} → {r.get('mitigation', '')}"
        for r in km.get("plan_risks", [])
    ) or "- (none)"
    return (
        f"# AGENTS.md\n\n"
        f"## Goal\n{km.get('goal', '')}\n\n"
        f"## Decided (shared across all cards — do not re-decide)\n{decided}\n\n"
        f"## Verified External Facts\n{factcheck.strip()}\n\n"
        f"## Risks\n{risks}\n\n"
        f"{_AGENTS_STATIC}"
    )


def _render_kanban_init_md(km: Dict[str, Any]) -> str:
    lines = [
        "# KANBAN.init.md",
        "<!-- Input manifest for leo_decompose_plan. Do not rename ## headers. -->",
        "",
        f"- board_slug: {km['board_slug']}",
        f"- tenant: {km['tenant']}",
        f"- workspace_default: {km['workspace_default']}",
        f"- idempotency_prefix: {km['idempotency_prefix']}",
        "",
        "## Goal",
        km.get("goal", ""),
        "",
        "## Tasks",
    ]
    for t in km.get("tasks", []):
        parents = ", ".join(t["parents"]) or "(none)"
        acceptance = "; ".join(t["acceptance"]) or "(none)"
        lines += [
            f"### {t['local_id']} — {t['title']}",
            f"- assignee: {t['assignee']}",
            f"- priority: {t['priority']}",
            f"- workspace: {t['workspace']}",
            f"- parents: {parents}",
            f"- targets: {', '.join(t['targets']) or '(none)'}",
            f"- risk: {t['risk'] or '(none)'}",
            f"- acceptance: {acceptance}",
            "",
            f"{t['spec']}",
            "",
        ]
    warnings = km.get("warnings", [])
    if warnings:
        lines += ["## Open Questions / Warnings"]
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def leo_new_project(
    prompt: str,
    project_path: str,
    board_slug: str,
    tenant: str = "default",
    workdir: str = "",
    max_factcheck: bool = True,
) -> str:
    """Bootstrap a new project from a vague prompt.

    Writes AGENTS.md (minimal, auto-loaded by Hermes) and KANBAN.init.md (a
    manifest consumed by leo_decompose_plan) to project_path/. Never creates
    kanban cards or configures profiles.

    Args:
        prompt: Vague natural-language project description.
        project_path: Absolute dir for the two artifacts (created if missing).
        board_slug: Stamped into KANBAN.init.md; leo_decompose_plan reads it.
        tenant: Kanban tenant namespace (default "default").
        workdir: Absolute path for dir:<path> workspaces; "" → scratch.
        max_factcheck: Run ask_leo_extensive external fact-check (default True).

    Returns:
        JSON: {status, project_path, artifacts, board_slug, warnings,
               needs_user_decision, goal, task_count}.
    """
    _log_json("INFO", "leo_new_project_start", board=board_slug, tenant=tenant)
    warnings: List[str] = []
    workspace = f"dir:{workdir}" if workdir else "scratch"

    # ── Optional external fact-check (best-effort; never blocks planning) ────
    factcheck = ""
    if max_factcheck:
        try:
            raw_ext = await ask_leo_extensive(_build_factcheck_prompt(prompt))
            outer = json.loads(raw_ext)              # outer {status, content}
            if outer.get("status") == "success":
                inner = json.loads(outer.get("content", "{}"))  # content is JSON str
                if isinstance(inner, dict):
                    factcheck = json.dumps(inner, ensure_ascii=False)
                elif inner:
                    factcheck = str(inner)
        except Exception as exc:
            _log_json("WARN", "leo_new_project_factcheck_degraded", error=str(exc))
            warnings.append(f"fact-check failed ({exc}); proceeded without it")
        if not factcheck:
            warnings.append("fact-check returned no usable findings")

    # ── Plan via senior_planner (backgrounded; poll to completion) ───────────
    raw_plan = json.loads(await ask_leo_skill(
        skill_name="senior_planner",
        prompt=_build_planner_prompt(prompt, factcheck),
        filepaths=[],
        structured_plan=True,
    ))

    if raw_plan.get("status") == "working":
        raw_plan = await _poll_until_done(raw_plan["job_id"])
    elif raw_plan.get("status") == "busy":
        return json.dumps({
            "status": "busy", "message": "Leo busy; retry shortly.",
        }, ensure_ascii=False)
    elif raw_plan.get("status") == "still_working":
        return json.dumps({
            "status": "still_working",
            "conversation_uuid": raw_plan.get("conversation_uuid", ""),
            "message": "Leo still generating; call again with this uuid.",
        }, ensure_ascii=False)

    if raw_plan.get("status") == "error":
        return json.dumps({
            "status": "error", "error": raw_plan.get("error", "planner error"),
        }, ensure_ascii=False)

    plan_v1 = _extract_plan_json(raw_plan.get("content", ""))

    # ── Schema gate (D-3): fail fast on a non-plan_v1 shape ─────────────────
    if not plan_v1 or plan_v1.get("schema_version") != "plan_v1":
        return json.dumps({
            "status": "error",
            "error": "planner returned non-plan_v1 schema",
            "raw": str(plan_v1)[:300] if plan_v1 else raw_plan.get("content", "")[:300],
        }, ensure_ascii=False)

    # ── Actionable gate (D-2 / C-4): surface, never fabricate ────────────────
    if not _plan_is_actionable(plan_v1):
        return json.dumps({
            "status": "needs_user_decision",
            "needs_user_decision": plan_v1.get("needs_user_decision", []),
            "goal": plan_v1.get("goal", ""),
            "message": "Plan is underspecified; answer the questions and re-invoke.",
        }, ensure_ascii=False)

    # ── Map + render ALL file contents in memory FIRST (atomicity) ───────────
    km = _map_plan_v1_to_kanban(plan_v1, board_slug, tenant, workspace)
    warnings.extend(km["warnings"])
    agents_md = _render_agents_md(km, factcheck)
    kanban_md = _render_kanban_init_md(km)

    try:
        from pathlib import Path as _Path
        import tempfile as _tempfile

        root = _Path(project_path).expanduser()
        root.mkdir(parents=True, exist_ok=True)

        artifacts = {}
        for name, content in (("AGENTS.md", agents_md), ("KANBAN.init.md", kanban_md)):
            target = root / name
            fd, tmp = _tempfile.mkstemp(dir=str(root), prefix=f".{name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(tmp, str(target))
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            artifacts[name] = str(target)
    except OSError as exc:
        _log_json("ERROR", "leo_new_project_write_error", error=str(exc))
        return json.dumps({
            "status": "error", "error": f"failed to write artifacts: {exc}",
        }, ensure_ascii=False)

    _log_json(
        "INFO", "leo_new_project_done",
        board=board_slug, tasks=len(km["tasks"]), warnings=len(warnings),
    )
    return json.dumps({
        "status": "success",
        "project_path": str(root),
        "artifacts": artifacts,
        "board_slug": board_slug,
        "warnings": warnings,
        "needs_user_decision": [],
        "goal": km.get("goal", ""),
        "task_count": len(km["tasks"]),
    }, ensure_ascii=False)


# ── Executor Façade: single-file autonomous lifecycle over ONE UUID ──────────
# Light local model = EXECUTOR. Leo = BRAIN. One file, one conversation_uuid,
# one atomic action per turn. Reuses the engine untouched.


# Leo-facing contract: a legitimate task + strict output format.
# NOTE: this is NOT a persona. It never says "you are directing an executor",
# never says "you cannot reason", and never references another model. Those
# executor operating rules live ONLY in the client-side executor system prompt.
_LEO_STEP_CONTRACT = """\
Your task: determine the single next step required to accomplish the goal for \
the target file, and return it in the exact format below.

Return one fenced ```json block containing exactly one step object, then \
<<<LEO_DONE>>> on its own line. Do not add commentary before or after the block.

The step object must be exactly one of:
  {"action":"create","path":"<abs>","content":"<full literal file contents>"}
  {"action":"replace","path":"<abs>","find":"<exact literal>","replace":"<exact literal>"}
  {"action":"run","command":"<single shell command>"}
  {"action":"done","summary":"<one sentence>"}

Every non-done step must also include:
  "verify":"<one shell command; exit code 0 means success>"
  "why":"<one short sentence>"

Requirements:
- The step concerns only the target file. Do not reference other files.
- content/find/replace must be complete and literal: no placeholders, no
  ellipses, no "rest unchanged".
- Return exactly one step for this turn, never a multi-step list.
- If the goal is already met, return the "done" step.
"""


def _build_executor_session_prompt(goal: str, target_file: str) -> str:
    """Turn-1 request TO Leo: file is scoped, goal stated, format requested.

    Framed as a first-person request from the caller, so Leo treats it as a
    legitimate task, not as instructions embedded in pasted material.
    """
    return (
        f"I need the single next step to work on one file.\n\n"
        f"Target file (the only file in scope): {target_file}\n"
        f"Goal: {goal}\n\n"
        f"{_LEO_STEP_CONTRACT}"
    )


def _build_executor_turn_prompt(last_result: str, cap: int = 4000) -> str:
    """Resume-turn request: the raw result of the previous step, capped.

    The cap keeps the billed executor context and Leo's growing conversation
    small; the tail is preserved because errors live there.

    When the result contains a verify failure signal, a prefix is prepended
    that matches the systematic-debugging skill's description: trigger in
    Hermes, so the skill auto-loads into the session on the next turn.
    """
    # ── Failure detection ────────────────────────────────────────────────────
    # Check the raw last_result BEFORE truncation so the tail (where errors
    # live) is always included in the scan even if the head is cut.
    _result_lower = last_result.lower()
    _failed = any([
        "exit 1"          in last_result,    # shell exit code
        "exit code: 1"    in _result_lower,  # some runners format it this way
        "verify_fail"     in _result_lower,  # explicit sentinel from executor
        "error:"          in _result_lower,  # Python/compiler errors
        "traceback"       in _result_lower,  # Python exceptions
        "nameerror"       in _result_lower,  # common Python runtime errors
        "syntaxerror"     in _result_lower,
        "assertionerror"  in _result_lower,
        "failed"          in _result_lower,  # test runner output
        "no such file"    in _result_lower,  # filesystem errors
    ])

    # ── Truncation (unchanged logic) ─────────────────────────────────────────
    if len(last_result) > cap:
        head = last_result[: cap // 2]
        tail = last_result[-cap // 2 :]
        last_result = f"{head}\n\n[... output truncated ...]\n\n{tail}"

    # ── Failure prefix ───────────────────────────────────────────────────────
    # The phrase "systematic debugging required" matches the description:
    # trigger in the systematic-debugging Superpowers skill so Hermes
    # auto-loads it into the session. Only prepended on actual failures —
    # never on a clean verify — so the skill does not fire spuriously.
    _prefix = (
        "VERIFY FAILED — systematic debugging required.\n\n"
        if _failed else ""
    )

    return (
        f"{_prefix}"
        f"Here is the result of the previous step "
        f"(stdout + stderr + verify exit status):\n\n{last_result}\n\n"
        f"Return the next single step in the same format, or the \"done\" "
        f"step if the goal is met."
    )


async def _poll_until_done(job_id: str, timeout: float = 300.0) -> Dict[str, Any]:
    """Absorb ask_leo_result polling so the executor never sees a job_id."""
    deadline = time.monotonic() + timeout
    delay = 1.5
    while time.monotonic() < deadline:
        res = json.loads(await ask_leo_result(job_id))
        if res.get("status") in ("success", "error"):
            return res
        await asyncio.sleep(delay)
        delay = min(delay * 1.3, 6.0)
    return {"status": "error", "error": "Leo timed out; call again to resume."}


def _extract_single_action(planner_content: str) -> Dict[str, Any]:
    """Parse Leo's fenced JSON single-action block; tolerant of stray prose."""
    if not planner_content:
        return {"action": "error", "error": "empty planner response"}
    m = re.search(r"```json\s*(\{.*?\})\s*```", planner_content, re.DOTALL)
    if not m:
        m = re.search(r"(\{.*\})", planner_content, re.DOTALL)
    if not m:
        return {"action": "error", "error": "no JSON action found",
                "raw": planner_content[:300]}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return {"action": "error", "error": f"bad JSON: {e}", "raw": m.group(1)[:300]}


def _looks_like_pasted_file(prompt: str, filepaths: List[str]) -> Optional[str]:
    """Detect the read-then-paste anti-pattern in an incoming request.

    Guards the token-economy contract: the caller must pass a path in
    `filepaths` and let the server inject the file, never paste file contents
    into `prompt`. Returns an actionable rejection reason when the prompt looks
    like pasted source, or None when the request is clean.

    Detection is heuristic and deliberately conservative: it fires only on a
    large prompt that also carries structural code signals, so a genuinely long
    task description without pasted code is not penalised.
    """
    code_signals = (
        prompt.count("\n") > 40
        or "```" in prompt
        or bool(
            re.search(
                r"^\s*(def |class |import |from |func |const |public |private )",
                prompt,
                re.MULTILINE,
            )
        )
    )
    if len(prompt) > 3000 and code_signals:
        return (
            "This prompt appears to contain pasted file contents. Do NOT read or "
            "paste files. Put ONLY your task description in `prompt` and pass the "
            "absolute path in `filepaths`; the server injects the file for you."
        )
    if len(prompt) > 5000 and not filepaths:
        return (
            "This prompt is very large and no `filepaths` were provided. If it "
            "contains file content, do NOT paste it: pass the absolute path in "
            "`filepaths` and keep only the task description in `prompt`."
        )
    return None

@mcp.tool()
async def leo_next_instruction(
    target_file: str,
    goal: str,
    conversation_uuid: str = "",
    last_result: str = "",
) -> str:
    """Get the SINGLE next atomic action for the locked target file.

    DO NOT read or open the target file yourself, not before turn 1, not ever.
    The file is injected ONCE by this server on turn 1. Reading it yourself
    duplicates it into your context for zero benefit: you are the EXECUTOR, you
    do not need to see the file to run the returned action. Pass its path as
    `target_file` and execute what comes back.

    You are an EXECUTOR. Do not plan. Call this, perform the ONE returned
    action verbatim, then call again with the raw result. Loop until
    action == "done".

    TURN 1: conversation_uuid="" — the file is injected once; a uuid is returned.
    TURN 2+: pass back the SAME conversation_uuid plus last_result (raw
        stdout+stderr). Never re-send the file. Never change the uuid.

    Returns JSON: {"action":..., "verify":..., "conversation_uuid":"<reuse>"}.
    action "done" ends the session; action "error"/"wait" → show and stop/retry.
    """
    _log_json("INFO", "leo_next_instruction", uuid=conversation_uuid[:8], first=not conversation_uuid)
    is_first = not conversation_uuid

    if is_first:
        prompt = _build_executor_session_prompt(goal, target_file)
        files = [target_file]
    else:
        prompt = _build_executor_turn_prompt(last_result)
        files = []

    raw = json.loads(await ask_leo_skill(
        skill_name="senior_planner",
        prompt=prompt,
        filepaths=files,
        conversation_uuid=conversation_uuid,
    ))

    # Turn 1 returns a background job → absorb polling to recover the UUID.
    if raw.get("status") == "working":
        raw = await _poll_until_done(raw["job_id"])
    elif raw.get("status") == "busy":
        return json.dumps({"action": "wait",
                           "message": "Leo busy; retry shortly.",
                           "conversation_uuid": conversation_uuid}, ensure_ascii=False)

    if raw.get("status") == "still_working":
        return json.dumps({"action": "wait",
                           "message": "Leo still generating; call again with this uuid.",
                           "conversation_uuid": raw.get("conversation_uuid", conversation_uuid)},
                          ensure_ascii=False)

    if raw.get("status") == "error":
        return json.dumps({"action": "error",
                           "error": raw.get("error", "unknown"),
                           "conversation_uuid": conversation_uuid}, ensure_ascii=False)

    resolved_uuid = raw.get("conversation_uuid", "") or conversation_uuid
    action = _extract_single_action(raw.get("content", ""))
    action["conversation_uuid"] = resolved_uuid    # executor MUST reuse this
    return json.dumps(action, ensure_ascii=False)


@mcp.tool()
async def leo_apply_edit(
    target_file: str,
    find: str,
    replace: str,
    conversation_uuid: str = "",
) -> str:
    """Apply ONE literal find/replace to the locked target file (writes to disk).

    Pass the path only; never read or paste the file. Mechanical, no reasoning.
    Wraps code_editor (str_replace). Synchronous.
    Reuse the SAME conversation_uuid so the edit shares Leo's memory with the
    reasoning turns.

    Returns JSON: the code_editor summary plus conversation_uuid to reuse.
    """
    _log_json("INFO", "leo_apply_edit", file=target_file, uuid=conversation_uuid[:8])
    prompt = (
        f"Apply exactly one str_replace edit to the file.\n"
        f"FIND (exact literal):\n{find}\n\n"
        f"REPLACE WITH (exact literal):\n{replace}\n"
    )
    raw = await ask_leo_skill(
        skill_name="code_editor",
        prompt=prompt,
        filepaths=[target_file],
        conversation_uuid=conversation_uuid,
    )
    result = json.loads(raw)
    # code_editor is synchronous; surface its uuid for reuse next turn.
    if "conversation_uuid" not in result:
        result["conversation_uuid"] = conversation_uuid
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    # Single-instance guard lives here (not at import time) so importing the
    # module for tests never fights a running server's lock.
    _acquire_lock()

    # Encryption-format startup probe (Layer 6): detect a non-v10 build loudly
    # rather than silently serving plaintext reads. Observability-only — never
    # blocks startup.
    try:
        from leo_chat.db.leo_read import probe_encryption_format
        _enc_format = probe_encryption_format()
        if _enc_format == "v10":
            print("[OK] AIChat DB format: v10 (AES-CBC encrypted)", file=sys.stderr)
        elif _enc_format == "plaintext":
            print(
                "[WARN] AIChat DB format: PLAINTEXT (non-v10 build). decrypt() "
                "will pass blobs through unencrypted.",
                file=sys.stderr,
            )
        else:
            print(
                "[WARN] AIChat DB format: UNAVAILABLE (no assistant entry yet; "
                "will re-evaluate on first generation).",
                file=sys.stderr,
            )
    except Exception as _enc_exc:
        print(f"[WARN] Encryption-format probe failed: {_enc_exc}", file=sys.stderr)

    # Attempt CDP bootstrap at startup; tool calls retry the connection later.
    if not bootstrap_brave_cdp(port=9222, profile=BRAVE_PROFILE):
        print(
            "[WARN] CDP not ready at startup; will retry on first tool call.",
            file=sys.stderr,
        )
    mcp.run(transport="streamable-http")
