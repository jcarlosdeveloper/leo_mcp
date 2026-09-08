"""Decision matrix for ask_leo_skill: when disk writes activate and when they must not.

execute_leo_flow_with_robust_patches is mocked so no real Leo call is made; only
routing logic is exercised (write mode, per-skill opt-in, single-file guard,
planner isolation).
"""

import json
from unittest.mock import patch, AsyncMock

import pytest

import leo_mcp_server as srv


def _call(**kwargs):
    """Invoke ask_leo_skill and return its parsed JSON result."""
    import asyncio
    return json.loads(asyncio.get_event_loop().run_until_complete(
        srv.ask_leo_skill(**kwargs)
    ))


@pytest.fixture
def mock_flow():
    """Mock execute_leo_flow_with_robust_patches to return a canned patch envelope and uuid."""
    payload = (
        "<<<LEO_PATCH>>>\n"
        "FILE: /tmp/leo_test/x.py\n"
        "ACTION: modify\n"
        "RISK: LOW\n"
        "SUMMARY: stub\n"
        "<<<CONTENT>>>\n"
        "print('x')\n"
        "<<<END_LEO_PATCH>>>"
    )
    with patch.object(
        srv, "execute_leo_flow_with_robust_patches",
        new=AsyncMock(return_value=(payload, "uuid-1")),
    ) as m:
        yield m


@pytest.mark.asyncio
async def test_disabled_never_writes(mock_flow):
    """With write mode disabled, a patch skill never returns a write summary.

    code_refiner auto-streams, so the response is a backgrounded "working"
    handle (no "mode"), never a write/dry_run summary and never "error".
    """
    with patch.object(srv, "WRITE_MODE", "disabled"):
        out = json.loads(await srv.ask_leo_skill(
            skill_name="code_refiner", prompt="x", filepaths=["/tmp/leo_test/x.py"]
        ))
    assert out["status"] in ("working", "success", "still_working", "busy")
    assert out.get("mode") not in ("write", "dry_run")


@pytest.mark.asyncio
async def test_editor_never_writes_when_disabled_config(mock_flow):
    """senior_planner does not opt into patch output, so it never writes.

    senior_planner auto-streams, so the response is a backgrounded "working"
    handle (no "mode"), never a write/dry_run summary and never "error".
    """
    with patch.object(srv, "WRITE_MODE", "enabled"):
        out = json.loads(await srv.ask_leo_skill(
            skill_name="senior_planner", prompt="plan", filepaths=["/tmp/leo_test/x.py"]
        ))
    assert out["status"] in ("working", "success", "still_working", "busy")
    assert out.get("mode") not in ("write", "dry_run")


@pytest.mark.asyncio
async def test_refiner_multi_file_rejected(mock_flow):
    """Patch mode enforces exactly one file per request."""
    with patch.object(srv, "WRITE_MODE", "enabled"):
        out = json.loads(await srv.ask_leo_skill(
            skill_name="code_refiner", prompt="x",
            filepaths=["/tmp/a.py", "/tmp/b.py"]
        ))
    assert out["status"] == "error"
    assert "one file" in out["error"]
