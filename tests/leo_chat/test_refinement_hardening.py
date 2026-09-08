"""Regression tests for the delegation-hardening refactor.

Covers factory override validation, byte-faithful raw context reading, and the
per-request write-root resolution (project / temp allowlist, fail-closed).
"""

import tempfile
from pathlib import Path

import pytest


# ── Fase E: factory hardening ────────────────────────────────────────────────


def test_register_rejects_missing_override():
    from leo_chat.skills.skill_factory import SkillFactory
    from leo_chat.skills.base_skill import BaseSkill

    class NoDescription(BaseSkill):
        skill_id = "no_desc"
        model_key = "some-model"
        # description NOT overridden

    with pytest.raises(TypeError):
        SkillFactory.register("no_desc", NoDescription)


def test_register_rejects_key_mismatch():
    from leo_chat.skills.skill_factory import SkillFactory
    from leo_chat.skills.base_skill import BaseSkill

    class Mismatch(BaseSkill):
        skill_id = "actual_id"
        model_key = "some-model"
        description = "a real description"

    with pytest.raises(TypeError):
        SkillFactory.register("different_key", Mismatch)


def test_get_skill_class_does_not_instantiate():
    from leo_chat.skills.skill_factory import SkillFactory

    cls = SkillFactory.get_skill_class("code_refiner")
    assert isinstance(cls, type)
    assert cls.supports_patch_output is True


def test_skill_supports_patch_reads_class_flag():
    from leo_chat.skills.skill_support import skill_supports_patch

    assert skill_supports_patch("code_refiner") is True
    assert skill_supports_patch("code_editor") is True
    assert skill_supports_patch("senior_planner") is False
    assert skill_supports_patch("nonexistent") is False


# ── Fase A: raw mode byte fidelity ───────────────────────────────────────────


def test_raw_read_is_byte_identical():
    from leo_chat.context.prompt_builder import _read_file_safe

    content = "def f():   \n    x = 1  \n\n\treturn x"  # trailing ws, tab, no final NL
    p = Path(tempfile.mktemp(suffix=".py"))
    p.write_text(content)
    try:
        assert _read_file_safe(p, raw=True) == content
    finally:
        p.unlink()


def test_annotated_read_has_line_numbers():
    from leo_chat.context.prompt_builder import _read_file_safe

    p = Path(tempfile.mktemp(suffix=".py"))
    p.write_text("a = 1\nb = 2\n")
    try:
        out = _read_file_safe(p, raw=False)
        assert "# File:" in out and "1|" in out
    finally:
        p.unlink()


def test_raw_assemble_skips_project_context():
    from leo_chat.context.prompt_builder import assemble_context

    # This repo has pyproject.toml/AGENTS.md, so non-raw would inject them.
    raw = assemble_context(["leo_mcp_server.py"], raw=True)
    annotated = assemble_context(["leo_mcp_server.py"], raw=False)
    assert "PROJECT CONTEXT" not in raw
    assert "PROJECT CONTEXT" in annotated


# ── Fase B/I: per-request write root (denylist-only policy) ──────────────────


def test_write_root_returns_parent_for_any_file():
    import leo_mcp_server as srv

    proj = tempfile.mkdtemp()
    sub = Path(proj, "pkg")
    sub.mkdir()
    f = sub / "mod.py"
    f.write_text("x = 1\n")
    # New policy: parent of target is always returned (denylist is the gate)
    assert srv._resolve_write_root([str(f)]) == str(sub.resolve())


def test_write_root_temp_uses_parent():
    import leo_mcp_server as srv

    d = Path(tempfile.mkdtemp())  # under the system temp dir
    f = d / "scratch.py"
    f.write_text("y = 1\n")
    assert srv._resolve_write_root([str(f)]) == str(d.resolve())


def test_write_root_nonexistent_file_returns_parent():
    import leo_mcp_server as srv

    # A file that does not exist yet (create action): parent is returned.
    target = Path.home() / "some_project" / "new_file.py"
    assert srv._resolve_write_root([str(target)]) == str((Path.home() / "some_project").resolve())


def test_write_root_empty_is_none():
    import leo_mcp_server as srv

    assert srv._resolve_write_root([]) is None


# ── Fase G: byte-faithful stitching + coherent structured completion ─────────


class _FakeLeoPage:
    """Minimal stand-in for BraveLeoPage used by the stitching/completion tests."""

    def __init__(self, continuation="", uuid="conv-1"):
        self._last_model_key = "some-model"
        self.conversation_uuid = uuid
        self._continuation = continuation
        self.sent = []

    async def _send_one_message(self, final_prompt, model_key, max_attempts=1, is_first=False):
        self.sent.append(final_prompt)
        return True

    async def wait_for_response(self, timeout_ms=0):
        return self._continuation


async def test_patch_stitching_is_byte_faithful_at_pathological_cut():
    """Cut on 'newline + indent + trailing spaces' must reconstruct byte-identically."""
    from leo_chat.execution import _auto_continue_if_truncated

    # Truncated exactly after trailing whitespace, no final newline.
    existing = "def f():\n    x = 1\n    return x   "
    # Leo repeats the overlapping tail verbatim, then continues, then closes.
    continuation = "    return x   \n    y = 2\n# <<<END>>>"
    expected = "def f():\n    x = 1\n    return x   \n    y = 2\n# <<<END>>>"

    page = _FakeLeoPage(continuation=continuation)
    result = await _auto_continue_if_truncated(
        page,
        existing,
        max_continues=3,
        is_truncated=lambda text: "<<<END>>>" not in text,
        continue_prompt="continue",
        patch_mode=True,
    )

    assert result == expected, f"stitching not byte-faithful:\n{result!r}\n!=\n{expected!r}"
    # Trailing whitespace at the cut survived and no duplication occurred.
    assert "    return x   \n" in result
    assert result.count("return x") == 1


async def test_clean_cut_does_not_hide_the_bug():
    """A clean (whitespace-free) cut also stitches byte-identically in patch mode."""
    from leo_chat.execution import _auto_continue_if_truncated

    existing = "line1\nline2"
    continuation = "line2\nline3\n# <<<END>>>"
    expected = "line1\nline2\nline3\n# <<<END>>>"

    page = _FakeLeoPage(continuation=continuation)
    result = await _auto_continue_if_truncated(
        page,
        existing,
        max_continues=3,
        is_truncated=lambda text: "<<<END>>>" not in text,
        patch_mode=True,
    )
    assert result == expected


async def test_non_advancing_continuation_discarded_patch_mode():
    """A patch continuation that adds no new bytes stops the loop without appending."""
    from leo_chat.execution import _auto_continue_if_truncated

    # existing already contains the full tail; the "continuation" is a re-send
    # of that same tail with no new content.
    existing = "def f():\n    return x"
    continuation = "def f():\n    return x"  # identical → overlap strip yields ""
    page = _FakeLeoPage(continuation=continuation)

    result = await _auto_continue_if_truncated(
        page,
        existing,
        max_continues=3,
        is_truncated=lambda text: True,  # force at least one continuation attempt
        patch_mode=True,
    )

    # No duplication: the non-advancing continuation was discarded.
    assert result == existing
    # Exactly one continuation message was attempted (loop broke, not spun max_continues).
    assert len(page.sent) == 1


async def test_non_advancing_continuation_discarded_prose_mode():
    """Whitespace-only prose continuation is discarded, not appended as a blank line."""
    from leo_chat.execution import _auto_continue_if_truncated

    existing = "The quick brown fox."
    page = _FakeLeoPage(continuation="   ")  # whitespace only

    result = await _auto_continue_if_truncated(
        page,
        existing,
        max_continues=3,
        is_truncated=lambda text: True,
        patch_mode=False,
    )

    assert result == existing
    assert len(page.sent) == 1


def test_strip_overlap_detects_short_overlap_exact():
    """Overlaps < 100 chars must be detected (old range step=50 min was 100)."""
    from leo_chat.execution import _strip_overlap

    existing = "abcdefghij"          # tail 'hij' is 3 chars
    continuation = "hij and the rest"
    assert _strip_overlap(existing, continuation, exact=True) == " and the rest"


def test_strip_overlap_short_overlap_prose():
    from leo_chat.execution import _strip_overlap

    existing = "...the quick brown fox"
    continuation = "fox jumps over"
    assert _strip_overlap(existing, continuation, exact=False) == " jumps over"


def test_strip_overlap_exact_preserves_whitespace():
    """exact=True must not strip whitespace from the matched tail."""
    from leo_chat.execution import _strip_overlap

    existing = "code\n    return x   "
    continuation = "    return x   \nmore"
    # The whole trailing-whitespace tail is the overlap; only 'more' remains.
    assert _strip_overlap(existing, continuation, exact=True) == "\nmore"


async def test_structured_completion_does_not_truncate_planner_json(monkeypatch):
    """Planner JSON is served via the coherent completion path, returned whole."""
    import json
    import leo_chat.db.leo_read as leo_read
    from leo_chat.execution import _poll_completion_for_structured

    payload = json.dumps({"steps": [{"id": i, "desc": "x" * 50} for i in range(40)]})

    captured = {}

    def fake_wait_for_completion(conversation_uuid, expected_min_rowid=-1, timeout=180, required_sentinel=None):
        captured["uuid"] = conversation_uuid
        captured["min_rowid"] = expected_min_rowid
        captured["timeout"] = timeout
        captured["sentinel"] = required_sentinel
        return payload

    monkeypatch.setattr(leo_read, "wait_for_completion", fake_wait_for_completion)

    page = _FakeLeoPage(uuid="planner-conv")
    result = await _poll_completion_for_structured(page, expected_min_rowid=6886, timeout=180)

    # Full JSON returned and parseable (not truncated by a streaming heuristic).
    assert json.loads(result) == json.loads(payload)
    assert captured["uuid"] == "planner-conv"
    assert captured["min_rowid"] == 6886
    assert captured["timeout"] >= 180

def test_wait_for_completion_anchors_by_rowid_and_stabilizes(monkeypatch):
    from leo_chat.db import leo_read
    from leo_chat.db.leo_read import AssistantState

    seen_rowids = []
    _call_count = [0]

    def fake_state(conversation_uuid, expected_min_rowid=-1):
        seen_rowids.append(expected_min_rowid)
        _call_count[0] += 1
        return AssistantState(
            state=AssistantState.State.COMPLETE,
            text="final answer",
            entry_uuid="entry-1",
        )

    # Advance time by 1.0s per call so the loop never hangs if stable
    # logic exits early (which it should after stable_needed polls).
    _t = [0.0]
    def _tick():
        _t[0] += 1.0
        return _t[0]

    class _FakeTime:
        @staticmethod
        def time():
            return _tick()
        @staticmethod
        def sleep(_):
            pass

    monkeypatch.setattr(leo_read, "latest_assistant_state", fake_state)
    monkeypatch.setattr(leo_read, "time", _FakeTime)

    result = leo_read.wait_for_completion(
        conversation_uuid="conv-1",
        expected_min_rowid=42,
        timeout=180,
    )
    assert result == "final answer", (
        f"Expected 'final answer', got {result!r}. "
        f"fake_state called {_call_count[0]} times, rowids seen: {seen_rowids}"
    )
    # Anchor: every poll used the supplied rowid baseline.
    assert all(r == 42 for r in seen_rowids), f"Bad rowids: {seen_rowids}"
    # Sanity: stabilization required at least stable_needed=2 polls.
    assert _call_count[0] >= 2, f"Expected ≥2 polls, got {_call_count[0]}"


def test_wait_for_completion_no_row_fast_fail(monkeypatch):
    """Persistent MISSING raises LeoNoRowTimeout after ~no_row_timeout seconds."""
    import pytest
    from leo_chat.db import leo_read
    from leo_chat.db.leo_read import AssistantState, LeoNoRowTimeout

    def fake_state(conversation_uuid, expected_min_rowid=-1):
        return AssistantState(
            state=AssistantState.State.MISSING,
            text=None,
            entry_uuid=None,
        )

    _t = [0.0]
    def _tick():
        _t[0] += 1.0
        return _t[0]

    class _FakeTime:
        @staticmethod
        def time():
            return _tick()
        @staticmethod
        def sleep(_):
            pass

    monkeypatch.setattr(leo_read, "latest_assistant_state", fake_state)
    monkeypatch.setattr(leo_read, "time", _FakeTime)

    with pytest.raises(LeoNoRowTimeout):
        leo_read.wait_for_completion(
            conversation_uuid="conv-missing",
            expected_min_rowid=-1,
            timeout=180,
            no_row_timeout=3.0,
        )