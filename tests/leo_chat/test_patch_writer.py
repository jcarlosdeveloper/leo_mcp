"""Tests for single-file patch application: validation, containment, atomicity."""

import os
import tempfile
from pathlib import Path

from leo_chat.patch_writer import (
    apply_single_patch,
    apply_edit_patch,
    edit_response_complete,
    response_has_terminal_token,
    strip_terminal_token,
    TERMINAL_TOKEN,
)


def _mk_root():
    return tempfile.mkdtemp()


def _env(file, content, action="modify", risk="LOW", summary="test change"):
    """Build a FILE_PATCH envelope for the given fields.

    Mirrors how a real response is shaped: the closing sentinel sits on its own
    line, so a formatting newline is added after content regardless of whether
    content itself already ends in one. The parser strips exactly one leading
    and one trailing newline around the content, so this round-trips exactly.
    """
    return (
        "<<<LEO_PATCH>>>\n"
        f"FILE: {file}\n"
        f"ACTION: {action}\n"
        f"RISK: {risk}\n"
        f"SUMMARY: {summary}\n"
        "<<<CONTENT>>>\n"
        f"{content}\n"
        "<<<END_LEO_PATCH>>>"
    )


def test_dry_run_does_not_write():
    root = _mk_root()
    target = os.path.join(root, "sample.py")
    Path(target).write_text("old\n")
    payload = _env(target, "new\n")
    r = apply_single_patch(payload, allowed_root=root, dry_run=True)
    assert r["mode"] == "dry_run"


def test_terminal_token_present():
    assert response_has_terminal_token('{"a": 1}\n' + TERMINAL_TOKEN) is True


def test_terminal_token_absent():
    assert response_has_terminal_token('{"a": 1}') is False


def test_terminal_token_empty_or_none():
    assert response_has_terminal_token("") is False
    assert response_has_terminal_token(None) is False


def test_terminal_token_not_at_end():
    # A token present mid-document is not a genuine terminator.
    assert response_has_terminal_token(TERMINAL_TOKEN + "\n{\"a\": 1}") is False


def test_strip_terminal_token():
    assert strip_terminal_token('{"a": 1}\n' + TERMINAL_TOKEN) == '{"a": 1}'
    assert strip_terminal_token("plain text") == "plain text"
    assert strip_terminal_token(None) == ""


def test_write_creates_backup():
    root = _mk_root()
    target = os.path.join(root, "sample.py")
    Path(target).write_text("old\n")
    payload = _env(target, "new\n")
    r = apply_single_patch(payload, allowed_root=root, dry_run=False)
    assert r["mode"] == "write"
    assert Path(r["file"]).read_text() == "new\n"
    assert Path(r["file"] + ".bak").read_text() == "old\n"


def test_path_traversal_rejected():
    root = _mk_root()
    payload = _env("/etc/passwd", "x")
    r = apply_single_patch(payload, allowed_root=root)
    assert r["status"] == "error"
    assert "outside" in r["error"]


def test_truncated_envelope_rejected():
    root = _mk_root()
    target = os.path.join(root, "sample.py")
    Path(target).write_text("old\n")
    payload = (
        "<<<LEO_PATCH>>>\n"
        f"FILE: {target}\n"
        "ACTION: modify\n"
        "<<<CONTENT>>>\n"
        "class Foo:\n    def bar(self):\n        return "
    )
    r = apply_single_patch(payload, allowed_root=root)
    assert r["status"] == "error"
    assert "truncated" in r["error"]


def test_no_envelope_rejected():
    root = _mk_root()
    r = apply_single_patch("just some prose with no envelope at all", allowed_root=root)
    assert r["status"] == "error"
    assert "envelope" in r["error"]


def test_modify_nonexistent_rejected():
    root = _mk_root()
    payload = _env(os.path.join(root, "nope.py"), "x", action="modify")
    r = apply_single_patch(payload, allowed_root=root)
    assert r["status"] == "error"
    assert "does not exist" in r["error"]


def test_create_writes_new_file():
    root = _mk_root()
    new = os.path.join(root, "created.py")
    payload = _env(new, "hi\n", action="create")
    r = apply_single_patch(payload, allowed_root=root, dry_run=False)
    assert r["status"] == "success"
    assert Path(r["file"]).read_text() == "hi\n"


def test_create_existing_rejected():
    root = _mk_root()
    target = os.path.join(root, "exists.py")
    Path(target).write_text("here\n")
    payload = _env(target, "x", action="create")
    r = apply_single_patch(payload, allowed_root=root)
    assert r["status"] == "error"
    assert "already exists" in r["error"]


def test_extracts_envelope_from_prose():
    root = _mk_root()
    target = os.path.join(root, "x.py")
    Path(target).write_text("old\n")
    payload = (
        "Sure, here it is:\n\n"
        + _env(target, "new\n")
        + "\nHope that helps!"
    )
    r = apply_single_patch(payload, allowed_root=root, dry_run=True)
    assert r["status"] == "success"


def test_content_with_braces_and_quotes_roundtrips():
    root = _mk_root()
    target = os.path.join(root, "code.py")
    Path(target).write_text("old\n")
    content = (
        'def greet(name: str) -> str:\n'
        '    data = {"key": "value", "nested": {"a": 1}}\n'
        '    return f"Hello, {name}!"  # uses \\n and "quotes" raw\n'
    )
    payload = _env(target, content)
    r = apply_single_patch(payload, allowed_root=root, dry_run=False)
    assert r["status"] == "success"
    assert Path(r["file"]).read_text() == content


def test_forbidden_location_rejected_inside_root():
    """A sensitive location is rejected even when nominally inside the root.

    Uses the real home ~/.ssh so the denylist (not the root containment check)
    is what rejects it.
    """
    from pathlib import Path as P
    ssh_dir = P.home() / ".ssh"
    target = str(ssh_dir / "leo_probe_config")
    # Allow the whole home as root so containment passes and only the denylist
    # can reject the write.
    r = apply_single_patch(_env(target, "x\n"), allowed_root=str(P.home()))
    assert r["status"] == "error"
    assert "forbidden" in r["error"]


def test_backup_is_versioned_not_clobbered():
    """A second write preserves the original .bak and creates .bak.1."""
    root = _mk_root()
    target = os.path.join(root, "s.py")
    Path(target).write_text("original\n")
    apply_single_patch(_env(target, "v1\n"), allowed_root=root, dry_run=False)
    apply_single_patch(_env(target, "v2\n"), allowed_root=root, dry_run=False)
    assert Path(target + ".bak").read_text() == "original\n"
    assert Path(target + ".bak.1").read_text() == "v1\n"
    assert Path(target).read_text() == "v2\n"


def test_toctou_abort_on_mtime_change():
    """A file changed after mtime capture aborts the write (no clobber)."""
    root = _mk_root()
    target = os.path.join(root, "t.py")
    Path(target).write_text("a\n")
    stale_ns = Path(target).stat().st_mtime_ns
    # Simulate a concurrent edit that advanced mtime after capture.
    new_ns = stale_ns + 10_000_000
    os.utime(target, ns=(new_ns, new_ns))
    r = apply_single_patch(
        _env(target, "b\n"), allowed_root=root, expected_mtime_ns=stale_ns
    )
    assert r["status"] == "error"
    assert "changed on disk" in r["error"]
    assert Path(target).read_text() == "a\n"


def test_toctou_allows_unchanged_file():
    """A matching mtime lets the write proceed normally."""
    root = _mk_root()
    target = os.path.join(root, "u.py")
    Path(target).write_text("a\n")
    mtime_ns = Path(target).stat().st_mtime_ns
    r = apply_single_patch(
        _env(target, "b\n"), allowed_root=root, expected_mtime_ns=mtime_ns
    )
    assert r["status"] == "success"
    assert Path(target).read_text() == "b\n"


# ---------------------------------------------------------------------------
# PatchValidator tests
# ---------------------------------------------------------------------------

from leo_chat.patch_validator import PatchValidator


def test_validator_passes_valid_envelope():
    root = _mk_root()
    target = os.path.join(root, "ok.py")
    Path(target).write_text("old\n")
    payload = _env(target, "new\n")
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is True
    assert "patch" in detail


def test_validator_rejects_missing_end_sentinel():
    root = _mk_root()
    target = os.path.join(root, "x.py")
    Path(target).write_text("old\n")
    payload = (
        "<<<LEO_PATCH>>>\n"
        f"FILE: {target}\n"
        "ACTION: modify\n"
        "RISK: LOW\n"
        "SUMMARY: test\n"
        "<<<CONTENT>>>\n"
        "new content\n"
    )
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "syntactic"
    assert "truncated" in detail["error"]


def test_validator_rejects_no_envelope():
    root = _mk_root()
    ok, detail = PatchValidator.validate("just some prose, no envelope here", root)
    assert ok is False
    assert detail["stage"] == "syntactic"
    assert "envelope" in detail["error"]


def test_validator_rejects_missing_required_header():
    root = _mk_root()
    target = os.path.join(root, "h.py")
    Path(target).write_text("old\n")
    payload = (
        "<<<LEO_PATCH>>>\n"
        f"FILE: {target}\n"
        "ACTION: modify\n"
        "<<<CONTENT>>>\n"
        "new\n"
        "<<<END_LEO_PATCH>>>"
    )
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "extraction"


def test_validator_rejects_invalid_action():
    root = _mk_root()
    target = os.path.join(root, "a.py")
    Path(target).write_text("old\n")
    payload = _env(target, "new\n", action="delete")
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "extraction"


def test_validator_rejects_empty_content():
    root = _mk_root()
    target = os.path.join(root, "e.py")
    Path(target).write_text("old\n")
    payload = _env(target, "   ")
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "semantic"


def test_validator_rejects_placeholder_content():
    root = _mk_root()
    target = os.path.join(root, "p.py")
    Path(target).write_text("old\n")
    payload = _env(target, "def f():\n    [implement this]\n")
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "semantic"


def test_validator_allows_ordinary_todo_comments():
    """A narrowed stub pattern must not flag legitimate TODO/FIXME comments."""
    root = _mk_root()
    target = os.path.join(root, "todo.py")
    Path(target).write_text("old\n")
    content = (
        "# TODO: revisit this later\n"
        "# FIXME: edge case with empty input\n"
        "# TODO: add more tests\n"
        "def f():\n    return 1\n"
    )
    payload = _env(target, content)
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is True


def test_validator_rejects_suspicious_shrink():
    root = _mk_root()
    target = os.path.join(root, "big.py")
    Path(target).write_text("x = 1\n" * 500)
    payload = _env(target, "x = 1\n")
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "semantic"
    assert "truncated" in detail["error"]


def test_validator_rejects_path_outside_root():
    root = _mk_root()
    payload = _env("/etc/passwd", "x\n")
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "safety"


def test_validator_rejects_modify_nonexistent():
    root = _mk_root()
    payload = _env(os.path.join(root, "nope.py"), "x\n", action="modify")
    ok, detail = PatchValidator.validate(payload, root)
    assert ok is False
    assert detail["stage"] == "safety"


def test_apply_single_patch_reports_validation_stage():
    root = _mk_root()
    r = apply_single_patch("no envelope at all", allowed_root=root)
    assert r["status"] == "error"
    assert r["validation_stage"] == "syntactic"


# ---------------------------------------------------------------------------
# execute_leo_flow_with_robust_patches retry-gate tests (no browser involved)
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, patch

import leo_chat.execution as execution


async def test_robust_retry_stops_immediately_on_safety_failure():
    """A safety-stage failure (e.g. path outside root) is not retried."""
    root = _mk_root()
    bad_payload = _env("/etc/passwd", "x\n")
    stub = AsyncMock(return_value=(bad_payload, "uuid-1"))
    with patch.object(execution, "execute_leo_flow", stub), \
         patch("leo_chat.skills.skill_support.skill_supports_patch", return_value=True):
        response, uuid = await execution.execute_leo_flow_with_robust_patches(
            skill_name="code_refiner",
            user_prompt="do it",
            filepaths=["/etc/passwd"],
            structured=True,
            allowed_root=root,
            max_attempts=3,
        )
    assert stub.call_count == 1
    assert response == bad_payload
    assert uuid == "uuid-1"


async def test_robust_retry_exhausts_attempts_on_recoverable_failure():
    """A syntactic-stage failure (malformed envelope) retries up to max_attempts."""
    root = _mk_root()
    stub = AsyncMock(return_value=("not an envelope at all", "uuid-2"))
    with patch.object(execution, "execute_leo_flow", stub), \
         patch("leo_chat.skills.skill_support.skill_supports_patch", return_value=True):
        response, uuid = await execution.execute_leo_flow_with_robust_patches(
            skill_name="code_refiner",
            user_prompt="do it",
            filepaths=["/tmp/x.py"],
            structured=True,
            allowed_root=root,
            max_attempts=3,
        )
    assert stub.call_count == 3
    assert response == "not an envelope at all"


async def test_robust_retry_succeeds_on_first_valid_attempt():
    """A valid envelope on attempt 1 returns immediately, no retry."""
    root = _mk_root()
    target = os.path.join(root, "ok.py")
    Path(target).write_text("old\n")
    good_payload = _env(target, "new\n")
    stub = AsyncMock(return_value=(good_payload, "uuid-3"))
    with patch.object(execution, "execute_leo_flow", stub), \
         patch("leo_chat.skills.skill_support.skill_supports_patch", return_value=True):
        response, uuid = await execution.execute_leo_flow_with_robust_patches(
            skill_name="code_refiner",
            user_prompt="do it",
            filepaths=[target],
            structured=True,
            allowed_root=root,
            max_attempts=3,
        )
    assert stub.call_count == 1
    assert response == good_payload
    assert uuid == "uuid-3"


async def test_robust_retry_passes_through_for_non_patch_skill():
    """Non-patch skills (structured=False) bypass validation entirely."""
    stub = AsyncMock(return_value=("plain prose response", "uuid-4"))
    with patch.object(execution, "execute_leo_flow", stub):
        response, uuid = await execution.execute_leo_flow_with_robust_patches(
            skill_name="senior_planner",
            user_prompt="explain",
            structured=False,
        )
    assert stub.call_count == 1
    assert response == "plain prose response"


# ── LEO_EDIT (edit path) tests ──────────────────────────────────────────────


def _edit_env(file, old, new, action="edit", risk="LOW", summary="edit change"):
    return (
        "<<<LEO_EDIT>>>\n"
        f"FILE: {file}\n"
        f"ACTION: {action}\n"
        f"RISK: {risk}\n"
        f"SUMMARY: {summary}\n"
        "<<<OLD_STR>>>\n"
        f"{old}\n"
        "<<<NEW_STR>>>\n"
        f"{new}\n"
        "<<<END_LEO_EDIT>>>"
    )


def test_edit_response_complete():
    assert edit_response_complete(_edit_env("x", "a", "b")) is True
    assert edit_response_complete("<<<LEO_EDIT>>>\nFILE: x\n<<<OLD_STR>>>\na\n<<<NEW_STR>>>\nb") is False  # missing end
    assert edit_response_complete("no envelope here") is False
    assert edit_response_complete(None) is False


def test_apply_edit_single_match_writes_and_backs_up():
    root = _mk_root()
    target = os.path.join(root, "s.py")
    Path(target).write_text("a = 1\nb = 2\nc = 3\n")
    r = apply_edit_patch(
        _edit_env(target, "b = 2", "b = 22"), allowed_root=root, dry_run=False
    )
    assert r["status"] == "success"
    assert r["mode"] == "edit"
    assert Path(target).read_text() == "a = 1\nb = 22\nc = 3\n"
    assert Path(target + ".bak").read_text() == "a = 1\nb = 2\nc = 3\n"


def test_apply_edit_multiple_match_rejected():
    root = _mk_root()
    target = os.path.join(root, "s.py")
    Path(target).write_text("x = 1\nx = 1\n")
    r = apply_edit_patch(_edit_env(target, "x = 1", "x = 9"), allowed_root=root)
    assert r["status"] == "error"
    assert "matched" in r["error"]
    # No write happened.
    assert Path(target).read_text() == "x = 1\nx = 1\n"


def test_apply_edit_zero_match_rejected():
    root = _mk_root()
    target = os.path.join(root, "s.py")
    Path(target).write_text("keep this\n")
    r = apply_edit_patch(_edit_env(target, "not present", "z"), allowed_root=root)
    assert r["status"] == "error"
    assert "matched 0" in r["error"]


def test_apply_edit_empty_old_str_rejected():
    root = _mk_root()
    target = os.path.join(root, "s.py")
    Path(target).write_text("content\n")
    r = apply_edit_patch(_edit_env(target, "", "z"), allowed_root=root)
    assert r["status"] == "error"
    assert "empty" in r["error"]


def test_apply_edit_dry_run_no_write():
    root = _mk_root()
    target = os.path.join(root, "s.py")
    Path(target).write_text("a\nb\n")
    r = apply_edit_patch(
        _edit_env(target, "b", "B"), allowed_root=root, dry_run=True
    )
    assert r["status"] == "success"
    assert r["mode"] == "dry_run"
    assert Path(target).read_text() == "a\nb\n"


def test_apply_edit_forbidden_rejected():
    root = Path.home()
    r = apply_edit_patch(
        _edit_env(str(root / ".ssh" / "config"), "a", "b"), allowed_root=str(root)
    )
    assert r["status"] == "error"
    assert "forbidden" in r["error"]


def test_apply_edit_missing_target_rejected():
    root = _mk_root()
    target = os.path.join(root, "nope.py")
    r = apply_edit_patch(_edit_env(target, "a", "b"), allowed_root=root)
    assert r["status"] == "error"
    assert "does not exist" in r["error"]


def test_apply_edit_over_reach_rejected():
    """A widened old_str that swallows two separated change sites is refused.

    The model disambiguated a repeated token by widening old_str until it spanned
    both functions, making it unique but mutating two separated regions (a rename
    applied in add AND sub). The over-reach guard must reject this even though
    the old_str matches exactly once.
    """
    root = _mk_root()
    target = os.path.join(root, "math.py")
    original = (
        "def add(a, b):\n"
        "    result = a + b\n"
        "    return result\n"
        "\n"
        "def sub(a, b):\n"
        "    result = a - b\n"
        "    return result\n"
    )
    Path(target).write_text(original)

    old = (
        "def add(a, b):\n"
        "    result = a + b\n"
        "    return result\n"
        "\n"
        "def sub(a, b):\n"
        "    result = a - b\n"
        "    return result"
    )
    new = (
        "def add(a, b):\n"
        "    total = a + b\n"
        "    return total\n"
        "\n"
        "def sub(a, b):\n"
        "    total = a - b\n"
        "    return total"
    )
    r = apply_edit_patch(_edit_env(target, old, new), allowed_root=root)
    assert r["status"] == "error"
    assert "region" in r["error"]
    # No write happened; file untouched.
    assert Path(target).read_text() == original


def test_apply_edit_anchored_single_rename_succeeds():
    """Anchoring the one intended occurrence (via its enclosing def) is allowed.

    Same repeated token, but old_str scopes to the single 'add' function, so the
    change region count stays at 1 and the edit is applied to add only.
    """
    root = _mk_root()
    target = os.path.join(root, "math.py")
    original = (
        "def add(a, b):\n"
        "    result = a + b\n"
        "    return result\n"
        "\n"
        "def sub(a, b):\n"
        "    result = a - b\n"
        "    return result\n"
    )
    Path(target).write_text(original)

    old = "def add(a, b):\n    result = a + b\n    return result"
    new = "def add(a, b):\n    total = a + b\n    return total"
    r = apply_edit_patch(_edit_env(target, old, new), allowed_root=root)
    assert r["status"] == "success"
    after = Path(target).read_text()
    assert "def add(a, b):\n    total = a + b\n    return total" in after
    # sub is untouched.
    assert "result = a - b" in after
    assert "total = a - b" not in after

