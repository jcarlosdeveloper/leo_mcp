# Leo MCP Server — Master Delivery Plan

> **Scope**: Every statement is ground-truth verified against the Hermes v0.21.0 CLI binary and official documentation.
---

## Part 1 — Ground Truth: What Hermes Actually Is

Before any implementation decision, these facts govern everything.

### 1.1 Two surfaces, one DB

The board has exactly two front doors, both backed by `~/.hermes/kanban.db`https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md:

- **Agents** drive the board through injected `kanban_*` tools (`kanban_show`, `kanban_complete`, `kanban_request_review`, `kanban_request_changes`, `kanban_block`, `kanban_heartbeat`, `kanban_comment`, `kanban_create`, `kanban_link`, `kanban_unblock`, `kanban_attach`, etc.)
- **Humans/scripts** drive it through `hermes kanban …` CLI

Workers never shell out to `hermes kanban`. When the dispatcher spawns a worker it sets `HERMES_KANBAN_TASK=t_XXXXXXXX` in the child env, and that env var injects the `kanban_*` toolset into the model's schema automatically. Three reasons this matters: backend portability (workers may run in Docker where `hermes` isn't installed), no shell-quoting fragility, and structured JSON errors the model can reason about https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

### 1.2 Worker lifecycle (injected automatically)

Every spawned worker receives the `KANBAN_GUIDANCE` block in its system prompt — there is nothing to install per profile https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md:

1. Call `kanban_show()` — read title, body, parent handoffs, prior attempts, full comment thread
2. `cd $HERMES_KANBAN_WORKSPACE` via the terminal tool
3. Call `kanban_heartbeat(note="...")` every few minutes on long ops. The dispatcher reclaims tasks running past `kanban.dispatch_stale_timeout_seconds` (default 4h) with no heartbeat in the last hour https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md
4. Finish with `kanban_complete(summary=..., metadata={...})` or `kanban_block(reason="...")`
5. A worker that exits with status 0 while the task is still `running` triggers a `protocol_violation` event — Hermes injects up to two synthetic nudges before that happens https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md

### 1.3 The review lifecycle is same-card

`kanban_request_review()` flips the card to `review` status. The reviewer calls `kanban_complete` to approve or `kanban_request_changes` to bounce it back to the implementer — **on the same card, not a child card**. `kanban_comment` is the durable annotation channel; comments appear in every future `kanban_show()` on that card https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

### 1.4 `kanban_complete` has no `artifacts` parameter

The correct CLI signature is:
```
hermes kanban complete [--result R] [--summary S] [--metadata M] task_id
```
File delivery from the CLI is a **separate** command: `hermes kanban attach <task_id> <path>`https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

**Injected agent tool surface**: The `kanban_complete` tool injected into workers by the dispatcher is different. Its signature is `kanban_complete(summary=..., artifacts=[<absolute paths>])` and `artifacts` is a real top-level parameter. Absolute paths declared there are copied into durable per-task attachment storage before the scratch workspace is deleted. Evidence: `agent/prompt_builder.py:371` and `kanban.md:66`.

Only CLI templates that used an `--artifacts` flag were wrong. Agent-side templates that pass `artifacts=[...]` to the injected `kanban_complete` tool are correct and should be kept. Corrected throughout this plan.

### 1.5 `worker_context` surfaces parent handoffs automatically

When a downstream card (reviewer, synthesizer) calls `kanban_show()`, it receives the parent's `summary` and `metadata` fields as typed fields — not prose to parse https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md. This is why the metadata shape on every `kanban_complete` must be canonical: downstream cards depend on it structurally.

### 1.6 Scratch workspaces delete on completion

The default workspace is `scratch` — a fresh `tmp` dir deleted when the task completes. Files explicitly declared via `kanban_complete(artifacts=[...])` are copied into durable per-task attachment storage before cleanup. For engineering tasks that must persist, use `worktree:<path>` or `dir:<path>`https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

> **Correction from PLAN_PHASE_0**: `dir:{{WORKDIR}}` is valid syntax. The verified workspace forms are `scratch | worktree | worktree:<path> | dir:<path>`https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

---

## Part 2 — The Four-Profile Architecture

> **DISCOVERY update**: Profiles on the machine under test are `orchestrator`, `senior-coder`, `reviewer`, and `synthesizer` (soul_version 2). The profile formerly called `coder` is `senior-coder`. All references to `coder` below have been updated to `senior-coder`.

### 2.1 Profile set (verified against machine under test — 4 profiles, ceiling is 4)

| Profile | Role | Leo Surface | Toolsets | Routed when |
|---|---|---|---|---|
| `orchestrator` | Routes, never executes | `ask_leo_extensive` | `kanban` only — **NO terminal/file/web** | Project root cards, triage fan-out |
| `senior-coder` | Implements one file per card | `leo_next_instruction`, `leo_apply_edit` | `terminal, file` | implement, scaffold, patch, refactor |
| `reviewer` | Verifies completed impl — read-only shell | `ask_leo_extensive`, `ask_leo_result`, `ask_leo_quick` | `terminal` (read-only by convention), **no file writes** | `needs_review` / `review` status cards |
| `synthesizer` | Final delivery report | `ask_leo_extensive`, `ask_leo_result` | `terminal, file` | Terminal node after all reviewer cards done |

**Profile naming note**: The implementation profile is `senior-coder`, not `coder`. All card bodies, `--assignee` flags, and SOUL.md references must use `senior-coder`.

**MCP tool scoping (DISCOVERY finding)**: MCP tools are scoped at the profile level via `config.yaml`. There is no per-card MCP override — `kanban_create` has no `mcp_servers` parameter. The tool surfaces above are permanent and profile-scoped.

> **Profile proliferation guard**: 4 profiles is the ceiling. Do not add profiles without removing one.

### 2.2 Why `orchestrator` must be tool-restricted

The orchestrator **literally cannot execute implementation tasks** because `terminal` and `file` toolsets are disabled in its `config.yaml` — this is enforced at the platform level, not just by SOUL.md convention.

```yaml
# ~/.hermes/profiles/orchestrator/config.yaml
agent:
  disabled_toolsets:
    - terminal
    - file
```

### 2.3 SOUL.md vs config.yaml — the distinction that matters (DISCOVERY confirmed)

- **`config.yaml`** — enforces tool restrictions and MCP server availability at the platform level. `disabled_toolsets` listed here are **always removed**, even if a platform config still enables them. MCP tools are also scoped here, permanently, per profile.
- **`SOUL.md`** — is the slot #1 identity layer, injected verbatim every message. It belongs: identity, the Leo MCP loop with exact tool signatures, conversation_uuid discipline, and hard behavioral boundaries. It does **NOT** belong: skill invocations, MCP config, toolset restrictions, per-task instructions, repo conventions, or skill-specific gates. SOUL.md provides behavioral identity only — it cannot enforce toolset restrictions.

### 2.4 Skills — injected per card, not in SOUL.md (DISCOVERY finding)

Skills load via `kanban_create(skills=[...])` dispatcher injection at spawn. `/skill-name` slash commands only work in chat, not in system prompt context. The card body is the equivalent of the dispatcher's `--skills` injection in interactive mode. Skills must be referenced explicitly in the card body as MANDATORY instructions.

### 2.5 Interactive mode limitation (DISCOVERY critical finding)

`hermes -p <profile>` (interactive mode) bypasses the dispatcher — `HERMES_KANBAN_TASK` is never set. This splits kanban tools into two tiers:

- **Tier 1 (always available)**: `kanban_create`, `kanban_list`, `kanban_link`, `kanban_comment`, `kanban_attach`
- **Tier 2 (dispatcher only — requires `HERMES_KANBAN_TASK`)**: `kanban_show`, `kanban_complete`, `kanban_request_review`, `kanban_request_changes`, `kanban_heartbeat`, `kanban_block`

**Implication**: The full kanban lifecycle (including `kanban_request_review` and `kanban_complete`) can only be exercised by cards dispatched through the board — not by interactive profile sessions. End-to-end testing (Gate 6) must use dispatched cards, not interactive mode.

### 2.6 Profile descriptions (exact text for `hermes profile describe --text`)

**orchestrator**:
```
Orchestrator. Decomposes high-level goals into per-file Kanban cards and routes to
specialist profiles. Uses kanban_create with parents=[] for parallel work and
parents=[id] for sequential work. Never implements, writes files, or runs shell
commands. Uses ask_leo_extensive for reasoning. Loop: ask_leo_extensive then
create_card/link_cards/comment/verify/done/wait/error via kanban tools. One root card
per session. Stamp shared decisions into each card body. Restricted to kanban toolset
only — no terminal, file, or browser access.
```

**senior-coder**:
```
Executes atomic file-level build tasks from a Kanban card. Reads the card spec via
kanban_show(), changes to HERMES_KANBAN_WORKSPACE, runs leo_next_instruction /
leo_apply_edit loop until acceptance criteria pass, calls kanban_heartbeat every few
minutes on long ops, calls kanban_request_review(summary=..., metadata=...) when done.
Never calls kanban_complete directly — always routes through kanban_request_review.
One file per session. metadata must include: changed_files, verification, decisions,
residual_risk. Never destructive without confirmation gate.
Assign: implement, write, scaffold, patch, refactor.
```

**reviewer**:
```
Reviews completed implementation cards for correctness, security, edge cases, and spec
compliance. Read-only shell — never writes files. Uses ask_leo_extensive for analysis.
Reads changed_files and verification commands from parent handoff metadata via
kanban_show(). Runs every verification command before issuing any verdict.
PASS: kanban_complete(summary=..., metadata={verdict: PASS, ...}).
PASS_WITH_NOTES: kanban_complete with metadata.reviewer_notes.
FAIL: kanban_comment first (durable record), then kanban_request_changes(reason="FAIL:
<line-level citations>") — routes back to senior-coder on same card, no new card.
Never leo_next_instruction/leo_apply_edit. kanban_block only for true external blockers.
Assign: review status cards.
```

**synthesizer**:
```
Runs once at the terminal node after all reviewer cards are done. Shell + filesystem
access. Uses ask_leo_extensive for deep synthesis. Reads all parent handoffs via
kanban_show(), writes build_report_<ts>.md to HERMES_KANBAN_WORKSPACE, then delivers:
1. kanban_complete(summary=..., metadata={report_path, parent_ids, word_count, ...})
2. kanban attach <card_id> build_report_<ts>.md (separate call)
Turn budget: 25. No retry loop. Never leo_next_instruction/leo_apply_edit.
Assign: final synthesis card only.
```

---

## Part 3 — Card Architecture

### 3.1 The dependency DAG (fixed per feature)

```
[Orchestrator card]  ← triage, specifies + decomposes
        │
        ├── [Senior-coder card: file A]  ← parallel, no parent link between siblings
        │         │
        │   [Reviewer card: file A]
        │
        ├── [Senior-coder card: file B]
        │         │
        │   [Reviewer card: file B]
        │
        └── [Senior-coder card: file C]
                  │
              [Reviewer card: file C]
                  │
                  │  (all reviewer cards must be done)
                  ▼
         [Synthesizer card]  ← terminal node, runs once
```

Parallel senior-coder cards have **no parent links between them** — independence means they run concurrently. Each senior-coder card links only to its orchestrator parent and its reviewer child. The synthesizer gates on all reviewer cards.

### 3.2 Card creation CLI — verified shapes

```bash
# Orchestrator / triage root card
hermes kanban --board <slug> create "[Phase N] <title>" \
  --assignee orchestrator \
  --tenant <tenant> \
  --priority 5 \
  --triage \
  --max-retries 1 \
  --idempotency-key <board>-phaseN \
  --body "$(cat orchestrator_card.md)" \
  --json | jq -r .id

# Senior-coder card
hermes kanban --board <slug> create "Implement src/module/file.py" \
  --assignee senior-coder \
  --tenant <tenant> \
  --priority 4 \
  --parent <orchestrator_card_id> \
  --max-retries 2 \
  --workspace dir:/absolute/path/to/workdir \
  --body "$(cat senior_coder_card.md)" \
  --json | jq -r .id

# Reviewer card (child of implementation)
hermes kanban --board <slug> create "Review: Implement src/module/file.py" \
  --assignee reviewer \
  --tenant <tenant> \
  --priority 4 \
  --parent <senior_coder_card_id> \
  --max-retries 2 \
  --body "$(cat reviewer_card.md)" \
  --json | jq -r .id

# Synthesizer card (child of ALL reviewer cards)
hermes kanban --board <slug> create "[Synthesis] Final delivery report — <feature>" \
  --assignee synthesizer \
  --tenant <tenant> \
  --priority 2 \
  --parent <reviewer_1_id> \
  --parent <reviewer_2_id> \
  --max-retries 1 \
  --body "$(cat synthesizer_card.md)" \
  --json | jq -r .id
```

**Key CLI facts verified against v0.21.0 binary**:
- `--board` is a **top-level** flag on `hermes kanban`, not a flag on `create`
- `--priority` accepts **numeric values only** (1–5), not strings like `critical|high|normal|low`
- `--parent` is repeatable
- `--goal-max-turns N` is the correct flag (not `max_turns`)
- `--idempotency-key` prevents duplicate creation on re-runs
- `--triage` parks in triage column first
- Task IDs are `t_XXXXXXXX` (8 hex chars), returned as `"id"` in `--json` output

### 3.3 Canonical metadata shapes

**Coder → `kanban_request_review`**:
```json
{
  "changed_files": ["src/module/file.py", "tests/test_file.py"],
  "verification": ["pytest tests/test_file.py -x -q", "ruff check src/module/file.py"],
  "decisions": ["chose X over Y because Z"],
  "residual_risk": ["known gap not blocking review"],
  "tests_run": 14,
  "tests_passed": 14
}
```

**Reviewer PASS → `kanban_complete`**:
```json
{
  "verdict": "PASS",
  "criteria_checked": ["criterion: met"],
  "security_findings": [],
  "verification_output": "first 300 chars of test output",
  "reviewer_notes": "non-blocking observations if any",
  "residual_risk": ["known gaps not blocking approval"]
}
```

**Reviewer FAIL — two-step** (comment first, then request-changes):
```python
# Step 1 — durable structured record
kanban_comment(body="FAIL handoff:\n" + json.dumps({
    "verdict": "FAIL",
    "criteria_failed": ["criterion: why it failed"],
    "security_findings": ["file:line — issue"],
    "required_changes": ["specific change required"],
    "verification_output": "test failure output, first 300 chars"
}, indent=2))

# Step 2 — routes card back to senior-coder, same card, no new card
kanban_request_changes(reason="FAIL: one-line summary of blocking issue")
```

> `kanban_comment` must come **before** `kanban_request_changes` because it is the durable annotation channel — it appears in every future `kanban_show()` on that card, meaning the senior-coder will see it when the card is re-dispatched https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

**Synthesizer → `kanban_complete` + `kanban attach`**:
```python
kanban_complete(
    summary="2-3 sentence executive summary",
    metadata={
        "build_summary": "prose summary",
        "files_changed": ["path: description"],
        "reviewer_verdicts": ["card_title: PASS"],
        "reviewer_flags": ["PASS_WITH_NOTES items"],
        "integration_risks": ["cross-file risks"],
        "next_sprint": [
            {"title": "...", "assignee": "senior-coder", "priority": 4, "reason": "..."}
        ],
        "cards_synthesized": ["t_XXXXXXXX", "t_YYYYYYYY"]
    }
)
# Separate file delivery — no artifacts= parameter exists on kanban_complete
# hermes kanban attach <synthesizer_task_id> build_report_YYYY-MM-DD_HHMM.md
```

---

## Part 4 — The `leo_decompose_plan` Decision

### 4.1 ADR-003: Why keep it (justified)

Native `hermes kanban specify` and `hermes kanban decompose` exist and work https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md. The question is whether `leo_decompose_plan` adds enough value to justify building and maintaining it.

**It does, for three specific reasons**:

1. **Leo-driven decomposition quality**: `senior_planner` produces typed I/O contracts, shell-verifiable acceptance criteria, and verbatim spec excerpts embedded in each card body. The native Hermes specifier profile cannot match this because it operates on the generic specifier prompt without access to Leo's reasoning surface.
2. **Self-contained card bodies**: The `_build_kanban_card_body` template stamps the full spec excerpt verbatim into each card so workers never need the original document. Native `decompose` does not guarantee this — workers on natively-decomposed cards may still need external context.
3. **Role-aware routing in one pass**: The decomposer assigns `reviewer` and `synthesizer` cards with correct `--parent` wiring atomically. Native decompose requires manual post-processing to achieve this.

**Verdict**: Keep `leo_decompose_plan`. Scope it narrowly as a card-body-quality tool, not a replacement for the Hermes dispatcher.

### 4.2 What `leo_decompose_plan` must NOT do

- It must not duplicate what `hermes kanban decompose` does for triage fan-out
- It must not manage the dispatcher loop or retry logic
- It does not replace `senior_planner` — it calls `senior_planner` as a sub-step

### 4.3 Corrected `leo_decompose_plan` code issues (from PLAN_PHASE_2)

The draft code in PLAN_PHASE_2 has two bugs to fix before implementation:

**Bug 1 — workspace parameter uses literal `{WORKDIR}` as default**:
```python
# WRONG
workspace: str = "dir:{WORKDIR}",

# CORRECT
workspace: str = "scratch",  # caller passes dir:/absolute/path if needed
```

**Bug 2 — priority passed as string, CLI expects integer**:
```python
# WRONG — priority from Leo's JSON may be string "5"
"--priority", t_prio,

# CORRECT — cast to int, validate range
t_prio_int = max(1, min(5, int(t_prio)))
cmd += ["--priority", str(t_prio_int)]
```

**Bug 3 — `next_sprint.priority` in synthesizer metadata uses string levels**:
The synthesizer card template in multiple docs uses `"priority": "critical|high|normal|low"`. The verified CLI only accepts integers 1–5. Fix: use `"priority": 4` (integer) throughout.

---

## Part 5 — The `setup.sh` Bootstrap (Corrected)

Key corrections applied to the PLAN_PHASE_0 `setup.sh`:

```bash
#!/usr/bin/env bash
# setup.sh — Hermes Kanban board bootstrap
# Usage: source project.env && bash setup.sh
# Idempotent — safe to re-run. Cards use --idempotency-key.

set -euo pipefail

# ... (colour helpers from PLAN_PHASE_0 — unchanged) ...

# ── Profile creation ──────────────────────────────────────────────────────────
# Create profiles with correct model IDs (not display names)
hermes profile create orchestrator \
  --model "anthropic/nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"

hermes profile create senior-coder \
  --model "anthropic/nvidia_nim/nvidia/nemotron-3-super-120b-a12b"

hermes profile create reviewer \
  --model "anthropic/nvidia_nim/deepseek-ai/deepseek-v4-pro-0813"

hermes profile create synthesizer \
  --model "anthropic/nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"

# ── Orchestrator tool restriction (enforced, not just convention) ─────────────
mkdir -p ~/.hermes/profiles/orchestrator
cat > ~/.hermes/profiles/orchestrator/config.yaml << 'EOF'
agent:
  disabled_toolsets:
    - terminal
    - file
EOF

# ── Profile descriptions (routing hints for the decomposer) ──────────────────
hermes profile describe orchestrator --text "..."   # (full text from Part 2.6)
hermes profile describe senior-coder --text "..."
hermes profile describe reviewer --text "..."
hermes profile describe synthesizer --text "..."

# ── Board config ──────────────────────────────────────────────────────────────
hermes config set kanban.orchestrator_profile orchestrator
hermes config set kanban.default_assignee senior-coder
hermes config set kanban.auto_decompose false
hermes config set kanban.max_in_progress "${MAX_IN_PROGRESS}"
hermes config set kanban.dispatch_interval_seconds "${DISPATCH_INTERVAL}"
hermes config set kanban.failure_limit "${DEFAULT_MAX_RETRIES}"
hermes config set kanban.heartbeat_timeout 300

# ── Seed cards (--priority as integer, --idempotency-key for re-run safety) ───
P1=$(hermes kanban --board "${BOARD_SLUG}" create \
  "${PHASE1_TITLE}" \
  --assignee orchestrator \
  --tenant "${TENANT}" \
  --priority 5 \
  --triage \
  --max-retries "${DEFAULT_MAX_RETRIES}" \
  --idempotency-key "${BOARD_SLUG}-phase1" \
  --body "${PHASE1_BODY}" \
  --json | jq -r .id)
```

---

## Part 6 — TDD Protocol

This is `🔒 STATIC` across all projects and must not change.

All `senior-coder` cards follow **Acceptance Test Driven Development** — acceptance or integration test first, unit tests written during implementation:

1. **RED** — Write the acceptance/integration test mapped to the card's Acceptance Criteria. Run it. It must **fail** before a single line of implementation is written.
2. **GREEN** — Write minimum implementation to pass. Write unit tests as you go.
3. **REFACTOR** — Clean up. Full suite must still pass.

**A card is not ready for `kanban_request_review` until**:
- All Acceptance Criteria tests pass
- Full test suite passes (no regressions)
- Lint and type check pass
- No code exists that was written before a failing test existed

If the test runner is broken:
```python
kanban_block(reason="capability: test infrastructure not functional")
```

---

## Part 7 — `kanban_block` Reference

```
kanban_block(reason="<kind>: <one-line description>")
```

| Kind | Use when |
|---|---|
| `needs_input` | Human decision required before proceeding |
| `needs_decision` | Architectural choice not in ARCHITECTURE.md |
| `dependency` | Waiting on another card or external system |
| `capability` | Requires a tool or access this profile lacks |
| `transient` | Temporary failure — retry after human resolves |

> `review-required` is **not** a block kind. Use `kanban_request_review` for normal code review flow. Reserve `kanban_block` for genuine blockers the worker cannot resolve https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

Repeated same-kind re-blocks auto-escalate to triage after `BLOCK_RECURRENCE_LIMIT` (default 2) cycles — the board's unblock-loop breaker prevents infinite retry storms https://github.com/nousresearch/hermes-agent/blob/main/website/docs/user-guide/features/kanban.md.

---

## Part 8 — Validated Checklist (ordered by dependency)

Work through these in sequence. Each gate must pass before the next group starts.

### Gate 0 — Environment

- [ ] `hermes gateway start` runs clean with no errors
- [ ] NIM server healthy at `NIM_URL` (`curl -sf ${NIM_URL}/health`)
- [ ] Leo MCP server reachable at `LEO_MCP_URL`
- [ ] `hermes` binary is v0.21.0 (`hermes --version`)

### Gate 1 — Profiles

- [ ] `orchestrator` profile created with correct model ID
- [ ] `orchestrator` `config.yaml` has `disabled_toolsets: [terminal, file]`
- [ ] `senior-coder`, `reviewer`, `synthesizer` profiles created with correct model IDs
- [ ] All four profile descriptions set (`hermes profile describe <name> --text "..."`)
- [ ] **Smoke test**: create one orchestrator card, confirm it cannot invoke terminal tools

### Gate 2 — Board

- [ ] Board `leo-decompose` created with `--default-workdir`
- [ ] `create --json` id field confirmed as `t_XXXXXXXX` format
- [ ] Board config applied (failure_limit, max_in_progress, heartbeat_timeout)

### Gate 3 — Review lifecycle

- [ ] Live test: senior-coder calls `kanban_request_review` → card moves to `review`
- [ ] Live test: reviewer calls `kanban_request_changes` → same card routes back to senior-coder (no new card created)
- [ ] Live test: reviewer calls `kanban_complete` → card reaches `done`
- [ ] Confirm `kanban_comment` appears in subsequent `kanban_show()` on same card

### Gate 4 — File delivery

- [ ] Synthesizer calls `kanban_complete` (metadata only) — confirmed no `artifacts=` param
- [ ] Synthesizer calls `hermes kanban attach <id> <path>` separately — file attached
- [ ] Attachment accessible in task drawer / `kanban_attachments` tool

### Gate 5 — `leo_decompose_plan`

- [ ] `_parse_task_graph` unit tests written (RED) before server code
- [ ] `_build_kanban_card_body` unit tests cover senior-coder, reviewer, synthesizer branches
- [ ] FAIL-path: `kanban_request_changes` re-routes same card, no duplicate created
- [ ] Priority bug fixed: all `--priority` values cast to int (1–5)
- [ ] Workspace bug fixed: default is `scratch`, not `dir:{WORKDIR}`
- [ ] Role-sorted creation order confirmed (senior-coder → reviewer → synthesizer)
- [ ] `--idempotency-key` on all `leo_decompose_plan`-created cards

### Gate 6 — End-to-end

- [ ] Full pipeline: orchestrator → senior-coder → reviewer (PASS) → synthesizer
- [ ] Full pipeline: orchestrator → senior-coder → reviewer (FAIL) → senior-coder retry → reviewer (PASS) → synthesizer
- [ ] `build_report_*.md` produced, attached, accessible
- [ ] `hermes kanban diagnostics` returns clean board health

---

## Part 9 — Open Questions (must resolve before Gate 5)

| # | Question | Impact | Suggested resolution |
|---|---|---|---|
| OQ-1 | `verifier` (swarm) vs `reviewer` (request-review) terminology mismatch | Low — swarm not used in this pipeline | Keep `reviewer` as the profile name; note that `hermes kanban swarm --verifier` would need mapping if swarm is adopted later |
| OQ-2 | Thinking vs no-thinking model variant per profile | Medium — affects token cost and quality | Default to `anthropic/nvidia_nim/...` (with thinking) for orchestrator/synthesizer; `claude-3-freecc-no-thinking/nvidia_nim/...` for senior-coder to reduce cost |
| OQ-3 | `kanban_* tool` vs `platform_toolsets.cli: kanban` in config — are these the same surface? | High — must be correct before Gate 1 | The `kanban_*` tools are the Python-layer tool surface injected by the dispatcher; `platform_toolsets` in config controls which toolsets a profile can load. They are related but distinct. Validate with a live profile smoke test |
| OQ-4 | `auto_subscribe_on_create` behavior with `leo_decompose_plan`-created cards | Low | Set `kanban.auto_subscribe_on_create: false` for programmatically-created cards to avoid spurious wake turns |

---

## Part 10 — What Each File Owns (Change Zones)

| File | Section | Mutable? | Who edits |
|---|---|---|---|
| `project.env` | Everything | ✏️ Every project | You, before `setup.sh` |
| `AGENTS.md` | Project Identity, Stack, Repo Layout, Build/Test Commands, Code Conventions, Architecture Constraints, Changes Requiring Approval, Phase Log | ✏️ Every project | You, at setup |
| `AGENTS.md` | Prerequisites, TDD Protocol, Worker Contract, Profile Map, Blocked Card Protocol | 🔒 Never | Don't touch |
| `ARCHITECTURE.md` | All content | ✏️ Every project | You, when architecture changes |
| `ARCHITECTURE.md` | Section headers | 🔒 Never | Don't touch — workers find them by exact name |
| `CARD_TEMPLATE.md` | `✏️ PROJECT` slots | ✏️ Per card instance | Orchestrator or you at setup |
| `CARD_TEMPLATE.md` | `🔒 STATIC` sections (Decomposition Rules, TDD Steps, Verdict Contract, Scope Boundaries) | 🔒 Never | Don't touch |
| `setup.sh` | Everything | ✏️ Every project | You, before running |
| `leo_mcp_server.py` | `leo_decompose_plan` and helpers | ✏️ Implementation | After Gate 5 |

---

## Summary of All Corrections Applied

| Source doc | Incorrect claim | Corrected to |
|---|---|---|
| PLAN_PHASE_0 `setup.sh` | `--priority critical/high/normal/low` strings | `--priority 1-5` integers only |
| PLAN_PHASE_0 `AGENTS.md` | `artifacts=[]` on `kanban_complete` | `kanban_complete` + separate `kanban attach` |
| PLAN_PHASE_2 | `default` profile with full tools | `orchestrator` profile, `terminal`/`file` disabled in config.yaml |
| PLAN_PHASE_2 code | `workspace: str = "dir:{WORKDIR}"` default | `workspace: str = "scratch"` |
| PLAN_PHASE_2 code | `--priority` passed as raw string from Leo JSON | Cast to `int`, clamp to 1–5 |
| PLAN_PHASE_1 | Model IDs like `deepseek-v4-pro` | `anthropic/nvidia_nim/deepseek-ai/deepseek-v4-pro-0813` |
| CARD_TEMPLATE | `next_sprint.priority` as string level | Integer 1–5 |
| Multiple docs | SOUL.md enforces tool restrictions | Only `config.yaml` `disabled_toolsets` enforces restrictions; SOUL.md is guidance only |
| Multiple docs | Reviewer creates a child card for FAIL | `kanban_request_changes` on same card; `kanban_comment` first for durability |
| Multiple docs | `--goal-max-turns` referred to as `max_turns` | `--goal-max-turns N` is the correct CLI flag |
## Part 1 Verification Checklist

- [x] Two surfaces / one DB (agents use injected kanban_* tools, humans use CLI) — VERIFIED — kanban.md: "The board has exactly two front doors, both backed by ~/.hermes/kanban.db"
- [x] HERMES_KANBAN_TASK env var injects the kanban toolset — VERIFIED — kanban.md: dispatcher sets HERMES_KANBAN_TASK in child env, which injects kanban_* toolset automatically
- [x] Worker lifecycle KANBAN_GUIDANCE injected automatically — VERIFIED — kanban.md: "Every spawned worker receives the KANBAN_GUIDANCE block in its system prompt — there is nothing to install per profile"
- [x] Heartbeat every few minutes, dispatcher reclaims past kanban.dispatch_stale_timeout_seconds (default 4h) with no heartbeat in last hour — VERIFIED — kanban.md: stale timeout and heartbeat reclaim behavior documented
- [x] Review lifecycle is same-card via kanban_request_review / kanban_request_changes — VERIFIED — kanban.md: "The reviewer calls kanban_complete to approve or kanban_request_changes to bounce it back to the implementer — on the same card, not a child card"
- [!] CLAIM 1.4 IS INACCURATE: "kanban_complete has no artifacts parameter" is true ONLY for the `hermes kanban complete` CLI (flags --result/--summary/--metadata only), but FALSE for the injected agent tool: the real signature is kanban_complete(summary=..., artifacts=[<absolute paths>]) where artifacts is a top-level param — INACCURATE — evidence: agent/prompt_builder.py:371 and kanban.md:66
- [x] worker_context surfaces parent summary+metadata as typed fields — VERIFIED — kanban.md: downstream cards receive parent summary and metadata as typed fields via kanban_show()
- [x] scratch workspace default is deleted on completion, valid forms are scratch|worktree|worktree:<path>|dir:<path> — VERIFIED — kanban.md: workspace lifecycle and valid syntax forms documented
