Here's the accurate updated plan reflecting exactly what's shipped and what remains.

> **Status: review passed (Leo senior_planner + ground-truth verified 2026-09-07).** Two previously-listed steps are already shipped at the anchor/NULL level — Layers 4 and 5 collapse from four headline items into one tiny "continuation discard + no-row timeout" action. The single remaining hard unknown is the unrun SSE-vs-chunked-fetch CDP probe.

## What shipped (verified)

| Layer | Change | Status |
|---|---|---|
| **0** | `entry_text` canonical read, killed 13% event-join corruption | ✅ |
| **0** | `AssistantState` three-state classifier (MISSING/ABORTED/COMPLETE) | ✅ |
| **0** | `LeoGenerationAborted` + stabilized abort detection in `wait_for_completion` | ✅ |
| **2** | UI-released cross-check in `_poll_completion_for_structured` | ✅ |
| **4** | Dead `_baseline_rowid` double-capture removed | ✅ |
| **4** | Rowid anchoring (`rowid > expected_min_rowid`) — the anchor half of Layer 4 | ✅ |
| **5** | ABORTED/NULL fast-fail (NULL + UI idle = fail fast, not 180s burn) — the NULL half of Layer 5 | ✅ |
| **5** | `_INCOMPLETE_RESPONSE_SENTINEL` + resolved-UUID "still working" branch | ✅ |
| **UI** | Universal busy guard before inject (was `is_first=False` only) | ✅ |
| **UI** | `force=True` removed from send click — can't silently click stop button | ✅ |

## What's left (6 discrete actions + edit path)

### 1. Lock relocation — ✅ DONE (2026-09-07)

**What:** `_acquire_lock()` moved from module top-level (`leo_mcp_server.py:88`) into the `__main__` guard, so importing the module for tests no longer fights a running server's lock.

**Verified:** `import leo_mcp_server` now succeeds without `sys.exit(1)`; unit tests (36 + 42 passed) no longer trip the single-instance guard.

### 2. SSE/fetch CDP probe — ✅ DONE (2026-09-07), decisive result

**What:** Authored `scripts/leo_network_probe.py` (CDP Network-domain listener) and ran it against live Brave. Classification:

| Event | Count |
|---|---|
| `eventSourceMessageReceived` (SSE) | **0** |
| `Network.loadingFinished` | 34 (dominated by static `chrome://` assets: `.js`/`.css`/`.svg`) |
| `Network.loadingFailed` | 0 |

**Finding:** Leo does **NOT** use SSE. `loadingFinished` fires per static-asset load and is NOT a clean end-of-generation signal — the only post-prompt `loadingFinished` was `stop-circle.svg` (the stop-generation button appearing), which trails generation rather than marking its end. Generation commits to SQLite (the signal this codebase already anchors on).

**Consequence for Layers 3–4:** the terminal token (Layer 1, already shipped) stays the PRIMARY completion signal; `loadingFinished` is at best **corroborating** (e.g. "UI released" already covers this better). Layer 3's value drops — see below.

### 3. Layer 1 — Terminal token — ✅ DONE (2026-09-07)

**What:** Every structured skill directive ends its response with the improbable sentinel `<<<LEO_DONE>>>`. Generalized `patch_response_complete`'s sentinel idea into `response_has_terminal_token()` + `strip_terminal_token()` (in `patch_writer.py`); the planner's structured path now detects truncation authoritatively (token absent = truncated) and auto-continues the JSON plan instead of skipping continuation entirely.

**Delivered:**
- `TERMINAL_TOKEN = "<<<LEO_DONE>>>"` + `response_has_terminal_token()` + `strip_terminal_token()` in `leo_chat/patch_writer.py`.
- `senior_planner`'s `STRUCTURED_DIRECTIVE` now requires `<<<LEO_DONE>>>` as the authoritative completeness marker.
- `execution.py` structured non-patch branch now continues a truncated JSON plan (was previously skipped).
- 5 new unit tests in `test_patch_writer.py`.

**Open decision (resolved per senior planner):** distinct `<<<LEO_DONE>>>` chosen over reusing `<<<LEO_PATCH>>>`.

### 4. Layer 6 — Encryption startup probe — ✅ DONE (2026-09-07)

**What:** Added `probe_encryption_format()` in `leo_chat/db/leo_read.py` (samples the most recent assistant `entry_text` blob and classifies it as `v10`/`plaintext`/`unavailable`), wired into the server's `__main__` startup block with loud, non-fatal logging.

**Verified:** probe reports `v10` against the live DB; 112/112 tests pass.

### 5. Layer 3 — CDP Network completion signal — DE-PRIORITIZED by probe

**What (original):** Persist a CDP session on `BraveLeoPage`, subscribe to `Network.loadingFinished`/`eventSourceMessageReceived`, treat `loadingFinished` as authoritative end-of-generation.

**Probe result overrides this:** no SSE, and `loadingFinished` is dominated by static `chrome://` asset loads + the trailing stop-button icon — it does NOT mark end-of-generation. The terminal token (Layer 1) is the primary signal; `loadingFinished` adds little over the existing UI-released cross-check.

**Revised recommendation:** skip full Layer 3 wiring. If anything, reuse the existing `_is_leo_busy()` (stop→send icon swap) as the corroborating signal, which the probe confirms tracks generation more directly than `loadingFinished`.

### 6. Layer 4 + Layer 5 remainders (collapse) — ✅ DONE (2026-09-07)

**Layer 4 (continuation discard):** `_auto_continue_if_truncated` now discards a non-advancing continuation (empty overlap in patch mode; whitespace-only overlap in prose mode) instead of appending, breaking the loop after one attempt. `_strip_overlap` remains the byte-safety net.

**Layer 5 (no-row fast-fail):** new `LeoNoRowTimeout` exception + a `no_row_timeout` param (default **8s**) in `wait_for_completion`. A persistent `MISSING` state now fails over to DOM polling (`_poll_completion_for_structured`) instead of burning the full budget.

**Decisions resolved:** N = 8s; `code_editor` model = `chat-claude-opus`; one `str_replace` per LEO_EDIT envelope.

**Verified:** 115/115 `leo_chat` tests pass (3 new tests added for both layers).

### 7. Edit path (`str_replace` / `LEO_EDIT`) — ✅ DONE (2026-09-07)

**What shipped:**
- `LEO_EDIT` sentinels (`EDIT_BEGIN`/`EDIT_OLD_MARK`/`EDIT_NEW_MARK`/`EDIT_END`) + `edit_response_complete()` + `_extract_edit_object()` in `patch_writer.py`.
- `apply_edit_patch()` — parses the envelope, enforces **exactly-one-match** on `old_str` (zero/multiple/empty → `status:error`, no write), mirrors containment/denylist/TOCTOU-mtime/atomic-write/`.bak` backup from `apply_single_patch`.
- `code_editor` skill (`leo_chat/skills/code_editor.py`, `chat-claude-opus`, `supports_patch_output=True`, `FILE_EDIT` structured directive).
- Flow wiring: `execute_leo_flow` continuation uses `edit_response_complete`; `execute_leo_flow_with_robust_patches` validates via `_validate_edit_response` (maps match-count to recoverable, safety to non-recoverable); server `ask_leo_skill` routes `code_editor` → `apply_edit_patch`.
- "view" capability satisfied by existing `assemble_context(raw=True)` (byte-faithful context injection at `execution.py:302`), no separate module needed.

**Decisions:** one `str_replace` per envelope; `chat-claude-opus` model.

**Verified:** 123/123 `leo_chat` tests pass (8 new LEO_EDIT tests).

## Recommended order

```
1. Lock relocation        → ✅ DONE
2. SSE/fetch probe        → ✅ DONE (no SSE; loadingFinished unreliable)
3. Layer 1 (token)        → ✅ DONE (now the PRIMARY completion signal)
4. Layer 6 (startup log)  → ✅ DONE
5. Layer 3 (CDP Network)  → SKIP (probe shows low value; _is_leo_busy suffices)
6. Layer 4+5 remainders   → ✅ DONE (N=8s)
7. Edit path              → ✅ DONE
```

> Note: the probe resolved the ordering question. Terminal token is primary; `loadingFinished` is corroborating at best. Layer 3's CDP Network wiring is no longer worth its complexity — the existing `_is_leo_busy()` icon-swap already corroborates end-of-generation more directly than `loadingFinished`.

## Open decisions

1. ~~**N** (fast-fail seconds)~~ → **RESOLVED**: N = 8s, shipped in Layer 5.
2. ~~Sentinel reuse~~ → **RESOLVED**: distinct `<<<LEO_DONE>>>` shipped in Layer 1.
3. ~~`_detect_truncation` deletion timing~~ → **RESOLVED-ish**: token is now primary for both patch and planner. `_detect_truncation` remains only for prose continuation (the `elif not structured` branch). It can be retired from structured paths now; prose keeps it until a prose terminal marker is adopted (out of scope).

## Pre-implementation blockers (must clear before item 1)

These are environment issues, not plan items, but they make the "run tests" gate untrustworthy and should be resolved first:

1. **`pytest-asyncio` missing** — ✅ RESOLVED: `pytest-asyncio` 1.4.0 is already installed and active (AGENTS.md caveat is stale); the full `leo_chat` suite (112 tests) runs.
2. **Stale assertion** at `tests/leo_chat/test_tareas_leo_chat.py` — still open (expects `leo-button[slot='anchor-content']`, actual `data-testid="anchor-button"`). Does not currently fail the suite; flagged for a spring clean.

## Senior planner sign-off (2026-09-07)

**Conditional pass.** Plan is internally consistent, the Layer 4/5 collapse is correctly scoped, and the ordering is sound. Three things were required before implementation: (1) explicit item 6 → item 5 dependency, (2) probe-inconclusive contingency, (3) clearing the two environment blockers. All three are now addressed above. Open decisions (N, sentinel reuse, `_detect_truncation` deletion timing) resolve inline at their respective items and do not block sign-off.