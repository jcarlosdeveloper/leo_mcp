#!/usr/bin/env python3
"""
Tests for leo_new_project — the project-bootstrap tool and its pure helpers.

Covers:
- _extract_plan_json: fenced / unfenced / malformed extraction
- _plan_is_actionable: empty steps, needs_user_decision present, both clear
- _map_plan_v1_to_kanban: risk->priority, depends_on->parents, dangling-edge
  dropping (keep the task, drop only the edge, warn with specifics)
- _render_agents_md: minimality (no ARCHITECTURE content leaks in)
- _render_kanban_init_md: manifest carries warnings under ## Open Questions
"""

import json

import leo_mcp_server as srv

# ==============================================================================
# _extract_plan_json
# ==============================================================================


def test_extract_plan_json_fenced():
    """Fenced ```json block is parsed."""
    content = 'prefix\n```json\n{"schema_version":"plan_v1","goal":"x"}\n```\nsuffix'
    result = srv._extract_plan_json(content)
    assert result == {"schema_version": "plan_v1", "goal": "x"}


def test_extract_plan_json_unfenced():
    """Unfenced JSON object is parsed."""
    content = 'here is the plan {"schema_version":"plan_v1","steps":[{"id":0}]}'
    result = srv._extract_plan_json(content)
    assert result["schema_version"] == "plan_v1"
    assert result["steps"] == [{"id": 0}]


def test_extract_plan_json_malformed_returns_empty():
    """Malformed JSON returns {} instead of raising."""
    assert srv._extract_plan_json("not json at all") == {}
    assert srv._extract_plan_json("```json\n{broken\n```") == {}


def test_extract_plan_json_empty_returns_empty():
    """Empty content returns {}."""
    assert srv._extract_plan_json("") == {}
    assert srv._extract_plan_json(None) == {}


# ==============================================================================
# _plan_is_actionable
# ==============================================================================


def test_plan_actionable_when_steps_and_no_nud():
    plan = {"steps": [{"id": 0}], "needs_user_decision": []}
    assert srv._plan_is_actionable(plan) is True


def test_plan_not_actionable_when_empty_steps():
    plan = {"steps": [], "needs_user_decision": []}
    assert srv._plan_is_actionable(plan) is False


def test_plan_not_actionable_when_nud_present():
    plan = {"steps": [{"id": 0}], "needs_user_decision": ["which db?"]}
    assert srv._plan_is_actionable(plan) is False


def test_plan_not_actionable_when_both_bad():
    plan = {"steps": [], "needs_user_decision": ["which db?"]}
    assert srv._plan_is_actionable(plan) is False


# ==============================================================================
# _map_plan_v1_to_kanban
# ==============================================================================

_SAMPLE_PLAN = {
    "schema_version": "plan_v1",
    "goal": "Build a CLI tool",
    "assumptions": ["Python 3.12"],
    "steps": [
        {
            "id": 0,
            "description": "Scaffold the package",
            "targets": ["src/__init__.py"],
            "depends_on": [],
            "risk": "LOW",
            "done_when": "python -c 'import src'",
        },
        {
            "id": 1,
            "description": "Implement core logic",
            "targets": ["src/core.py"],
            "depends_on": [0],
            "risk": "HIGH",
            "done_when": "pytest",
        },
        {
            "id": 2,
            "description": "Add CLI entrypoint",
            "targets": ["src/cli.py"],
            "depends_on": [1, 999],  # 999 is dangling
            "risk": "MEDIUM",
            "done_when": "src/cli.py --help",
        },
    ],
    "risks": [{"risk": "API drift", "mitigation": "pin versions"}],
    "needs_user_decision": [],
}


def test_map_keeps_all_tasks():
    km = srv._map_plan_v1_to_kanban(_SAMPLE_PLAN, "board", "default", "scratch")
    assert len(km["tasks"]) == 3


def test_map_risk_to_priority():
    km = srv._map_plan_v1_to_kanban(_SAMPLE_PLAN, "board", "default", "scratch")
    by_id = {t["local_id"]: t for t in km["tasks"]}
    assert by_id["task-0"]["priority"] == 3  # LOW
    assert by_id["task-1"]["priority"] == 5  # HIGH
    assert by_id["task-2"]["priority"] == 4  # MEDIUM


def test_map_depends_on_to_parents():
    km = srv._map_plan_v1_to_kanban(_SAMPLE_PLAN, "board", "default", "scratch")
    by_id = {t["local_id"]: t for t in km["tasks"]}
    assert by_id["task-0"]["parents"] == []
    assert by_id["task-1"]["parents"] == ["task-0"]


def test_map_drops_dangling_edge_keeps_task():
    """Dangling depends_on drops ONLY the edge, keeps the task, warns with ids."""
    km = srv._map_plan_v1_to_kanban(_SAMPLE_PLAN, "board", "default", "scratch")
    by_id = {t["local_id"]: t for t in km["tasks"]}
    # task-2 depends on [1, 999]; 999 is unresolvable → only task-1 survives.
    assert by_id["task-2"]["parents"] == ["task-1"]
    # The task itself is still emitted.
    assert "task-2" in by_id
    # The warning names the exact dropped id and the source step.
    assert any("999" in w and "step 2" in w for w in km["warnings"])


def test_map_done_when_verbatim():
    """done_when passes through verbatim (no shell assumption)."""
    km = srv._map_plan_v1_to_kanban(_SAMPLE_PLAN, "board", "default", "scratch")
    by_id = {t["local_id"]: t for t in km["tasks"]}
    assert by_id["task-0"]["acceptance"] == ["python -c 'import src'"]


def test_map_default_priority_on_unknown_risk():
    plan = {
        "steps": [{"id": 0, "description": "x", "targets": ["a.py"], "risk": "WEIRD"}],
        "needs_user_decision": [],
    }
    km = srv._map_plan_v1_to_kanban(plan, "board", "default", "scratch")
    assert km["tasks"][0]["priority"] == 4  # default


def test_map_idempotency_prefix():
    km = srv._map_plan_v1_to_kanban(_SAMPLE_PLAN, "myboard", "default", "scratch")
    assert km["idempotency_prefix"].startswith("myboard-")


def test_map_zero_targets_uses_description_title():
    """A step with no targets keeps its description as the title."""
    plan = {"steps": [{"id": 0, "description": "do the thing",
                       "depends_on": [], "risk": "LOW"}],
            "needs_user_decision": []}
    km = srv._map_plan_v1_to_kanban(plan, "board", "default", "scratch")
    t = km["tasks"][0]
    assert t["targets"] == []          # zero targets preserved verbatim
    assert t["title"] == "do the thing"


def test_map_multiple_targets_all_preserved():
    """Multiple targets are ALL preserved in targets AND listed in the title."""
    plan = {"steps": [{"id": 0, "description": "wire it up",
                       "targets": ["a.py", "b.py", "c.py"],
                       "depends_on": [], "risk": "LOW"}],
            "needs_user_decision": []}
    km = srv._map_plan_v1_to_kanban(plan, "board", "default", "scratch")
    t = km["tasks"][0]
    assert t["targets"] == ["a.py", "b.py", "c.py"]   # none dropped
    assert t["title"] == "Implement a.py, b.py, c.py"  # all in the label


# ==============================================================================
# _render_agents_md
# ==============================================================================


def test_render_agents_md_is_minimal():
    km = srv._map_plan_v1_to_kanban(_SAMPLE_PLAN, "board", "default", "scratch")
    md = srv._render_agents_md(km, "factcheck text")
    assert "# AGENTS.md" in md
    assert "## Goal" in md
    assert "## Decided" in md
    assert "## Verified External Facts" in md
    assert "factcheck text" in md
    assert "## Risks" in md
    assert "Worker Contract" in md  # static core present


# ==============================================================================
# _render_kanban_init_md
# ==============================================================================


def test_render_kanban_init_md_has_tasks_and_warnings():
    plan = json.loads(json.dumps(_SAMPLE_PLAN))  # deep copy
    km = srv._map_plan_v1_to_kanban(plan, "board", "default", "scratch")
    md = srv._render_kanban_init_md(km)
    assert "# KANBAN.init.md" in md
    assert "## Tasks" in md
    assert "### task-0" in md
    assert "## Open Questions / Warnings" in md
    assert "999" in md  # warning specifics surfaced in the manifest


def test_render_kanban_init_md_no_warnings_section_when_clean():
    plan = {
        "goal": "g",
        "assumptions": [],
        "risks": [],
        "steps": [{"id": 0, "description": "x", "targets": ["a.py"], "depends_on": [],
                   "risk": "LOW", "done_when": "pytest"}],
    }
    km = srv._map_plan_v1_to_kanban(plan, "board", "default", "scratch")
    assert km["warnings"] == []
    md = srv._render_kanban_init_md(km)
    assert "## Open Questions / Warnings" not in md
