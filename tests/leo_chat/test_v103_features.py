#!/usr/bin/env python3
"""
Tests for v10.3 features — skills, context injection, streaming, cache, file format, models.

Covers:
- t1: code_refiner skill registration and prompt building
- t2: project context auto-injection (AGENTS.md, pyproject.toml, dedup)
- t3: streaming auto-detection heuristics
- t4: cache key generation with mtime sensitivity
- t5: file injection with line numbers, language detection, size formatting
- t6: models.toml loading and built-in fallback
"""

import hashlib
import time
from pathlib import Path

import pytest


# ==============================================================================
# t1: code_refiner skill
# ==============================================================================


def test_code_refiner_skill_registered():
    """code_refiner skill is registered in the factory."""
    from leo_chat.skills.skill_factory import SkillFactory

    skills = SkillFactory.list_skills()
    assert "code_refiner" in skills, (
        f"code_refiner not found in skills: {list(skills.keys())}"
    )


def test_code_refiner_uses_opus_model():
    """code_refiner uses chat-claude-opus by default."""
    from leo_chat.skills.skill_factory import SkillFactory

    skill = SkillFactory.get_skill("code_refiner")
    assert skill.model_key == "chat-claude-opus", (
        f"Expected chat-claude-opus, got {skill.model_key}"
    )


def test_code_refiner_prompt_building():
    """code_refiner builds a prompt with context and user input."""
    from leo_chat.skills.skill_factory import SkillFactory

    timestamp = int(time.time())
    skill = SkillFactory.get_skill("code_refiner")
    prompt = skill.get_full_prompt(
        f"Refactor the parser module (ts={timestamp})",
        "Context: legacy Python 2 code",
    )
    assert "Refactor the parser module" in prompt
    assert "legacy Python 2 code" in prompt
    assert "<system>" in prompt
    assert len(prompt) > 200, f"Prompt too short: {len(prompt)} chars"


# ==============================================================================
# t2: project context auto-injection
# ==============================================================================


def test_project_context_auto_injects_agents_md():
    """When requesting a file in leo_mcp, AGENTS.md is auto-injected."""
    from leo_chat.context.prompt_builder import assemble_context

    ctx = assemble_context(["leo_mcp_server.py"])
    assert "PROJECT CONTEXT (auto-injected)" in ctx, "Missing PROJECT CONTEXT section"
    assert "AGENTS.md" in ctx, "AGENTS.md not auto-injected"
    assert "pyproject.toml" in ctx, "pyproject.toml not auto-injected"


def test_project_context_no_duplication():
    """When user explicitly requests AGENTS.md, it does not appear in PROJECT CONTEXT."""
    from leo_chat.context.prompt_builder import assemble_context

    ctx = assemble_context(["AGENTS.md", "leo_mcp_server.py"])
    # Count PROJECT FILE headers for AGENTS.md
    project_headers = [
        line for line in ctx.split("\n") if "PROJECT FILE: AGENTS.md" in line
    ]
    assert len(project_headers) == 0, (
        f"AGENTS.md should not appear in PROJECT CONTEXT when user requested it: "
        f"found {len(project_headers)}"
    )


def test_project_context_empty_filepaths():
    """Empty filepaths produce empty context (no crash)."""
    from leo_chat.context.prompt_builder import assemble_context

    ctx = assemble_context([])
    assert ctx == "", f"Expected empty string, got {len(ctx)} chars"


# ==============================================================================
# t3: streaming auto-detection
# ==============================================================================


# Inline the function since we can't import leo_mcp_server without lock acquisition.
def _auto_detect_stream(skill_name: str, prompt: str, filepaths: list) -> bool:
    _STREAMING_SKILLS = {"senior_planner", "code_refiner"}
    _STREAMING_KEYWORDS = [
        "explain", "refactor", "review", "plan", "design", "architect",
        "analyze", "document", "compare", "evaluate", "summarize",
        "optimize", "restructure", "migrate",
    ]
    _SHORT_KEYWORDS = [
        "fix", "find", "where", "what", "how do i", "show me",
        "locate", "identify", "quick", "one line", "syntax",
    ]
    prompt_lower = prompt.lower()
    if skill_name in _STREAMING_SKILLS:
        return True
    if any(kw in prompt_lower for kw in _STREAMING_KEYWORDS):
        if not any(sk in prompt_lower for sk in _SHORT_KEYWORDS):
            return True
    if filepaths and len(filepaths) >= 3:
        return True
    return False


def test_streaming_long_form_skills():
    """senior_planner and code_refiner always stream."""
    assert _auto_detect_stream("senior_planner", "anything", []) is True
    assert _auto_detect_stream("code_refiner", "whatever", []) is True


def test_streaming_long_keyword_triggers():
    """Prompts with 'explain', 'refactor', etc. trigger streaming."""
    assert _auto_detect_stream("code_editor", "explain the architecture", []) is True
    assert _auto_detect_stream("code_editor", "please refactor this", []) is True


def test_streaming_short_keyword_suppresses():
    """Short keywords like 'fix' suppress streaming even with long keywords."""
    assert _auto_detect_stream("code_editor", "fix the bug", []) is False
    assert _auto_detect_stream("code_editor", "explain how to fix this", []) is False


def test_streaming_heavy_files():
    """3+ files trigger streaming."""
    assert _auto_detect_stream("code_editor", "check", ["a.py", "b.py", "c.py"]) is True


def test_streaming_default_short():
    """Default: no streaming for generic short prompts."""
    assert _auto_detect_stream("code_editor", "what is python", []) is False
    assert _auto_detect_stream("code_editor", "find the error", []) is False


# ==============================================================================
# t4: cache key generation
# ==============================================================================


def _make_cache_key(skill_name: str, prompt: str, filepaths: list) -> str:
    parts = [skill_name, prompt]
    for fp in sorted(filepaths or []):
        try:
            p = Path(fp).expanduser().resolve()
            mtime = str(int(p.stat().st_mtime)) if p.exists() else "MISSING"
        except Exception:
            mtime = "ERROR"
        parts.append(f"{fp}:{mtime}")
    combined = "::".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def test_cache_key_same_input_same_key():
    """Same skill + prompt + files → same cache key."""
    k1 = _make_cache_key("senior_planner", "fix bug", ["leo_mcp_server.py"])
    k2 = _make_cache_key("senior_planner", "fix bug", ["leo_mcp_server.py"])
    assert k1 == k2, "Same inputs should produce same key"


def test_cache_key_different_prompt_different_key():
    """Different prompt → different cache key."""
    k1 = _make_cache_key("senior_planner", "fix bug A", ["leo_mcp_server.py"])
    k2 = _make_cache_key("senior_planner", "fix bug B", ["leo_mcp_server.py"])
    assert k1 != k2


def test_cache_key_different_skill_different_key():
    """Different skill → different cache key."""
    k1 = _make_cache_key("senior_planner", "fix bug", [])
    k2 = _make_cache_key("code_refiner", "fix bug", [])
    assert k1 != k2


def test_cache_key_no_files_consistent():
    """Empty filepaths produce consistent keys."""
    k1 = _make_cache_key("code_editor", "hello", [])
    k2 = _make_cache_key("code_editor", "hello", [])
    assert k1 == k2


def test_cache_key_missing_file_handled():
    """Missing file path is handled gracefully (key includes 'MISSING')."""
    k = _make_cache_key("senior_planner", "fix", ["/nonexistent/path.py"])
    assert len(k) == 64, f"Expected 64-char hex digest, got {len(k)}"


def test_cache_key_mtime_sensitivity():
    """Key changes when file mtime changes (simulated with a temp file)."""
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
        f.write(b"original content\n")
        tmp_path = f.name

    try:
        k1 = _make_cache_key("skill", "prompt", [tmp_path])

        # Modify mtime explicitly (some filesystems have 1s granularity)
        new_mtime = os.stat(tmp_path).st_mtime + 10
        os.utime(tmp_path, (new_mtime, new_mtime))

        k2 = _make_cache_key("skill", "prompt", [tmp_path])
        assert k1 != k2, (
            "Cache key should change when file mtime changes"
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ==============================================================================
# t5: file injection enhancements (line numbers, language, size)
# ==============================================================================


def test_file_injection_has_metadata_header():
    """Injected files include File, Language, Lines, Size metadata."""
    from leo_chat.context.prompt_builder import assemble_context

    ctx = assemble_context(["leo_mcp_server.py"])
    assert "# File: leo_mcp_server.py" in ctx
    assert "# Language: Python" in ctx
    assert "# Lines:" in ctx
    assert "# Size:" in ctx


def test_file_injection_has_line_numbers():
    """Injected files have 5-digit line numbers."""
    from leo_chat.context.prompt_builder import assemble_context

    ctx = assemble_context(["leo_mcp_server.py"])
    # First line should be "    1|..."
    lines = ctx.split("\n")
    numbered_lines = [l for l in lines if l.startswith("    1|")]
    assert len(numbered_lines) >= 1, "No line-numbered content found"


def test_language_detection_python():
    """Python files are detected correctly."""
    from leo_chat.context.prompt_builder import _detect_language

    assert _detect_language(Path("main.py")) == "Python"
    assert _detect_language(Path("src/module.py")) == "Python"


def test_language_detection_various():
    """Various extensions map to correct languages."""
    from leo_chat.context.prompt_builder import _detect_language

    cases = [
        ("app.js", "JavaScript"),
        ("types.ts", "TypeScript"),
        ("main.rs", "Rust"),
        ("server.go", "Go"),
        ("Dockerfile", "Dockerfile"),
        ("Makefile", "Makefile"),
        ("AGENTS.md", "Markdown"),
        ("config.toml", "TOML"),
        ("data.json", "JSON"),
        ("style.css", "CSS"),
        ("index.html", "HTML"),
        ("vars.env", "Environment"),
    ]
    for filename, expected in cases:
        assert _detect_language(Path(filename)) == expected, (
            f"{filename}: expected {expected}"
        )


def test_language_detection_unknown():
    """Unknown extensions return 'Unknown (.ext)'."""
    from leo_chat.context.prompt_builder import _detect_language

    result = _detect_language(Path("data.xyzzy"))
    assert "Unknown" in result


def test_format_size():
    """File sizes are formatted in human-readable form."""
    from leo_chat.context.prompt_builder import _format_size

    assert _format_size(0) == "0 B"
    assert _format_size(512) == "512 B"
    assert _format_size(2048) == "2.0 KB"
    assert _format_size(1048576) == "1.0 MB"
    assert _format_size(1572864) == "1.5 MB"


# ==============================================================================
# t6: models.toml registry
# ==============================================================================


def test_models_toml_loads_all_entries():
    """models.toml loads all 15 models correctly."""
    from leo_chat.pages.brave_leo_page import _load_model_registry

    # Clear cache to force fresh load
    import leo_chat.pages.brave_leo_page as blp
    blp._model_registry_cache = None

    registry = _load_model_registry()
    assert len(registry) >= 15, f"Expected 15+ models, got {len(registry)}"
    assert registry["chat-claude-sonnet"] == "claude sonnet"
    assert registry["chat-claude-opus"] == "claude opus"
    assert registry["chat-deepseek-v3-1"] == "deepseek v3.1"
    assert registry["chat-qwen-3-coder-480b"] == "qwen 3 coder 480b"


def test_models_toml_cache_is_used():
    """Registry is cached after first load."""
    from leo_chat.pages.brave_leo_page import _load_model_registry

    import leo_chat.pages.brave_leo_page as blp
    blp._model_registry_cache = None

    r1 = _load_model_registry()
    r2 = _load_model_registry()
    # Same object in memory (cached)
    assert r1 is r2, "Registry should be the same cached object"


def test_models_toml_fallback_on_missing():
    """If models.toml missing, fallback includes core models."""
    # Simulate missing file by clearing cache and pointing to nonexistent path
    import leo_chat.pages.brave_leo_page as blp

    blp._model_registry_cache = None
    original_path = blp._MODEL_REGISTRY_PATH
    blp._MODEL_REGISTRY_PATH = Path("/nonexistent/models.toml")

    try:
        registry = blp._load_model_registry()
        # Fallback minimal set
        assert "chat-automatic" in registry
        assert "chat-claude-sonnet" in registry
        assert "chat-claude-opus" in registry
    finally:
        blp._MODEL_REGISTRY_PATH = original_path
        blp._model_registry_cache = None


# ==============================================================================
# v10.4: Chunking, truncation detection, conversation resume
# ==============================================================================


def test_split_prompt_at_boundaries_paragraphs():
    """Split at paragraph boundaries (double newlines)."""
    from leo_chat.pages.brave_leo_page import BraveLeoPage

    text = "Line 1\n\nLine 2 with more content here.\nLine 3.\n\nLine 4 final."
    chunks = BraveLeoPage._split_prompt_at_boundaries(text, 30)
    assert all(len(c) <= 30 for c in chunks), "No chunk should exceed max"
    assert len(chunks) == 3, f"Expected 3 chunks, got {len(chunks)}"


def test_split_prompt_at_boundaries_large_homogeneous():
    """Large homogeneous text splits correctly without data loss."""
    from leo_chat.pages.brave_leo_page import BraveLeoPage

    big = "A" * 40000
    chunks = BraveLeoPage._split_prompt_at_boundaries(big, 18000)
    assert max(len(c) for c in chunks) <= 18000
    assert sum(len(c) for c in chunks) == 40000, "No data loss"


def test_split_prompt_at_boundaries_edge_cases():
    """Edge cases: empty, short, exact boundary."""
    from leo_chat.pages.brave_leo_page import BraveLeoPage

    assert BraveLeoPage._split_prompt_at_boundaries("", 100) == []
    assert BraveLeoPage._split_prompt_at_boundaries("short", 100) == ["short"]


def test_detect_truncation_unclosed_fence():
    """Odd number of ``` markers → truncated."""
    from leo_chat.helpers import _detect_truncation

    text = "Here is the fix:\n```python\ndef foo():\n    return 42"
    assert _detect_truncation(text) is True


def test_detect_truncation_ellipsis():
    """Trailing ellipsis → truncated."""
    from leo_chat.helpers import _detect_truncation

    assert _detect_truncation("The answer is...") is True


def test_detect_truncation_mid_sentence():
    """Ends with alphanumeric mid-sentence → truncated."""
    from leo_chat.helpers import _detect_truncation

    assert _detect_truncation("The solution involves using the") is True


def test_detect_truncation_complete():
    """Complete response with proper punctuation → not truncated."""
    from leo_chat.helpers import _detect_truncation

    text = "Here is the fix:\n```python\ndef foo():\n    return 42\n```\n\nDone."
    assert _detect_truncation(text) is False


def test_detect_truncation_empty():
    """Empty text → not truncated."""
    from leo_chat.helpers import _detect_truncation

    assert _detect_truncation("") is False
    assert _detect_truncation("   ") is False


def test_detect_truncation_unclosed_bold():
    """Unclosed **bold** marker → truncated."""
    from leo_chat.helpers import _detect_truncation

    assert _detect_truncation("This is **very important") is True


def test_detect_truncation_unclosed_italic():
    """Unclosed *italic* marker → truncated."""
    from leo_chat.helpers import _detect_truncation

    assert _detect_truncation("Note: *this needs attention") is True


def test_detect_truncation_closed_bold():
    """Closed **bold** marker → not truncated."""
    from leo_chat.helpers import _detect_truncation

    assert _detect_truncation("This is **very important** indeed.") is False


def test_detect_truncation_truncated_list():
    """Last line is a bullet list item → truncated."""
    from leo_chat.helpers import _detect_truncation

    text = "Changes needed:\n- Add validation\n- Fix the parser"
    assert _detect_truncation(text) is True


def test_detect_truncation_truncated_numbered_list():
    """Last line is a numbered list item → truncated."""
    from leo_chat.helpers import _detect_truncation

    text = "Steps:\n1. Install deps\n2. Run migrations\n3. Deploy"
    assert _detect_truncation(text) is True


def test_detect_truncation_complete_list():
    """List followed by conclusion → not truncated."""
    from leo_chat.helpers import _detect_truncation

    text = "Options:\n- Option A\n- Option B\n\nThe best choice is Option A."
    assert _detect_truncation(text) is False


def test_detect_truncation_url_not_truncated():
    """URL at end is not mid-sentence truncation."""
    from leo_chat.helpers import _detect_truncation

    assert _detect_truncation(
        "See the docs at https://example.com/docs"
    ) is False


def test_leo_max_prompt_chars_constant():
    """Chunking threshold is set at 18k (conservative, observed cap ~20k)."""
    from leo_chat.pages.brave_leo_page import BraveLeoPage

    assert BraveLeoPage.LEO_MAX_PROMPT_CHARS == 18_000