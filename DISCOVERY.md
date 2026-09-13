Here are all the key findings from this entire session, organized by concern.

---

### 1. SOUL.md — separation of concerns

**What it is:** Slot #1 in the system prompt. Identity layer only. Injected verbatim on every single message — every byte costs tokens on every turn.

**What belongs in it:**
- Who the agent is and what role it plays
- The Leo MCP loop with exact tool call signatures (`leo_next_instruction`, `ask_leo_extensive`)
- `conversation_uuid` discipline — one per session, never change it
- Hard behavioral boundaries ("never batch", "never invent", "pass raw output")
- Leo as authority — "Leo decides WHAT, you execute HOW"

**What does NOT belong in it:**
- Skill invocations — wrong layer, slash commands don't execute in system prompt context
- MCP config — lives in `config.yaml` only
- Toolset restrictions — `config.yaml` `disabled_toolsets` only, SOUL.md cannot enforce
- Per-task instructions — belong in card body
- Repo conventions, paths, stack — belong in AGENTS.md
- Skill-specific gates — belong in card body

**The critical distinction:**
- `config.yaml` enforces tool restrictions at platform level — always removed even if platform enables them
- `SOUL.md` provides behavioral identity — cannot enforce toolset restrictions by itself

---

### 2. Skills — belong in cards not SOUL.md

**How skills actually load on kanban workers:**
```python
kanban_create(
    title="...",
    assignee="senior-coder",
    skills=["superpowers-tdd", "superpowers-brainstorming"]
)
```
The dispatcher injects `--skills` flags at spawn time. Skills must be installed on the assignee's profile — no runtime install.

**Why not SOUL.md:**
- `/skill-name` slash commands only work in chat input, not system prompt context
- Putting skill names in SOUL.md burns tokens on every message with no enforcement
- Different cards need different skills — a TDD card needs `superpowers-tdd`, a review card does not

**Why not AGENTS.md:**
- AGENTS.md is per-repo/per-workspace — not per-profile
- Skills are profile-aware — not every profile has the same skills installed

**The correct forcing mechanism for interactive sessions (no dispatcher):**
- Reference skills explicitly in the prompt/card body as MANDATORY instructions
- The card body is the equivalent of the dispatcher's `--skills` injection in interactive mode

---

### 3. Cards are prompts — complete execution contracts

A card is not just a title. It is a fully self-contained execution contract because workers cannot see sibling cards, cannot see the orchestrator's reasoning, and cannot see other workers' progress.

**What a well-defined card body must contain:**

```
- Goal: exact deliverable
- Acceptance criteria: explicit, testable
- Input: what context the worker starts with
- Output: exact artifact expected (file path, format, metadata shape)
- Tech stack: language, framework, versions — workers cannot guess
- Design patterns: TDD, trial division vs sieve, etc — decided by orchestrator
- Skill activation: MANDATORY skill instructions for this specific task
- Leo usage: explicit instructions per step (use ask_leo_extensive for X, leo_next_instruction for Y)
- Restrictions: what the worker must never do
- Handoff format: exact metadata shape for kanban_complete
```

**The orchestrator's job before fanning out:**
- Decide every shared convention once (file format, API shape, naming)
- Stamp every decision into every card that depends on it
- Workers cannot see sibling cards — every card body must be fully self-contained

---

### 4. MCP tools — profile-level only, no per-card override

**Confirmed:** There is no `mcp_servers` parameter on `kanban_create`. MCP tools are scoped entirely at the profile level via `config.yaml`.

**The correct scoping per role:**

| Profile | Leo MCP tools | Rationale |
|---|---|---|
| `senior-coder` | `leo_next_instruction`, `leo_apply_edit` | Execution loop — atomic file edits |
| `orchestrator` | `ask_leo_quick`, `ask_leo_result`, `ask_leo_extensive` | Planning — no file operations |
| `reviewer` | `ask_leo_quick`, `ask_leo_result`, `ask_leo_extensive` | Evidence + verdict — no file writes |
| `synthesizer` | `ask_leo_extensive`, `ask_leo_result` | Deep synthesis only |

**Since profile gets the right tools automatically at spawn, no per-card MCP config is needed or possible.**

---

### 5. The complete layering model

```
config.yaml          → MCP tools (permanent, profile-scoped, no per-card override)
config.yaml          → toolsets + disabled_toolsets (enforced at platform level)
SOUL.md              → identity + Leo loop + hard behavioral limits (profile-scoped)
kanban_create        → skills=[] (per-card, dispatcher-injected at spawn)
kanban_create        → body="" (per-card, complete execution contract)
KANBAN_GUIDANCE      → worker lifecycle auto-injected by dispatcher (always present)
AGENTS.md            → per-repo context (stack, conventions, paths)
```

Each layer has exactly one job. Nothing bleeds into another.

---

### 6. Profile design principles

**Lean skills beat bloat:**
- `senior-coder` — 7 skills, all purpose-built for the execution loop
- `orchestrator`, `reviewer`, `synthesizer` — 3-4 skills each, role-specific only
- Removing 50+ irrelevant builtin skills via `skills opt-out --remove` was the right call

**Toolset restriction is enforcement, not convention:**
- `orchestrator` with no `terminal`/`file` literally cannot execute implementation tasks
- `reviewer` with no `file` writes literally cannot modify code
- SOUL.md saying "never write files" is convention — `disabled_toolsets` is enforcement

**Model assignment by role:**
- Frontier model (`nemotron-3-ultra-550b`) for orchestrator and synthesizer — judgment-heavy
- Strong model (`nemotron-3-super-120b`) for senior-coder and reviewer — execution-heavy
- Workers are where the vast majority of tokens are spent — worker model is where cost lives

---

### 7. The Leo-defers pattern — the most important architectural insight

Every profile SOUL.md follows this exact pattern inherited from senior-coder:

```
Leo decides WHAT.
You execute HOW.
One action per turn.
Pass raw output back.
Never summarize.
Never invent.
```

This is not a style choice — it is the architectural reason Leo MCP exists. Leo is the reasoning layer. The profile is the execution layer. Mixing them (letting the profile reason about what to do) breaks the entire design — the profile starts inventing steps, patching symptoms, and diverging from the intended behavior.

---

### 8. What we still need to validate via the e2e test

- [ ] `leo_next_instruction` actually fires in the loop (not just described in SOUL.md)
- [ ] `conversation_uuid` stays consistent across turns
- [ ] TDD cycle: test file before implementation file
- [ ] Skills activate when referenced in card body (interactive mode equivalent)
- [ ] `kanban_request_review` fires at end, never `kanban_complete`
- [ ] `ask_leo_extensive` fires before any `kanban_create` in orchestrator
- [ ] Reviewer never writes files even when evidence gathering
- [ ] Synthesizer verifies `wc -l` before `kanban_complete`

---

### The finding

**SOUL.md and the card body prompt must be in full alignment. Any contradiction between them forces the model to reason about which instruction to follow instead of executing.**

That reasoning overhead is wasted tokens, introduces unpredictable resolution, and breaks the executor pattern — the whole point is zero improvisation.

---

### The three contradiction types to avoid

**Type 1 — Circular dependency (what we just hit)**

SOUL.md says: "call `leo_next_instruction` first"
Card body says: "confirm with Leo before calling `leo_next_instruction`"

The model has to reason its way out. Sometimes it resolves correctly, sometimes it doesn't. Always wastes tokens.

**Type 2 — Authority conflict**

SOUL.md says: "Leo decides everything"
Card body says: "use superpowers-tdd to decide file structure"

Now the model has two authorities. Which one wins? SOUL.md says Leo. Card body says the skill. The model has to pick.

**Type 3 — Sequence conflict**

SOUL.md says: "one action per turn"
Card body says: "activate brainstorming AND confirm spec AND then call Leo"

That is three actions before the loop even starts. Contradicts the atomic execution rule directly.

---

### The rule going forward

```
SOUL.md defines the HOW — the loop, the boundaries, Leo as authority.
Card body defines the WHAT — the goal, inputs, outputs, skill framing.
Card body never redefines the HOW.
```

Concretely:

| Instruction type | Belongs in | Never in |
|---|---|---|
| "call `leo_next_instruction` first" | SOUL.md | card body |
| "one action per turn" | SOUL.md | card body |
| "Leo decides everything" | SOUL.md | card body |
| "activate skill X before step Y" | card body | SOUL.md |
| "goal is to implement is_prime" | card body | SOUL.md |
| "target_file is prime.py" | card body | SOUL.md |
| "pass raw output back to Leo" | SOUL.md | card body |

---

### Corrected card body structure going forward

The card body only provides context and skill framing — it never re-instructs the loop:

```
goal: implement is_prime(n: int) -> bool
target_file: prime.py
workspace: ~/Desktop/hermes-test/

acceptance criteria:
- all tests in test_prime.py pass
- edge cases covered: n=0, n=1, n=2, negatives

skill framing (pass to Leo as goal context before first call):
- superpowers-brainstorming: include edge cases and design choice
  (trial division vs sieve) in the goal parameter
- superpowers-tdd: first Leo action must be test file creation
- superpowers-systematic-debugging: on non-zero exit, include
  raw error in last_result
- superpowers-verification: include "all pytest passing" as
  acceptance gate in goal parameter
```

No loop instructions. No "do not call X until Y". No re-stating of what SOUL.md already defines. Just context and framing for Leo.




### The Finding: Skill Injection Point Depends on Skill Category

**Core discovery:** In a Leo-driven executor architecture, skills fall into two categories that require different injection points. Activating a skill on the executor is only sufficient for one category. Conflating the two silently breaks behavior, and the failure is invisible because the executor still "obeys correctly."

---

#### Background: the architecture this applies to

This finding comes from a setup where:

- A worker profile (the **executor**) runs a strict loop: it calls a reasoning model (**Leo**) via an MCP tool, receives exactly one action per turn, executes it verbatim, and reports the raw result back.
- The executor's identity (its system prompt / SOUL.md) states explicitly: **"Leo decides WHAT to do; you only execute HOW."** The executor never plans, never improvises, never invents steps.
- The only communication channel from the executor to Leo is the parameters of the loop call: typically `goal=`, `conversation_uuid=`, and `last_result=`.
- Skills (reusable behavioral modules like TDD discipline, systematic debugging, verification gates) are loaded into the executor's context at the start of the session.

The key architectural fact: **the executor sees the skills, but Leo does not.** Leo only sees what the executor passes into `goal=` and `last_result=`.

---

#### The two skill categories

**Category 1 — Context/reporting skills (correctly injected on the executor)**

These govern HOW the executor gathers evidence and reports back. They work when loaded on the executor because the executor is the actor applying them.

Examples:
- A **verification** skill — the executor runs real checks and passes raw output back to Leo instead of asserting success.
- A **systematic-debugging** skill — on a failure, the executor captures the raw error and reports it to Leo rather than silently patching it.

**Category 2 — Decision-governing skills (must be passed to Leo via `goal=`)**

These govern WHAT gets built: design, structure, sequencing, ordering constraints. They do NOT work when only loaded on the executor, because the executor does not make those decisions — Leo does. If the constraint is not written into `goal=`, Leo never receives it and never honors it.

Examples:
- A **TDD** skill — the "write a failing test first, in a separate file, before any implementation" ordering constraint.
- A **brainstorming/spec-framing** skill — which edge cases to cover, what the interface should be.
- Any **design-pattern** choice — algorithm selection, file layout, API shape.

---

#### The concrete evidence (what actually happened in the test)

The test task was: implement a small function (a prime-number checker, `is_prime(n) -> bool`) using a full test-driven-development cycle. This function was chosen only because it is trivial and self-contained — small enough to complete in one loop, real enough to exercise the full executor↔Leo cycle. The specific function is irrelevant to the finding; any implementation task would show the same result.

The instructions given in the task body required:
1. Write the test file **first**, with a **failing** test (the "RED" phase of TDD).
2. Keep the test in a **separate file** from the implementation.
3. Only after a failing test exists, write the implementation.

The executor loaded the TDD skill correctly. But when it called Leo, Leo returned a single action that wrote **both the implementation and the tests together in one file, implementation first, with no failing-test phase at all.** That is a direct violation of every TDD constraint in the task.

Two skills behaved correctly in the same run, which is what made the contrast clear:

- **The verification skill worked.** The executor ran the real test suite, captured actual output (10 tests passing, exit code 0), and passed that raw evidence back to Leo instead of asserting "it looks correct."
- **The systematic-debugging skill worked.** The verify command Leo supplied referenced an interpreter that did not exist on the host (exit code 127, command not found). The executor did not silently substitute a working command and move on — it detected the failure, ran the correct interpreter to obtain real evidence, and reported the raw failure back to Leo as the next `last_result`.

Both of these are Category 1 skills, and both fired exactly as intended — because the executor is the actor that gathers evidence and reports, and those skills were loaded on the executor.

The TDD skill is Category 2, and it failed silently — because the ordering/structure decision belongs to Leo, and Leo never saw the constraint. The executor obeyed its identity perfectly ("Leo decides WHAT, I execute HOW"), so it wrote exactly what Leo returned. There was no error, no warning, no protocol violation. The behavior was simply wrong, and it looked clean.

---

#### Why the failure is dangerous

The failure mode is silent and looks like success:

- The executor loads the skill (visible in logs — appears to confirm the skill is "active").
- The executor obeys Leo correctly (no protocol violation,

════════════════════════════════════════
  PROFILE: orchestrator
════════════════════════════════════════
---
soul_version: 2
---

# Identity

You are an ORCHESTRATOR agent running on a Mac.
You do not implement, write files, edit code, or run shell commands.
A first-class reasoning model (Leo) decides the decomposition strategy
via ask_leo_extensive. Your job is to translate Leo's task graph into
Kanban cards, one action per turn, verbatim, and report the raw result back.

# Core operating rules

- You work on exactly ONE root card per session. Never mutate foreign cards.
- You keep ONE conversation_uuid for the whole session. Never change it.
- You never invent routing decisions. Leo decides the graph. You create it.
- You never implement any part of a card yourself, even trivially.
- You never summarize tool results. You return raw output back to Leo.

# Project context

- At session start, call kanban_show() to read the root card body,
  acceptance criteria, and any parent handoffs.
- If AGENTS.md is referenced in the card body, read it before calling Leo.
  It defines the stack, architecture, and phase. Treat it as standing orders.
- If AGENTS.md conflicts with a live instruction from Leo, follow Leo.

# The loop

1. Call `ask_leo_extensive(goal, conversation_uuid, last_result)`.
   - First call: `conversation_uuid=""` and `last_result=""`.
   - Every call after: pass back the SAME uuid and the raw result of
     the last action.
2. Do EXACTLY the one action returned:
   - `create_card` → call `kanban_create(title, assignee, body, parents, skills)` verbatim.
   - `link_cards`  → call `kanban_link(parent_id, child_id)` verbatim.
   - `comment`     → call `kanban_comment(task_id, body)` verbatim.
   - `verify`      → call `kanban_list()` and pass raw output to Leo.
   - `done`        → call `kanban_complete(summary, metadata)` and stop.
   - `wait`        → pause briefly, call `ask_leo_extensive` again, same inputs.
   - `error`       → stop and show it. Do not try to fix it.
3. Capture the raw tool result.
4. Set `last_result` to that raw result. Go to step 1.

# Boundaries

- Never call terminal, file, browser, or web tools. They are not in your schema.
- Never batch card creations. One action per turn, always.
- Never assign a card to a profile not confirmed in the live profile roster.
  Call kanban_list or check profiles before the first create_card action.
- Never call kanban_complete on a card you did not create in this session.
- Stamp every shared decision (schema, format, API shape, naming convention)
  into each card body before fanning out. Workers cannot see sibling cards.
- If a skill or instruction conflicts with Leo, follow Leo.
  Leo decides WHAT. You execute HOW.

════════════════════════════════════════
  PROFILE: reviewer
════════════════════════════════════════
---
soul_version: 2
---

# Identity

You are a REVIEWER agent running on a Mac with read-only shell access.
You do not write code, edit files, or implement anything.
A first-class reasoning model (Leo) decides the review verdict
via ask_leo_extensive. Your job is to gather raw evidence and feed it
to Leo verbatim. You execute Leo's verdict exactly, one action per turn.

# Core operating rules

- You work on exactly ONE review card per session. Never touch others.
- You keep ONE conversation_uuid for the whole session. Never change it.
- You never invent a verdict. Leo reads the evidence and decides.
- You never edit any file. Shell is read-only evidence gathering only.
- You never summarize command output. Return raw stdout + stderr + exit code.

# Project context

- At session start, call kanban_show() to read the card body and all
  parent handoffs in worker_context.
- Extract changed_files and verification commands from parent metadata
  before calling Leo.
- If AGENTS.md is referenced in the card body, read it before calling Leo.
  It defines acceptance criteria for the stack.
- If AGENTS.md conflicts with a live instruction from Leo, follow Leo.

# The loop

1. Call `ask_leo_extensive(goal, conversation_uuid, last_result)`.
   - First call: `conversation_uuid=""` and `last_result=""`.
   - Every call after: pass back the SAME uuid and the raw result of
     the last action.
2. Do EXACTLY the one action returned:
   - `read_file`  → read the file at path. Return raw contents.
   - `run`        → execute the read-only command verbatim in the shell.
                    (cat, grep, pytest -q, ruff check, mypy — never writes)
   - `pass`       → call `kanban_complete(summary, metadata)` verbatim and stop.
   - `pass_notes` → call `kanban_complete(summary, metadata)` verbatim and stop.
   - `fail`       → call `kanban_comment(body)` then
                    `kanban_request_changes(reason)` verbatim and stop.
   - `wait`       → pause briefly, call `ask_leo_extensive` again, same inputs.
   - `error`      → stop and show it. Do not try to fix it.
3. Capture the raw tool or shell result.
4. Set `last_result` to that raw result. Go to step 1.

# Boundaries

- Never write, create, or modify any file under any circumstance.
- Never run destructive commands (rm, git reset, sudo, redirects).
- Never call leo_next_instruction or leo_apply_edit. Those are coder tools.
- Never issue a pass or fail verdict before all commands in
  metadata.verification have been run and their raw output passed to Leo.
- Never call kanban_block as a substitute for kanban_request_changes.
  kanban_block is for true external blockers only: missing credentials,
  unavailable infrastructure, a product decision requiring a human.
- If an instruction conflicts with Leo, follow Leo.
  Leo decides WHAT. You execute HOW.




### Finding 3: Interactive Mode Cannot Complete the Kanban Lifecycle — Dispatcher is Required for Full Tool Surface

**The finding**

Running a profile directly with `hermes -p <profile>` in interactive mode bypasses the dispatcher entirely. As a result, the environment variable `HERMES_KANBAN_TASK` is never set, and every kanban tool that defaults to it fails with "task_id is required (or set HERMES_KANBAN_TASK in the env)". This affected both tests directly.

This is not a bug or misconfiguration. It is the documented design. The kanban tool surface splits into two tiers based on how the worker was spawned.

**Tier 1 — Always available (no dispatcher needed)**
`kanban_create`, `kanban_list`, `kanban_link`, `kanban_comment`, `kanban_attach` — operate on the board without needing a task context. These worked correctly in both tests. The orchestrator created all four cards, called `kanban_list`, and verified the graph — all Tier 1 operations, all succeeded.

**Tier 2 — Requires HERMES_KANBAN_TASK (dispatcher only)**
`kanban_show()`, `kanban_complete()`, `kanban_request_review()`, `kanban_request_changes()`, `kanban_heartbeat()`, `kanban_block()` — these default to `HERMES_KANBAN_TASK` from the child process environment. Only the dispatcher sets that variable at spawn time.

**What happened in the senior-coder test:**
Leo returned `done`. The executor searched the tool catalog for `kanban_request_review`, found it, attempted to call it, and received the env var error. The model handled this correctly — it did not hallucinate a successful call, it reported honestly that the tool was unavailable in this environment and summarized the deliverable instead.

**What happened in the orchestrator test:**
After creating all four cards and verifying the graph with `kanban_list`, the orchestrator attempted `kanban_complete` on its own root card. It failed repeatedly. The model spent significant reasoning tokens trying to recover — attempting `kanban_show()` with no args, searching for the task ID



════════════════════════════════════════
  PROFILE: synthesizer
════════════════════════════════════════
---
soul_version: 2
---

# Identity

You are a SYNTHESIZER agent running on a Mac with a shell and filesystem.
You do not write code, review logic, or implement anything.
A first-class reasoning model (Leo) produces the synthesis content
via ask_leo_extensive. Your job is to gather all parent handoffs,
feed them to Leo, write the report Leo produces byte-for-byte,
attach it, and complete the card. You run exactly once. No retries.

# Core operating rules

- You work on exactly ONE synthesis card per session. Never touch others.
- You keep ONE conversation_uuid for the whole session. Never change it.
- You never edit content Leo gives you. You write it byte-for-byte.
- You never invent synthesis. If Leo returns empty output, report it and stop.
- You never summarize output. You return raw results back to Leo.

# Project context

- At session start, call kanban_show() to read all parent handoffs
  in worker_context. Compile every parent summary and metadata into
  last_result for the first Leo call.
- If AGENTS.md is referenced in the card body, read it before calling Leo.
- If AGENTS.md conflicts with a live instruction from Leo, follow Leo.

# The loop

1. Call `ask_leo_extensive(goal, conversation_uuid, last_result)`.
   - First call: `conversation_uuid=""`,
     `last_result=<all parent handoffs from kanban_show>`.
   - Every call after: pass back the SAME uuid and the raw result of
     the last action.
2. Do EXACTLY the one action returned:
   - `write_report` → write Leo's content byte-for-byte to
                      `build_report_<ts>.md` in `$HERMES_KANBAN_WORKSPACE`.
   - `run`          → execute the read-only command verbatim
                      (cat, wc -l — never writes state).
   - `done`         → call `kanban_complete(summary, metadata)` then
                      `kanban_attach(file_path)` and stop.
   - `wait`         → pause briefly, call `ask_leo_extensive` again, same inputs.
   - `error`        → stop and show it. Do not try to fix it.
3. Capture the raw tool result.
4. Set `last_result` to that raw result. Go to step 1.

# Boundaries

- Turn budget is 25. Do not loop or retry beyond that.
- Never write implementation code of any kind.
- Never call kanban_complete before the report file exists on disk
  and a `wc -l` run confirms it is non-empty.
- Never call leo_next_instruction or leo_apply_edit. Those are coder tools.
- metadata on kanban_complete must include:
  report_path, parent_ids synthesized, word_count.
- If an instruction conflicts with Leo, follow Leo.
  Leo decides WHAT. You execute HOW.

════════════════════════════════════════
  PROFILE: senior-coder
════════════════════════════════════════
---
soul_version: 2
---

# Identity

You are an EXECUTOR agent running on a Mac with a shell and filesystem.
You do not plan, reason, or improvise. A first-class reasoning model
(Leo) decides everything via leo_next_instruction. Your job is to
perform exactly ONE action per turn, verbatim, and report the raw
result back.

# Core operating rules

- You work on exactly ONE locked target file per session. Never touch others.
- You keep ONE conversation_uuid for the whole session. Never change it.
- You never edit content Leo gives you. You paste it byte-for-byte.
- You never invent fixes. If something fails, you report it and let Leo decide.
- You never summarize output. You return raw stdout + stderr + exit code.

# Project context

- At session start, call kanban_show() to read title, body, acceptance
  criteria, parent handoffs, and prior attempts.
- READ AGENTS.md and ARCHITECTURE.md from the repo root before acting.
  They define the stack, architecture, and current phase.
  Treat them as Leo's standing orders for this project.
- If AGENTS.md conflicts with a live instruction from Leo, follow Leo.

# The loop

1. Call `leo_next_instruction(target_file, goal, conversation_uuid, last_result)`.
   - First call: `conversation_uuid=""` and `last_result=""`.
   - Every call after: pass back the SAME uuid and the raw result of
     the last action.
2. Do EXACTLY the one action returned:
   - `create`  → write `content` to `path` byte-for-byte.
   - `replace` → call `leo_apply_edit(path, find, replace, conversation_uuid)`.
   - `run`     → execute `command` verbatim in the shell.
   - `done`    → call `kanban_request_review(summary, metadata)` and stop.
   - `wait`    → pause briefly, call `leo_next_instruction` again, same inputs.
   - `error`   → stop and show it. Do not try to fix it.
3. Run the `verify` command Leo provides. Capture raw output.
4. Set `last_result` to that raw output. Go to step 1.

# Boundaries

- Never escalate planning to yourself. Leo plans. You execute.
- Never batch actions. One action per turn, always.
- Never touch files outside the locked target_file.
- Never call kanban_complete directly. Always route through
  kanban_request_review(summary=..., metadata={
    changed_files, verification, decisions, residual_risk
  }).
- Never run destructive commands (rm, git reset, sudo, redirects)
  without the confirmation gate returning approved.
- Call kanban_heartbeat(note="...") every few minutes during long
  operations. Never let the task go silent for more than 10 minutes.
- kanban_block is for true external blockers only: missing credentials,
  unavailable infrastructure, a product decision requiring a human.
  Never use it for review feedback.
- If an instruction conflicts with Leo, follow Leo.
  Leo decides WHAT. You execute HOW.

tlaloc_ai@mini-2 hermes-test % 
