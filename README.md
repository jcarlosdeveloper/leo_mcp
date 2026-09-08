# Leo MCP Server v10.3

Bridge between coding agents (Claude Code, Hermes) and Brave Leo AI via MCP
(Model Context Protocol). Delegates execution to Leo to reduce the orchestrating
agent's token usage: the agent plans, Leo executes.

## Quick Start

### Requirements
- macOS with Brave Browser installed
- Python 3.12+
- Brave running with debug port: --remote-debugging-port=9222
  (the server bootstraps this automatically if not already active)

### Installation
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

`requirements.txt` is a generated lock file (single source of truth is
`pyproject.toml`). To regenerate it after changing dependencies:

```bash
uv pip compile pyproject.toml -o requirements.txt
```

### Run
python3 leo_mcp_server.py

On startup, the server:
- Acquires a single-instance lock (~/.leo_mcp_server.lock)
- Bootstraps Brave with CDP port 9222 (restarting it if needed)
- Starts the MCP transport at http://127.0.0.1:9223

## Available Tools

| Tool                     | Description                                                                          |
|--------------------------|--------------------------------------------------------------------------------------|
| ask_leo_skill            | Runs a skill (refactor, debug, planning) with file injection and optional disk write |
| ask_leo_quick            | Fast web search via Brave AI                                                         |
| ask_leo_extensive        | Deep research via Brave AI                                                           |
| get_conversation_history | Recent conversation history                                                          |

### ask_leo_skill Skills

| Skill           | Use                          | Writes to disk       |
|-----------------|------------------------------|----------------------|
| code_refiner    | Refactoring and optimization | Yes (feature-flagged)|
| code_editor     | Minimal str_replace edits    | Yes (feature-flagged)|
| senior_planner  | Decomposition and planning   | No                   |

### Web Search Tools (ask_leo_quick / ask_leo_extensive)

`ask_leo_quick` (fast search) and `ask_leo_extensive` (deep research) launch
their **own** headless Chromium via Playwright — independent of the Brave CDP
connection on port 9222 used by `ask_leo_skill`. They therefore require the
Playwright browser binaries to be installed, in addition to Brave running:

```bash
source .venv/bin/activate
playwright install chromium
```

If you hit `BrowserType.launch: Executable doesn't exist at .../ms-playwright/...`,
run the command above; it means the Playwright browser binary is missing.

## Write-to-Disk (feature flag)

code_refiner can write the refactored file directly to disk instead of returning
the full content to the agent. This minimizes the tokens the agent re-ingests: it
receives only a compact summary (path, risk, one-line description).

Opt-in via a single environment variable; the write location is derived
per-request (see below), not from a global path:

| Variable       | Values                        | Effect                                          |
|----------------|-------------------------------|-------------------------------------------------|
| LEO_WRITE_MODE | enabled (default)             | Writes the modified file to disk                |
| LEO_WRITE_MODE | dry_run                       | Reports what would be written without touching disk |
| LEO_WRITE_MODE | disabled                      | Leo returns output only; nothing is written     |

Disk writing activates only when ALL hold:
1. LEO_WRITE_MODE is not disabled
2. The skill opts into patch output (code_refiner or code_editor)
3. Exactly one file path is provided

Note that write mode is ENABLED by default, so code_refiner and code_editor are
effectively write-patch skills out of the box: they REQUIRE exactly one absolute
path in `filepaths`. Omitting `filepaths` (or passing more than one) is rejected
with `status: "error"` before any Leo round-trip — it is not treated as optional
context. The non-writing skill senior_planner accepts `filepaths` only as optional
injected context and never writes to disk.

Write root is resolved per request (not a global LEO_WRITE_ROOT):
- If the target lives in a detectable project (AGENTS.md, pyproject.toml,
  package.json, Cargo.toml, go.mod, Makefile, ...), the project root is used.
- Otherwise, if the target is under a temp directory (/tmp or the system temp
  dir), that file's own parent directory is used.
- Anything else fails closed: the write is rejected. This lets one server
  serve multiple projects in a single session without a shared root.

Safety guarantees (enforced by patch_writer):
- Per-request root containment: writes outside the resolved root are rejected
  (symlink-safe)
- Denylist: writes to sensitive locations (~/.ssh, ~/.aws, ~/.config, ~/.gnupg,
  ~/Library, /etc, /usr, /bin, /sbin, /var, /System, or the bare home dir) are
  rejected even if they appear inside the root
- Anti-TOCTOU: if the target changes on disk during the Leo round-trip, the
  stale write is aborted instead of clobbering newer content
- Atomic writes via temp file + os.replace (no partial writes)
- Backup: the previous version is saved to a .bak file before overwriting;
  repeated writes use .bak.1, .bak.2, ... so the original is never clobbered
- Validation: malformed or truncated output is rejected before any write
- Action checks: modify requires an existing file; create requires a new one

Example:
mkdir -p /tmp/leo_test && echo "def f(): return 1+1" > /tmp/leo_test/x.py
LEO_WRITE_MODE=dry_run python3 leo_mcp_server.py

Recommendation: run against a git-tracked repo. The .bak file is a secondary
safety net; git diff / git checkout is the primary recovery path.

## Structured Output

code_refiner, code_editor, and senior_planner support a structured mode that
injects a strict output contract: a sentinel-delimited FILE_PATCH envelope for
the refiner (the file content is written verbatim, never JSON-escaped), a
FILE_EDIT envelope for the editor (a single OLD_STR/NEW_STR pair), and a
STRUCTURED_PLAN JSON object for the planner.

Note: Leo treats CDP-injected content as data, not instructions (anti-injection
by design). It may wrap output in markdown fences or add prose. The patch parser
tolerates this: it locates the envelope by its sentinels
(<<<LEO_PATCH>>> ... <<<END_LEO_PATCH>>>) within surrounding prose, and the
terminal sentinel is the sole completeness signal (a truncated response is
reported clearly and can be auto-continued).

## Troubleshooting

### `connect_over_cdp` times out — `ws connected` then silence
**Symptom**: `BrowserType.connect_over_cdp: Timeout 30000ms exceeded`; the call
log shows `<ws connecting>` → `<ws connected>` and then stops. The CDP endpoint
is healthy (`curl http://localhost:9222/json/version` responds, and a raw
WebSocket `Browser.getVersion` call succeeds).

**Cause (not a version/dependency issue)**: Playwright's `connect_over_cdp`
sends `Target.setAutoAttach(autoAttach=true, waitForDebuggerOnStart=true)` to
every existing target and waits for each to resolve
`Runtime.runIfWaitingForDebugger`. Brave's Leo AI conversation iframes
(`chrome-untrusted://leo-ai-conversation-entries/...`) and extension service
workers (e.g. `keepa`) never complete that handshake, so Playwright stalls —
intermittently, once a Leo conversation tab and/or extension workers become
resident in a long-lived session.

**Fix**: fully quit Brave and relaunch it, then restart the server:
```bash
osascript -e 'quit app "Brave Browser"' && sleep 3
brave-debug          # relaunch with --remote-debugging-port=9222
python3 leo_mcp_server.py
```
Closing the Leo `chrome://leo-ai/` tabs and any page that emits a continuous
stream of `Log.entryAdded` events (e.g. an admin panel with many password
inputs) also helps prevent the stall.

## Structure

leo_mcp/
├── leo_mcp_server.py      # MCP server + tool definitions
├── leo_chat/              # CDP module
│   ├── config.py          # Write-mode resolution (LEO_WRITE_MODE)
│   ├── execution.py       # Unified Leo flow (structured + streaming)
│   ├── patch_writer.py    # Single-file disk writes (validation, atomicity)
│   ├── pages/             # Brave Leo Page Object
│   ├── context/           # Prompt building
│   ├── db/                # SQLite + streaming
│   └── skills/            # Skill factory + skill_support
├── brave_search/          # Brave Search module
└── tests/                 # Tests (separate from production)
    ├── brave_search/
    ├── leo_chat/          # includes test_patch_writer.py
    └── integration/       # includes ask_leo_skill decision matrix

## Tests

source .venv/bin/activate
pytest tests/ -v

Coverage includes:
- Brave Browser (headless, CDP)
- leo_chat (skills, context, streaming)
- patch_writer (dry-run, versioned backup, path traversal, denylist, TOCTOU, envelope validation, action checks)
- integration (MCP server, ask_leo_skill decision matrix)

Fast verification without calling Leo (levels 1-5):
- Import resolution and function presence
- Per-skill structured contract injection
- LEO_WRITE_MODE resolution and per-request write-root derivation
- patch_writer safety guarantees
- Decision matrix: disabled never writes; senior_planner never writes even when enabled

## Architecture Notes

- Agent plans, Leo executes: token savings come from file injection (Leo reads
  files, the agent does not) plus disk writes (the agent receives a summary, not
  the file content).
- Conversation resume: pass conversation_uuid to send a follow-up without
  re-injecting history.
- Single structured axis: one flag drives both the file patch and the JSON plan;
  disk writing is a separate, narrower condition.

## Philosophy

- Visible failure over invisible magic: explicit bootstrap and validation
- User takes action over auto-repair: clear errors with resolution steps
- Opt-in destructive operations: disk writes are off by default, per-skill gated
- Data over instructions: injected content is never treated as commands

---

License: MIT
Author: Juan Carlos Duarte
Version: 10.3 (2026-07)