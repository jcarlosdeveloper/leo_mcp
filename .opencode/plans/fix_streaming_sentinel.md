# Fix Plan: Streaming Path Missing Terminal Token Check for Structured Skills

## Problem
The `senior_planner` skill uses SQLite streaming (`stream_response`) which returns when text stabilizes (3 consecutive identical polls) but **doesn't wait for the terminal token** (`<<<LEO_DONE>>>`). This causes false "complete" signals, triggering auto-continue loops.

## Root Cause
- `senior_planner` is in `_STREAMING_SKILLS` → always uses streaming path
- `stream_response` in `leo_read.py` only checks text stabilization, not sentinel presence
- Structured path (`wait_for_completion`) correctly waits for sentinel via `required_sentinel` param
- Mismatch causes `_plan_incomplete()` to find no `<<<LEO_DONE>>>` and trigger continuation

## Fix
1. Add `required_sentinel` parameter to `stream_response()` in `leo_read.py`
2. Add sentinel check in stabilization logic (mirror `wait_for_completion` behavior)
3. Update `_poll_sqlite_for_response_streaming()` in `execution.py` to pass the sentinel

## Files to Modify
- `/Users/juancarlos/Downloads/leo_mcp/leo_chat/db/leo_read.py` - Add sentinel parameter and check
- `/Users/juancarlos/Downloads/leo_mcp/leo_chat/execution.py` - Pass sentinel from `_structured_sentinel(skill)`

## Testing
- Run existing tests: `pytest tests/ -v`
- Verify multi-turn conversation test passes
- Verify senior_planner streaming path no longer triggers false continuations