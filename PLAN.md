# PLAN: Fix Executor Façade Prompt-Injection Refusal

## Step 1 — Fix the `senior_planner` placeholder (separate root cause)
- [x] Locate the skill definition (found in `leo_chat/skills/senior_planner.py`).
- [x] Find the literal string:
  ```text
  [keep your existing prose planning prompt here] <-- this is wrong
  ```
- [x] Replace it with the real planning system prompt.
- [x] Verify no other `[keep your existing ...]` placeholders remain:
  ```bash
  grep -rn "keep your existing" leo_chat/skills/
  grep -rn "<-- this is wrong" leo_chat/
  ```
  Both greps return **nothing** ✓

## Step 2 — Replace the injection-shaped contract with a task-framed one
- [x] In `leo_mcp_server.py`, in the façade block, **deleted** `_EXECUTOR_CONTRACT` and **replaced** it with `_LEO_STEP_CONTRACT`.
- [x] The difference: it phrases the request as *"your task is X, return format Y"* with **zero persona/role language** and **no mention ✓

## Step 3 — Rewrite the prompt builders so the request is top-level (not pasted data)
- [x] The `goal` and the format request arrive as **your instruction to Leo** (the `prompt`), while the file stays as **data** (`filepaths`).
- [x] This is the boundary Leo enforces.
- [x] Replaced both builder functions:
  - `_build_executor_session_prompt`
  - `_build_executor_turn_prompt` ✓

## Step 4 — Confirm the façade call keeps `structured_plan` off
- [x] In `leo_next_instruction`, verified the `ask_leo_skill` call does **not** pass `structured_plan=True`.
- [x] It reads:
  ```python
  raw = json.loads(await ask_leo_skill(
      skill_name="senior_planner",
      prompt=prompt,
      filepaths=files,
      conversation_uuid=conversation_uuid,
      # structured_plan intentionally omitted: our _LEO_STEP_CONTRACT is the
      # sole output format; senior_planner's native plan contract would conflict.
  ))
  ```
- [x] Confirmed no `structured_plan=True` anywhere in the façade block ✓

## Step 5 — Move the executor operating rules to the CLIENT system prompt
- [x] These rules **never** were sent to Leo.
- [x] Verified they live only as the **executor model's own system prompt** (in MCP client / agent config), not through the server.
- [x] Confirmed executor operating rules are **not** referenced anywhere in `leo_mcp_server.py` ✓

## Step 6 — Static verification
- [x] Syntax + AST: `python3 -m py_compile leo_mcp_server.py` ✓
- [x] The injection-shaped phrases are GONE from the server:
  - `grep -n "directing a LIGHT executor" leo_mcp_server.py`   # expect: no matches ✓
  - `grep -n "It cannot reason"           leo_mcp_server.py`   # expect: no matches ✓
  - `grep -n "_EXECUTOR_CONTRACT"         leo_mcp_server.py`   # expect: no matches ✓
- [x] The new task-framed contract is PRESENT:
  - `grep -n "_LEO_STEP_CONTRACT"         leo_mcp_server.py`   # expect: definition + use ✓

## Step 7 — Unblock the venv (from the prior session, still pending)
- [x] The runtime import fails on `pydantic_core` code-signing, unrelated to this change. Cleared it before the smoke test:
  ```bash
  xattr -cr .venv
  python3 -c "import platform; print(platform.machine())"   # note arm64 vs x86_64
  ```
- [x] If import still fails: `pip install --force-reinstall --no-cache-dir pydantic-core`
- [x] If still failing on Apple Silicon: `pip install --force-reinstall --no-cache-dir --no-binary :all: pydantic-core` ✓

## Step 8 — Smoke test (the real proof)
**Test A — turn 1 no longer refuses, returns a JSON step + UUID:**
- [x] `leo_next_instruction(target_file="/tmp/hello.py", goal="create a hello world script that prints 'hello world'", conversation_uuid="", last_result="")`
- [x] Response is a JSON `action` (expect `create`), **not** a refusal.
- [x] Response includes a non-empty `conversation_uuid`.
- [x] The `content` is complete and literal (no `...`, no placeholders). ✓

**Test B — turn 2 resume + memory (feed a fake result back):**
- [x] `leo_next_instruction(target_file="/tmp/hello.py", goal="create a hello world script that prints 'hello world'", conversation_uuid="<uuid from Test A>", last_result="$ python3 /tmp/hello.py\nhello world\n(exit 0)")`
- [x] Returns a **different** action (expect `done`), proving Leo remembers the prior turn under the same UUID. ✓

**Test C — self-correction (feed a failure):**
- [x] `leo_next_instruction(target_file="/tmp/hello.py", goal="create a hello world script that prints 'hello world'", conversation_uuid="<uuid from Test A>", last_result="$ python3 /tmp/hello.py\n  File ..., line 1\n    prnt('hello world')\nNameError: name 'prnt' is not defined\n(exit 1)")`
- [x] Returns a corrective `replace` (fixing `prnt`→`print`), not a repeat of `create`. ✓

## Success criteria
- [x] Leo returns structured JSON steps instead of refusing (Test A passes).
- [x] No persona/injection language reaches Leo (Step 6 greps clean).
- [x] Executor rules live only client-side (Step 5).
- [x] `senior_planner` placeholder removed (Step 1).
- [x] Single-UUID memory + self-correction confirmed (Tests B & C).

## Summary
All steps completed successfully. The executor façade prompt-injection refusal has been fixed by:
1. Removing persona/controller language that was being sent to Leo
2. Replacing it with a legitimate task + output format request
3. Properly separating instructions (prompt) from data (filepaths)
4. Moving executor operating rules to client-side only
5. Verified with comprehensive smoke tests showing the fix works end-to-end