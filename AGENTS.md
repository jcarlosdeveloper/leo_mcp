# AGENTS.md - Leo MCP Server v10.3

Coding agent instructions for Leo MCP Server.

---

## Dev Environment Tips

- **Brave requirement**: Must run with `--remote-debugging-port=9222`
- **Python version**: 3.12+ required (syntax: `bytes | None`)
- **Virtualenv**: Always use `.venv` - never global Python
- **Health check**: Server auto-verifies on startup (Keychain, Brave, port 9222, DB)

### Quick Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .  # Optional: install as editable package
```

`requirements.txt` is a generated lock file. `pyproject.toml` is the sole
source of truth for dependencies. Regenerate the lock after any change:

```bash
uv pip compile pyproject.toml -o requirements.txt
```

### Launch Brave with Debug Port
```bash
# One-time alias (add to .zshrc)
alias brave-debug='BRAVE=/Applications/Brave\ Browser.app/Contents/MacOS/Brave\ Browser; $BRAVE --remote-debugging-port=9222 --user-data-dir=/tmp/leo-debug-profile &'

brave-debug  # Launch Brave
python3 leo_mcp_server.py  # Start MCP server
```

---

## Testing Instructions

### Run All Tests
```bash
source .venv/bin/activate
pytest tests/ -v
```

### Test Categories
- **brave_search/**: Playwright headless tests
- **leo_chat/**: CDP, skills, context building, patch writer, registry, jobs
- **integration/**: MCP server, health check, ask_leo_skill decision matrix

### Focus on Specific Tests
```bash
# Run single test file
pytest tests/leo_chat/test_leo_chat_skills.py -v

# Run single test function
pytest tests/leo_chat/test_leo_chat_skills.py::test_prompt_building -v

# Run with output capture
pytest tests/integration/test_mcp_server.py -v -s
```

### Test Conventions
- **Dynamic prompts only**: Never hardcode prompts (anti-detection)
- **Timestamp rotation**: Use `prompt_rotation[timestamp % len(prompts)]`
- **Assert clearly**: `assert result, "Clear error message"`
- **Document expected failures**: Some tests document limitations (e.g., `brave://` URLs fail in headless)

### Example Test Pattern
```python
import time

def test_something():
    timestamp = int(time.time())
    prompt_rotation = [
        f"Prompt variant 1 (ts={timestamp})",
        f"Prompt variant 2 (run={timestamp % 1000})",
        f"Prompt variant 3 (batch={timestamp % 100})",
    ]
    user_prompt = prompt_rotation[timestamp % len(prompt_rotation)]
    
    # Test logic...
    assert len(results) > 0, "No results returned"
    return True
```

---

## Code Conventions

### Language: English Only
- **Comments**: English only (no Spanish)
- **Docstrings**: English only
- **Error messages**: English only
- **Variable names**: English only
- **Logs**: English only

**Why**: International team compatibility, consistency with Python ecosystem.

### Before/After Example
```python
# ❌ BEFORE (Spanish)
def get_key(force_reload: bool = False) -> bytes:
    """Obtiene la clave de Leo desde Keychain."""
    # Se carga UNA VEZ al iniciar
    if _cache is not None:
        return _cache  # Ya cargada

# ✅ AFTER (English)
def get_key(force_reload: bool = False) -> bytes:
    """Get Leo key from Keychain."""
    # Loaded ONCE at startup
    if _cache is not None:
        return _cache  # Already loaded
```

### Naming Conventions
- **Files**: `snake_case.py` (e.g., `brave_leo_page.py`)
- **Tests**: `test_<component>.py` (e.g., `test_mcp_server.py`)
- **Functions**: `snake_case()` (e.g., `assemble_context()`)
- **Classes**: `PascalCase` (e.g., `BraveLeoPage`, `SkillFactory`)

### Imports Order
```python
1. Standard library (os, sys, time, etc.)
2. Third-party (mcp, playwright, etc.)
3. Local modules (leo_chat.*)
```

### Logging
```python
from leo_chat.helpers import _log_json
_log_json("INFO", "event_type", key="value", extra="data")
# Output: {"timestamp": "...", "level": "INFO", "event_type": "...", ...}
```

### Patch-Mode Contract (ask_leo_skill)
- **code_refiner writes to disk by default** (LEO_WRITE_MODE defaults to enabled).
  It REQUIRES exactly one absolute path in `filepaths`; omitting it, or passing
  more than one, is rejected with `status: "error"` before any Leo round-trip.
  `filepaths` is NOT optional context for this skill.
- **code_editor writes to disk** too (minimal str_replace edits via a LEO_EDIT
  envelope — `apply_edit_patch`). Same single-`filepaths` contract as
  code_refiner. It enforces an exactly-one-match rule on `old_str`: zero or
  multiple matches fail with `status: "error"` and never write.
- Non-writing skills (senior_planner) treat `filepaths` as optional injected
  context only.
- Only `code_refiner` and `code_editor` opt into patch output. Keep
  `supports_patch_output` off for any new skill that must not write to disk.

---

## Selector & Model Registry Refactor (v10.x)

Extract hardcoded DOM selectors and model display names into TOML files so Brave
UI changes require zero code edits. Single source of truth, versioned schema,
full backward compatibility.

### Config Files (project root)
- `selectors.toml` — DOM selectors under `[selectors]`, with top-level
  `schema_version = 1`. Bump the version when the shape changes; the loader
  refuses unsupported versions.
- `models.toml` — model registry under `[models.<key>]`, mapping `model_key` →
  `name`.

### Code Modules
- `leo_chat/selectors.py` — `load_selectors()` / `get_selector()`. Cached, falls
  back to built-in defaults when the file is missing or schema-unsupported.
- `leo_chat/model_registry.py` — `load_model_registry(path, cache,
  force_reload)`. Pure function; fallback map when `models.toml` is absent.
- `leo_chat/pages/brave_leo_page.py` — POM. Consumes the TOML files but mirrors
  selectors as class attrs (`MODEL_BUTTON_SELECTOR`, `MODEL_ITEM`, `INPUT_FIELD`,
  `SEND_BUTTON`, `SHOW_ALL_MODELS`, `RESPONSE_SELECTOR`, `CONVERSATION_IFRAME`,
  legacy `MODEL_BUTTON`) and keeps the `_load_model_registry` shim
  (`_MODEL_REGISTRY_PATH`, `_model_registry_cache`) for backward compatibility.

### Status
The refactor is complete:
- [x] `brave_leo_page.py` consumes `selectors.py` / `model_registry.py`; the
      legacy `_load_model_registry` name is a thin backward-compat shim wrapping
      `load_model_registry` (no orphaned `Path` import, no circular import).
- [x] `leo_chat/__init__.py` exports `load_selectors`, `get_selector`,
      `load_model_registry` (plus `MODEL_REGISTRY_PATH`).
- [x] Backward compatibility verified via
      `tests/leo_chat/test_v103_features.py`,
      `tests/leo_chat/test_final_model_selection.py`, and
      `tests/leo_chat/test_tareas_leo_chat.py`.

### Known Caveats
- **Stale assertion**: `tests/leo_chat/test_tareas_leo_chat.py:91` expects
  `MODEL_BUTTON` to contain `leo-button[slot='anchor-content']`; the actual
  selector is `data-testid="anchor-button"`. Pre-existing, not caused by this refactor.

---

## PR Instructions

### Title Format
```
[leo-mcp] <Title>
Examples:
[leo-mcp] Add streaming support for UUID-specific responses
[leo-mcp] Fix key rotation detection in health check
[leo-mcp] Refactor helpers into modular structure
```

### Before Committing
```bash
# 1. Run tests
source .venv/bin/activate
pytest tests/ -v

# 2. Run linter (if configured)
ruff check .
ruff format .

# 3. Verify health check
python3 leo_mcp_server.py 2>&1 | grep -E "✅|❌"
# Expected: All 4 checks pass
```

### Commit Checklist
- [ ] Tests pass
- [ ] No hardcoded prompts (use dynamic rotation)
- [ ] English only (comments, docstrings, messages)
- [ ] Health check passes
- [ ] No legacy code references (`leo_ask.sh`, `AppleScript`)

---

## Troubleshooting

### Brave Not Responding on Port 9222
```bash
# Check if Brave is running
pgrep -x "Brave Browser"

# If not running, launch with debug port
brave-debug

# Verify port
lsof -i :9222
```

### `connect_over_cdp` times out — `ws connected` then silence
**Symptom**: `BrowserType.connect_over_cdp: Timeout 30000ms exceeded` with
`<ws connected>` appearing in the call log but no further protocol traffic. The
CDP endpoint itself is healthy (`curl http://localhost:9222/json/version`
returns instantly, and a raw WebSocket `Browser.getVersion` call succeeds).

**Cause (not a version/broken-dependency issue)**: Playwright's
`connect_over_cdp` calls `Target.setAutoAttach(autoAttach=true,
waitForDebuggerOnStart=true)` on every existing target and waits for each one to
resolve `Runtime.runIfWaitingForDebugger`. Brave's Leo AI conversation iframes
(`chrome-untrusted://leo-ai-conversation-entries/...`) and browser-extension
service workers (e.g. the `keepa`/`eimadpbc...` extensions) do not complete that
debugger-attach handshake the way plain Chrome pages do, so Playwright stalls.
The connection works right after a clean launch and breaks once a Leo
conversation tab and/or extension service workers are resident — so the failure
appears intermittently over a long-lived browser session.

**Fix**: fully quit Brave and relaunch it (fresh browser state clears the
wedged untrusted iframes / service workers), then restart the MCP server:
```bash
# Fully quit Brave (Cmd+Q or):
osascript -e 'quit app "Brave Browser"' && sleep 3
# Relaunch with the debug port (see the brave-debug alias in Dev Environment Tips)
brave-debug
# Restart the server
python3 leo_mcp_server.py
```
Reducing the number of resident tabs helps: close the Leo `chrome://leo-ai/`
tabs and any pages that emit a continuous stream of `Log.entryAdded` events
(e.g. an admin panel with many password/API-key inputs) before connecting.

### Health Check Fails on Keychain
```bash
# Steps to fix:
1. Unlock Mac (press any key)
2. Open Brave and use Leo once
3. System Settings → Privacy → Keychain → Brave
4. Restart leo_mcp_server.py
```

### Tests Fail with ModuleNotFoundError
```bash
# Activate venv
source .venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt

# Verify Python version (must be 3.12+)
python3 --version  # Must be >= 3.12
```

### Type Errors: `unsupported operand type(s) for |`
**Cause**: Syntax `bytes | None` requires Python 3.10+.

```bash
# Check venv Python version
source .venv/bin/activate
python3 --version  # Must be >= 3.12
```
# If 3.11 or lower, recreate venv
python3.12 -m venv .venv  # Use Python 3.12+
```

---

## Architecture Notes

### Why No Async Lock?
**Answer**: CDP/Playwright supports native concurrency. Lock was only needed for `leo_ask.sh` (AppleScript) that stole focus. Eliminated in v10.0.

### Why Key Without TTL?
**Answer**: Better visible failure than invisible magic. If Brave rotates the key, user SEES the problem (health check fails) and restarts explicitly.

### Why Health Check on Startup?
**Answer**: Detect problems early. User sees exactly what fails (Keychain, Brave, port 9222, DB) and can take immediate action.

### Why Dynamic Prompts?
**Answer**: Avoid bot/scripting detection. Hardcoded prompts like "Fix this bug" or "Test UUID" are recognizable patterns. Prompts with timestamp and dynamic variables look like real human interactions.

### Why Tests Separate from Production?
**Answer**: Industry standard convention. Mixing tests with production code is anti-pattern. `tests/` separate keeps `leo_chat/` and `brave_search/` clean.

---

## References

### Documentation
- `README.md`: Quick start guide (for humans)
- `AGENTS.md`: This file (for coding agents)
- `legacy/`: Historical documentation and backups

### Leo Skills (via `ask_leo_skill`)
- `code_refiner`: Whole-file refinement (writes to disk, single filepath)
- `code_editor`: Minimal str_replace edits (writes to disk, single filepath)
- `senior_planner`: Planning and decomposition (structured JSON plan, no disk write)

### External Tools
- **Playwright**: CDP for Brave Browser AND standalone headless Chromium for
  `ask_leo_quick` / `ask_leo_extensive`. The web-search tools launch their own
  browser (not the 9222 CDP), so they require `playwright install chromium`.
- **FastMCP**: MCP framework for Python
- **SQLite**: Leo DB (concurrent reads supported)

---

**Last updated**: 2026-09-08  
**Version**: 10.3  
**Author**: Juan Carlos Duarte