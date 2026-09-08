"""
Leo Chat Integration - Unified Flow for MCP Server

Full E2E: page.connect() → send() → polling/sqlite

Demonstrates how to unify:
1. Skill selection
2. Context building
3. Silent injection (CDP)
4. Streaming detection (SQLite polling)
"""

import asyncio
import logging
import time
from typing import Callable, List, Optional, Tuple

from leo_chat.skills.skill_factory import SkillFactory
from leo_chat.context.prompt_builder import assemble_context
from leo_chat.pages.brave_leo_page import BraveLeoPage
from leo_chat.patch_writer import (
    patch_response_complete,
    edit_response_complete,
    PATCH_END,
    EDIT_END,
    TERMINAL_TOKEN,
)
from leo_chat.patch_validator import PatchValidator, RECOVERABLE_STAGES
from leo_chat.helpers import _log_json

logger = logging.getLogger(__name__)

# Phase timing helper: emit a structured event with elapsed-ms relative to an
# explicit per-call t0, so per-phase durations can be diffed from mcp_debug.log
# without touching the Python logging config.
def _phase_start(event: str, t0: float, **kwargs) -> None:
    _log_json("INFO", event, elapsed_ms=int((time.monotonic() - t0) * 1000), **kwargs)


# Safety budget: reserve headroom below Leo's hard single-message limit so the
# compiled prompt (system + contract + user + context) never triggers the
# chunked path. `BraveLeoPage.LEO_MAX_PROMPT_CHARS` (18k) is the hard cap the
# page uses; we stay a few hundred chars under it to absorb any wrapper markup.
_PROMPT_MAX_TOTAL_CHARS = BraveLeoPage.LEO_MAX_PROMPT_CHARS - 500
_CONTEXT_TRUNCATION_MARKER = (
    "\n\n[... document truncated: too large to send in full over Leo's single-"
    "message limit. The beginning of the file is shown above; ask for the "
    "remaining sections if needed ...]\n"
)

# ── Dynamic response-timeout budget ──────────────────────────────────────────
# Leo's generation time grows with (a) how many turns the delivery required and
# (b) how much content was injected. A fixed timeout that is generous enough for
# a single short turn can be far too tight for a multi-turn delivery of a large
# document, causing the poller to give up mid-generation and surface a false
# "timeout"/empty response. Scale the budget linearly and clamp to a ceiling.
_RESPONSE_TIMEOUT_BASE_SEC = 120       # floor for a single, short turn
_RESPONSE_TIMEOUT_PER_TURN_SEC = 30    # extra budget per additional delivery turn
_RESPONSE_TIMEOUT_PER_10K_CHARS = 15   # extra budget per ~10k chars of context
_RESPONSE_TIMEOUT_MAX_SEC = 600        # hard ceiling (10 min)


def _compute_response_timeout_sec(total_sections: int, total_chars: int) -> int:
    """Compute a response-poll timeout sized to the actual work.

    Args:
        total_sections: Number of delivery turns (>= 1).
        total_chars: Total injected context/prompt size in characters.

    Returns:
        A timeout in seconds, clamped to [_RESPONSE_TIMEOUT_BASE_SEC,
        _RESPONSE_TIMEOUT_MAX_SEC].
    """
    extra_turns = max(0, total_sections - 1)
    chars_budget = int(total_chars / 10_000) * _RESPONSE_TIMEOUT_PER_10K_CHARS
    timeout = (
        _RESPONSE_TIMEOUT_BASE_SEC
        + extra_turns * _RESPONSE_TIMEOUT_PER_TURN_SEC
        + chars_budget
    )
    return max(_RESPONSE_TIMEOUT_BASE_SEC, min(timeout, _RESPONSE_TIMEOUT_MAX_SEC))


# Sentinel returned as the response text when the generation did not complete
# within the (dynamic) budget but a conversation UUID was resolved. The server
# maps this to an explicit status:"still_working" result (carrying the UUID so
# the caller can resume) instead of a misleading success or a bare client-side
# timeout. It is chosen to be un-collidable with real Leo prose.
_INCOMPLETE_RESPONSE_SENTINEL = "\x00LEO_GENERATION_INCOMPLETE\x00"


def _cap_file_context(
    file_context: str,
    user_prompt: str,
    structured: bool,
    skill,
) -> str:
    """Truncate the injected <context> so the final prompt fits in one turn.

    Preserves the user instruction and any structured output contract verbatim
    and only shrinks the file-context block (which is what balloons for large
    documents like PLAN.md). Returns ``file_context`` unchanged when the final
    prompt would already be under the cap, so small requests are byte-identical
    to the previous behavior.

    Args:
        file_context: Assembled file-context string (may be "").
        user_prompt:  The user's instruction.
        structured:   Whether structured output is requested.
        skill:        The resolved skill (to measure non-context parts exactly).

    Returns:
        The (possibly truncated) file-context string.
    """
    if not file_context:
        return file_context

    # Measure the fixed overhead: everything in get_full_prompt OTHER than the
    # <context> block. We can't easily call get_full_prompt with empty context
    # and diff reliably (the contract/system may vary), so rebuild the same
    # wrapper the skill uses (via its own get_full_prompt) with an empty context
    # and compare lengths — this is exact and skill-portable.
    overhead = len(skill.get_full_prompt(user_prompt, "", structured=structured))
    # get_full_prompt with context adds "<context>\n{...}\n</context>\n" wrapper.
    wrapper_len = len("<context>\n") + len("\n</context>\n")

    budget = _PROMPT_MAX_TOTAL_CHARS - overhead - wrapper_len
    if len(file_context) <= budget:
        return file_context

    logger.warning(
        f"⚠️ File context {len(file_context)} chars exceeds budget {budget}; "
        f"truncating to keep the prompt under {_PROMPT_MAX_TOTAL_CHARS} chars "
        f"(avoid chunked path)."
    )
    # Reserve room for the truncation marker itself.
    marker_len = len(_CONTEXT_TRUNCATION_MARKER)
    keep = max(0, budget - marker_len)
    return file_context[:keep] + _CONTEXT_TRUNCATION_MARKER

# Instruction prepended to every non-final "hold" turn when a large file is
# delivered across consecutive turns. Leo must ack receipt WITHOUT answering,
# so the fragments stay in-conversation and the real task is deferred to the
# final turn. Kept terse and generic so it works for any skill.
_HOLD_INSTRUCTION_TEMPLATE = (
    "You are receiving part {idx} of {total} of a document (delivered across "
    "multiple messages so it fits within a single-message length limit). "
    "Do NOT answer any question yet and do NOT analyze this part. Reply with "
    "only this short acknowledgement: 'Part {idx}/{total} received.'"
)
_FINAL_SECTION_INSTRUCTION = (
    "This is the final part ({idx}/{total}) of the document. The complete "
    "document is now loaded. Below is your actual task.\n\n"
)


def _plan_context_delivery(
    file_context: str,
    user_prompt: str,
    structured: bool,
    skill,
) -> List[str]:
    """Decide how to deliver file context: one turn, or split across turns.

    Returns a list of "context sections" to inject. A single-element list means
    the context is delivered in ONE normal turn (the fast path; identical to
    historical behavior). More than one element means the file was too large
    for a single turn WITHOUT truncation, so it is split on natural boundaries
    into N sections; each section becomes its own turn (a "hold" turn for
    sections 1..N-1 and the real instruction dispatch for section N).

    This deliberately AVOIDS truncation (which silently drops the tail of a
    large document) and AVOIDS the chunked path (which forces Leo to generate
    a response between fragments and can lose context if interrupted).

    Args:
        file_context: Assembled file-context string (may be "").
        user_prompt:  The user's instruction.
        structured:   Whether structured output is requested.
        skill:        The resolved skill.

    Returns:
        List of context strings (length >= 1). Length 1 is the common case.
    """
    if not file_context:
        return [file_context]

    # Overhead of the FINAL turn's non-context parts (system + contract + user
    # instruction), measured exactly via the skill's own get_full_prompt.
    final_overhead = len(
        skill.get_full_prompt(
            _FINAL_SECTION_INSTRUCTION.format(idx=0, total=1) + user_prompt,
            "",
            structured=structured,
        )
    )
    # A "hold" turn's non-context part: system + hold instruction (no contract).
    hold_overhead = len(
        skill.get_full_prompt(
            _HOLD_INSTRUCTION_TEMPLATE.format(idx=0, total=1),
            "",
            structured=False,
        )
    )
    wrapper_len = len("<context>\n") + len("\n</context>\n")

    # Section budget: fit inside a single turn under the total cap. Use the
    # LARGER of the two overheads so ANY turn (hold or final) stays under cap.
    overhead = max(final_overhead, hold_overhead)
    budget = _PROMPT_MAX_TOTAL_CHARS - overhead - wrapper_len

    if len(file_context) <= budget:
        return [file_context]

    # Split on natural boundaries (paragraph > newline > sentence > space).
    sections = BraveLeoPage._split_prompt_at_boundaries(file_context, budget)
    sections = [s for s in sections if s and s.strip()]
    if len(sections) <= 1:
        # Degenerate split (e.g. single huge token): fall back to a single
        # truncated section rather than looping forever.
        return [_cap_file_context(file_context, user_prompt, structured, skill)]

    return sections


def _structured_sentinel(skill) -> Optional[str]:
    """Return the terminal marker a structured skill's output must contain.

    The planner terminates its JSON plan with <<<LEO_DONE>>>; the patch and edit
    skills terminate their envelopes with their own end markers. This marker is
    threaded into the structured poller so it only returns an entry that is
    genuinely complete (marker present), never a stabilized mid-commit snapshot.
    Returns None for skills that do not produce structured output.
    """
    skill_id = getattr(skill, "skill_id", "")
    if skill_id == "code_editor":
        return EDIT_END
    if getattr(skill, "supports_patch_output", False):
        return PATCH_END
    if skill_id == "senior_planner":
        return TERMINAL_TOKEN
    return None


async def execute_leo_flow(
    skill_name: str,
    user_prompt: str,
    filepaths: Optional[List[str]] = None,
    model_override: Optional[str] = None,
    stream: bool = False,
    conversation_uuid: Optional[str] = None,
    structured: bool = False,
) -> Tuple[str, str]:
    """Execute the unified Leo Chat E2E flow.

    Pipeline:
    1. Retrieve the skill profile from the factory.
    2. Build the master prompt (skill instructions + file context), injecting
       the skill's structured output contract when structured=True.
    3. Inject silently via CDP (no focus stealing).
    4. Wait for completion (SQLite streaming or DOM polling).
    5. Return the generated response and the resolved conversation UUID.

    When conversation_uuid is provided, the flow navigates to the existing
    conversation and sends only the new prompt as a follow-up; no history is
    re-injected because Leo already holds the prior context.

    In structured mode, streaming is used to reliably capture the full payload.
    For the patch path (skills with supports_patch_output), auto-continuation
    is sentinel-aware: it keeps requesting continuations until the patch
    envelope's terminal marker appears, since raw file content can be stitched
    back together safely (unlike JSON, which cannot recover from truncation).
    Other structured output (e.g. the planner's JSON) skips continuation, as
    before, since it is not detected on a truncated response.

    Args:
        skill_name: Target skill (e.g. "code_refiner", "senior_planner").
        user_prompt: The core instruction or query.
        filepaths: Absolute file paths to read and inject as context.
        model_override: Optional model identifier overriding the skill default.
        stream: Force SQLite streaming; auto-enabled in structured mode.
        conversation_uuid: If provided, resumes that existing conversation.
        structured: If True, inject the skill's strict output contract and use
            the structured-safe capture path.

    Returns:
        Tuple[str, str]: (Leo's response, resolved conversation UUID).

    Raises:
        ValueError: If the requested skill does not exist.
        RuntimeError: If CDP connection or injection fails.
    """
    logger.info(
        f"🚀 Initiating Leo Flow: skill={skill_name}, "
        f"prompt_len={len(user_prompt)}, stream={stream}, structured={structured}"
    )
    _t0 = time.monotonic()
    _log_json("INFO", "leo_flow_phase_begin", phase="flow",
              skill=skill_name, stream=stream, structured=structured)
    _phase_start("leo_flow_phase", _t0, phase="skill_lookup")

    # 1. Retrieve the skill.
    logger.debug(f"1. Fetching skill '{skill_name}'...")
    try:
        skill = SkillFactory.get_skill(skill_name)

        if model_override:
            skill.model_key = model_override
            logger.debug(f"   Model override applied: {skill.model_key}")

        logger.debug(f"   ✅ Skill retrieved: {repr(skill)}")

    except ValueError as e:
        logger.error(f"❌ Skill lookup failed: {e}")
        available = ", ".join(SkillFactory.list_skills().keys())
        raise ValueError(
            f"Skill '{skill_name}' does not exist. Available: {available}"
        )

    _phase_start("leo_flow_phase", _t0, phase="skill_lookup_done")

    # 2. Build the master prompt. Patch-capable skills must receive the file
    #    byte-faithfully (no line numbers, no metadata, no whitespace stripping)
    #    so the model can reproduce it exactly; other skills get annotated
    #    context that is easier to reason about.
    logger.debug("2. Assembling master prompt context...")
    raw_context = getattr(skill, "supports_patch_output", False)
    file_context = assemble_context(filepaths, raw=raw_context) if filepaths else ""

    # When resuming, ensure the previous response finished before sending a
    # follow-up. History is not re-injected: navigating to the real UUID means
    # Leo already holds the full context.
    if conversation_uuid:
        from leo_chat.db.leo_read import is_response_complete, wait_for_completion

        logger.info(
            f"📋 Verifying conversation {conversation_uuid[:8]}... "
            f"is complete before resuming"
        )
        if not is_response_complete(conversation_uuid):
            logger.warning(
                f"⚠️ Conversation {conversation_uuid[:8]}... still generating, "
                f"waiting up to 120s..."
            )
            prev_raw = wait_for_completion(conversation_uuid, timeout=120)
            if prev_raw:
                logger.info(
                    f"✅ Conversation {conversation_uuid[:8]}... completed "
                    f"({len(prev_raw)} chars)"
                )
            else:
                logger.warning(
                    f"⚠️ Conversation {conversation_uuid[:8]}... still incomplete "
                    f"after timeout, proceeding anyway"
                )
        else:
            logger.debug(f"✅ Conversation {conversation_uuid[:8]}... is complete")

    # Inject the skill's structured contract uniformly when requested; each
    # skill supplies its own directive, so no per-skill branching is needed.
    #
    # Large file context is handled in one of two ways (decided here):
    #   * Fits within the single-turn budget → sent as one normal turn.
    #   * Exceeds it → the file is SPLIT ON NATURAL BOUNDARIES and injected as
    #     consecutive turns over ONE conversation UUID ("hold" turns first,
    #     then the real instruction as the final turn). This avoids the fragile
    #     chunked path (which forces Leo to generate between fragments and can
    #     lose context mid-interruption) AND avoids silently truncating the
    #     document. See _plan_context_delivery below.
    context_sections = _plan_context_delivery(
        file_context, user_prompt, structured, skill
    )

    if len(context_sections) == 1:
        full_prompt = skill.get_full_prompt(
            user_prompt, context_sections[0], structured=structured
        )
        logger.debug(f"   Final compiled prompt: {len(full_prompt)} characters")
        _phase_start(
            "leo_flow_phase", _t0, phase="prompt_assembled", prompt_len=len(full_prompt)
        )
    else:
        logger.info(
            f"📦 Large context ({len(file_context)} chars) split into "
            f"{len(context_sections)} sections over consecutive turns."
        )

    # 3. Silent CDP injection.
    logger.debug("3. Injecting prompt via CDP (headless/background)...")

    async with BraveLeoPage() as leo_page:
        _phase_start("leo_flow_phase", _t0, phase="page_connect")
        if not await leo_page.connect():
            raise RuntimeError(
                "Failed to connect to Brave. Ensure it is running with "
                "--remote-debugging-port=9222"
            )
        _phase_start("leo_flow_phase", _t0, phase="page_connect_done")

        # Resume by navigating to the existing conversation; on failure, fall
        # back to a fresh conversation.
        if conversation_uuid:
            success = await leo_page._navigate_to_conversation(conversation_uuid)
            if success:
                leo_page.conversation_uuid = conversation_uuid
            else:
                logger.warning(
                    f"⚠️ Could not navigate to conversation "
                    f"{conversation_uuid[:8]}..., creating new instead"
                )
                conversation_uuid = None  # recompute is_first correctly

        # Snapshot the assistant-entry rowid baseline BEFORE sending so the
        # structured poller anchors strictly on the response to THIS turn. On
        # the resume (multiturn) path this must precede injection: capturing it
        # afterwards races Leo's write — if the new assistant entry commits
        # before we read the max, the anchor skips past it and the poller
        # returns the previous turn's stale text (or None). On the first-turn
        # path this is a no-op baseline (-1) and get_max_rowid_any inside
        # _send_one_message provides the authoritative user-entry anchor.
        from leo_chat.db.leo_read import get_max_entry_rowid

        expected_min_rowid = get_max_entry_rowid(conversation_uuid)
        logger.debug(
            f"   rowid anchor (pre-send) for structured path: {expected_min_rowid} "
            f"(stream path uses UUID-only polling)"
        )

        # Inject either a single turn or a sequence of consecutive turns over
        # one conversation UUID (large-file delivery). When context_sections has
        # multiple elements, sections 1..N-1 are "hold" turns (Leo acks receipt
        # without answering) and section N carries the real task. The FIRST
        # turn is is_first=True (opens the conversation + selects the model);
        # all following turns resume the same UUID.
        total_sections = len(context_sections)
        resolved_uuid = conversation_uuid or ""
        final_prompt_to_send = None

        for idx, section in enumerate(context_sections, start=1):
            turn_is_first = (idx == 1) and (conversation_uuid is None)

            # Re-anchor the response pollers immediately before the FINAL turn.
            # Hold turns create their own assistant "ack" entries; if a poller
            # (structured OR streaming) anchored anywhere earlier, it would see
            # the hold ack as the latest entry and return it instead of the real
            # answer. Capture the max entry rowid right before injecting the
            # final (answer) turn — this is the anchor passed to both the
            # structured and streaming completion pollers below.
            if idx == total_sections:
                expected_min_rowid = get_max_entry_rowid(resolved_uuid or None)
                logger.debug(
                    f"   rowid anchor (pre-final-turn): {expected_min_rowid}"
                )

            if total_sections == 1:
                turn_prompt = skill.get_full_prompt(
                    user_prompt, section, structured=structured
                )
                final_prompt_to_send = turn_prompt
            elif idx < total_sections:
                turn_prompt = skill.get_full_prompt(
                    _HOLD_INSTRUCTION_TEMPLATE.format(idx=idx, total=total_sections),
                    section,
                    structured=False,
                )
            else:
                turn_prompt = skill.get_full_prompt(
                    _FINAL_SECTION_INSTRUCTION.format(idx=idx, total=total_sections)
                    + user_prompt,
                    section,
                    structured=structured,
                )
                final_prompt_to_send = turn_prompt

            logger.debug(
                f"   → Turn {idx}/{total_sections}: injecting "
                f"{len(turn_prompt)} chars (hold={idx < total_sections and total_sections > 1})"
            )
            success = await leo_page.inject_and_send(
                final_prompt=turn_prompt,
                model_key=skill.model_key,
                is_first=turn_is_first,
            )
            if not success:
                raise RuntimeError(f"Prompt CDP injection failed (turn {idx}/{total_sections}).")

            # Resolve/keep the conversation UUID for subsequent turns.
            resolved_uuid = leo_page.conversation_uuid or resolved_uuid

            # For hold turns, wait until Leo finishes acking before the next
            # turn (a busy Leo would swallow the next injection). The final turn
            # falls through to normal response polling below.
            if total_sections > 1 and idx < total_sections:
                await leo_page._wait_for_leo_idle(chunk=section)
                logger.debug(
                    f"   ✅ Hold turn {idx}/{total_sections} acked; continue."
                )

        _phase_start("leo_flow_phase", _t0, phase="injection_done",
                     resolved_uuid=(resolved_uuid or "")[:8])

        logger.debug("   ✅ Prompt successfully injected.")

        if resolved_uuid:
            logger.debug(f"   🔗 Resolved conversation UUID: {resolved_uuid[:8]}...")
        else:
            logger.warning("   ⚠️ No conversation UUID resolved (DOM fallback active)")

        full_prompt = final_prompt_to_send or ""

        # 4. Await generation completion.
        #    - structured (patch AND planner): coherent read (immutable=1) +
        #      rowid anchor + stabilization. The schema has no deterministic
        #      end-of-generation flag, so this "coherent + stabilize" path is
        #      what protects fragile structured output (truncated JSON is
        #      unparseable; a corrupted patch is worse) from false early cuts.
        #    - prose streaming: the heuristic stream path, where a false cut is
        #      only cosmetic.
        #    - otherwise: DOM polling.
        #
        #    The timeout is DYNAMIC: it scales with the number of delivery turns
        #    and the injected content size, so a multi-turn review of a large
        #    document is not prematurely cut off by a fixed budget tuned for a
        #    single short turn.
        response_timeout = _compute_response_timeout_sec(
            total_sections,
            len(full_prompt) if total_sections == 1 else len(file_context),
        )
        logger.debug(
            f"4. Awaiting response (structured={structured}, stream={stream}, "
            f"timeout={response_timeout}s)..."
        )
        _log_json(
            "INFO", "leo_flow_response_timeout",
            timeout_sec=response_timeout, total_sections=total_sections,
        )

        if structured:
            logger.debug("   → Mode: Structured (coherent SQLite completion)")
            response_text = await _poll_completion_for_structured(
                leo_page,
                expected_min_rowid=expected_min_rowid,
                timeout=response_timeout,
                required_sentinel=_structured_sentinel(skill),
            )
            _phase_start("leo_flow_phase", _t0, phase="generation_done_structured",
                         resp_len=len(response_text or ""))
        elif stream:
            logger.debug("   → Mode: Streaming (SQLite Polling)")
            response_text = await _poll_sqlite_for_response_streaming(
                leo_page,
                timeout=response_timeout,
                poll_interval=1.5,
                after_rowid=expected_min_rowid,
            )
            _phase_start("leo_flow_phase", _t0, phase="generation_done_stream",
                         resp_len=len(response_text or ""))
        else:
            logger.debug("   → Mode: Standard (DOM Polling)")
            response_text = await leo_page.wait_for_response(
                timeout_ms=response_timeout * 1000
            )
            _phase_start("leo_flow_phase", _t0, phase="generation_done_dom",
                         resp_len=len(response_text or ""))

        # Guard: reject suspiciously short responses that are likely Leo's
        # between-chunk acknowledgements ("Got it, send the next part") rather
        # than the real answer. 80 chars rules out those stubs without blocking
        # any legitimately short answer. Re-poll once with full timeout.
        if response_text and 0 < len(response_text.strip()) < 80:
            logger.warning(
                f"⚠️ Response too short ({len(response_text.strip())} chars), "
                f"likely a chunk acknowledgement — re-polling for real answer."
            )
            if structured:
                response_text = await _poll_completion_for_structured(
                    leo_page,
                    expected_min_rowid=expected_min_rowid,
                    timeout=response_timeout,
                    required_sentinel=_structured_sentinel(skill),
                )
            elif stream:
                response_text = await _poll_sqlite_for_response_streaming(
                    leo_page,
                    timeout=response_timeout,
                    poll_interval=1.5,
                    after_rowid=expected_min_rowid,
                )
            else:
                response_text = await leo_page.wait_for_response(
                    timeout_ms=response_timeout * 1000
                )

        if not response_text or len(response_text.strip()) == 0:
            if resolved_uuid:
                # Generation has a live conversation but produced nothing within
                # budget — most likely still generating. Signal "still working"
                # (resumable) rather than a bare failure, so the caller can
                # resume the conversation instead of treating it as a timeout.
                logger.warning(
                    "Empty/truncated response with a resolved UUID — treating as "
                    "incomplete generation (still working)."
                )
                response_text = _INCOMPLETE_RESPONSE_SENTINEL
            else:
                logger.warning("Received empty or truncated response.")
                response_text = "⚠️ Failed to extract a valid response from Leo."

        # 5. Continuation strategy depends on the output shape.
        #    - Patch output: sentinel-delimited verbatim file → byte-faithful
        #      continuation until <<<END_LEO_PATCH>>>.
        #    - Structured non-patch (planner JSON): now has an authoritative
        #      terminal token (<<<LEO_DONE>>>), so truncation IS detectable and
        #      the plan can be continued safely (prose overlap-strip joins the
        #      fenced JSON block).
        #    - Prose: heuristic continuation, unchanged.
        if structured and skill.skill_id == "code_editor":
            # Edit path: LEO_EDIT envelope, byte-faithful continuation until the
            # edit's terminal sentinel. Mirrors the patch branch, but with the
            # edit's own completeness signal.
            response_text = await _auto_continue_if_truncated(
                leo_page,
                response_text,
                max_continues=3,
                is_truncated=lambda text: not edit_response_complete(text),
                continue_prompt=(
                    "Continue the edit envelope exactly where you stopped. "
                    "Repeat nothing already sent. Finish with "
                    "<<<END_LEO_EDIT>>>."
                ),
                patch_mode=True,
            )
        elif structured and getattr(skill, "supports_patch_output", False):
            response_text = await _auto_continue_if_truncated(
                leo_page,
                response_text,
                max_continues=3,
                is_truncated=lambda text: not patch_response_complete(text),
                continue_prompt=(
                    "Continue the file content exactly where you stopped. "
                    "Repeat nothing already sent. Finish with "
                    "<<<END_LEO_PATCH>>>."
                ),
                patch_mode=True,
            )
        elif structured:
            # Non-patch structured skill (planner): completion is authoritative
            # via the terminal token. The marker is the sole completeness signal:
            # if it is present ANYWHERE in the accumulated text the plan is done,
            # even if trailing prose (e.g. an earlier "already complete" reply)
            # follows it. This guards against the self-perpetuating loop where a
            # continuation's prose is stitched AFTER the marker and then wrongly
            # re-triggers another continuation.
            def _plan_incomplete(text: str) -> bool:
                if not text:
                    return True
                if TERMINAL_TOKEN in text:
                    return False
                return True

            if _plan_incomplete(response_text):
                _log_json(
                    "INFO", "leo_plan_continue",
                    resp_len=len(response_text or ""),
                    tail=(response_text or "")[-120:],
                )
            response_text = await _auto_continue_if_truncated(
                leo_page,
                response_text,
                max_continues=2,
                is_truncated=_plan_incomplete,
                continue_prompt=(
                    "Continue the JSON plan exactly where you stopped. Repeat "
                    "nothing already sent. End with <<<LEO_DONE>>> on its own "
                    "line."
                ),
                patch_mode=False,
            )
        elif not structured:
            response_text = await _auto_continue_if_truncated(
                leo_page, response_text, max_continues=2
            )

        logger.info(f"✅ Response finalized: {len(response_text)} characters.")
        _phase_start("leo_flow_phase", _t0, phase="flow_done", resp_len=len(response_text))

    return response_text, resolved_uuid


async def execute_leo_flow_with_robust_patches(
    skill_name: str,
    user_prompt: str,
    filepaths: Optional[List[str]] = None,
    model_override: Optional[str] = None,
    stream: bool = False,
    conversation_uuid: Optional[str] = None,
    structured: bool = False,
    allowed_root: Optional[str] = None,
    max_attempts: int = 2,
) -> Tuple[str, str]:
    """Execute Leo flow with validated patch output and a bounded correction retry.

    Non-patch skills (structured=False, or a skill without supports_patch_output)
    pass straight through to execute_leo_flow with no retry overhead. For
    patch-capable skills, each response is validated with PatchValidator before
    being accepted. A failure in a recoverable stage (syntactic, extraction,
    semantic — deviations Leo can plausibly fix from a correction prompt) is
    retried up to max_attempts in the same conversation. A safety-stage failure
    (path outside root, forbidden location, modify target missing) is not
    retryable and returns immediately, since no correction prompt changes where
    the target file lives.
    """
    from leo_chat.skills.skill_support import skill_supports_patch

    if not (structured and skill_supports_patch(skill_name)):
        return await execute_leo_flow(
            skill_name=skill_name,
            user_prompt=user_prompt,
            filepaths=filepaths,
            model_override=model_override,
            stream=stream,
            conversation_uuid=conversation_uuid,
            structured=structured,
        )

    last_response = ""
    last_uuid = ""
    current_uuid = conversation_uuid
    is_edit = skill_name == "code_editor"

    for attempt in range(1, max_attempts + 1):
        logger.info(
            f"Patch attempt {attempt}/{max_attempts} "
            f"skill={skill_name} uuid={current_uuid or 'new'}"
        )

        prompt_to_send = (
            user_prompt
            if attempt == 1
            else _build_correction_prompt(user_prompt, last_response, attempt)
        )

        response_text, resolved_uuid = await execute_leo_flow(
            skill_name=skill_name,
            user_prompt=prompt_to_send,
            filepaths=filepaths,
            model_override=model_override,
            stream=stream,
            conversation_uuid=current_uuid,
            structured=structured,
        )

        last_response = response_text
        last_uuid = resolved_uuid
        current_uuid = resolved_uuid

        if is_edit:
            ok, detail = _validate_edit_response(response_text, allowed_root or "")
        else:
            ok, detail = PatchValidator.validate(response_text, allowed_root or "")

        if ok:
            logger.info(
                f"Patch validated on attempt {attempt} "
                f"uuid={resolved_uuid[:8] if resolved_uuid else ''}"
            )
            return response_text, resolved_uuid

        stage = detail.get("stage")
        logger.warning(
            f"Patch validation failed attempt {attempt}/{max_attempts}: "
            f"stage={stage} error={detail.get('error')}"
        )

        if stage not in RECOVERABLE_STAGES:
            logger.error(f"Non-recoverable validation stage '{stage}'; not retrying")
            break

    logger.error(
        f"Patch validation failed after {attempt} attempt(s); "
        f"returning last response for caller-side error reporting"
    )
    return last_response, last_uuid


def _validate_edit_response(response_text: str, allowed_root: str):
    """Validate a LEO_EDIT envelope for the robust-patches retry loop.

    Reuses apply_edit_patch's dry-run (which reads the file, checks the
    exactly-one-match rule, containment, denylist, and TOCTOU) and maps the
    result into the (ok, detail) shape PatchValidator uses. Zero/multi/empty
    old_str matches are RECOVERABLE (a correction prompt can fix the snippet);
    safety/path failures are non-recoverable.
    """
    from leo_chat.patch_writer import apply_edit_patch

    result = apply_edit_patch(
        response_text, allowed_root, dry_run=True, expected_mtime_ns=None
    )

    if result.get("status") == "success":
        return True, {"patch": result}

    error = result.get("error", "edit validation failed")

    # Classify by error message: match-count, over-reach, and empty-old
    # failures are recoverable; safety/location failures are not.
    if ("matched" in error) or ("old_str is empty" in error) or (
        "change region" in error
    ) or ("no edit envelope" in error) or ("missing" in error) or ("truncated" in error):
        stage = "semantic"
    else:
        stage = "safety"

    return False, {"stage": stage, "error": error}


def _build_correction_prompt(
    original_prompt: str,
    failed_response: str,
    attempt: int,
) -> str:
    """Build a correction prompt that includes the prior failure's context."""
    preview = failed_response[:300].strip() if failed_response else "(empty)"
    return (
        f"Your previous response (attempt {attempt - 1}) did not produce a valid "
        f"FILE_PATCH envelope.\n\n"
        f"Response preview:\n{preview}\n\n"
        f"Original task:\n{original_prompt}\n\n"
        f"Requirements:\n"
        f"1. Start with <<<LEO_PATCH>>>\n"
        f"2. Include FILE, ACTION, RISK, SUMMARY headers\n"
        f"3. Separate content with <<<CONTENT>>>\n"
        f"4. End with <<<END_LEO_PATCH>>>\n"
        f"5. Content must be the complete file, no placeholders\n"
        f"Respond ONLY with the envelope."
    )


async def _auto_continue_if_truncated(
    leo_page: "BraveLeoPage",
    response_text: str,
    max_continues: int = 2,
    is_truncated: Optional[Callable[[str], bool]] = None,
    continue_prompt: str = "continue from here onwards",
    patch_mode: bool = False,
) -> str:
    """Detect a truncated response and auto-continue within the same conversation.

    is_truncated defaults to the prose heuristics in helpers._detect_truncation;
    callers with a deterministic completeness signal (e.g. the patch envelope's
    terminal sentinel) should pass their own check instead.

    Skips any continuation whose content already exists in the accumulated
    response, preventing duplicate text when Leo had already finished.

    patch_mode controls stitching fidelity:
      - True (patch/file content): merge is BYTE-FAITHFUL. The overlap is matched
        byte-for-byte and the raw continuation is appended with no rstrip/lstrip
        and no injected newline — trailing whitespace and indentation at the cut
        point are preserved exactly.
      - False (prose): the original whitespace-normalizing behavior, which is
        cosmetic-only for prose.
    """
    if is_truncated is None:
        from leo_chat.helpers import _detect_truncation
        is_truncated = _detect_truncation

    for cont in range(1, max_continues + 1):
        if not is_truncated(response_text):
            break

        logger.info(
            f"🔍 Response appears truncated (attempt {cont}/{max_continues}), "
            f"auto-continuing..."
        )

        try:
            success = await leo_page._send_one_message(
                final_prompt=continue_prompt,
                model_key=leo_page._last_model_key,
                max_attempts=1,
                is_first=False,
            )
            if not success:
                logger.warning("Auto-continue injection failed.")
                break

            continuation = await leo_page.wait_for_response(timeout_ms=120000)

            if not continuation or len(continuation.strip()) == 0:
                logger.warning("Auto-continue produced empty response.")
                break

            if patch_mode:
                # Byte-faithful stitching: never strip the continuation, match
                # the overlap exactly, append raw (no rstrip/lstrip/"\n").
                if continuation in response_text:
                    logger.warning(
                        "Continuation duplicates existing content, "
                        "response already complete, stopping auto-continue."
                    )
                    break
                overlap = _strip_overlap(response_text, continuation, exact=True)
                # Non-advancing discard: the continuation added no new bytes.
                # A re-send that reproduces the existing tail (or is empty after
                # overlap removal) must stop the loop, not spin max_continues.
                if not overlap:
                    logger.warning(
                        "Continuation is non-advancing (no new bytes), "
                        "discarding and stopping auto-continue."
                    )
                    break
                response_text = response_text + overlap
            else:
                cont_stripped = continuation.strip()

                # Continuation already present means Leo re-sent a finished response.
                if cont_stripped in response_text:
                    logger.warning(
                        "Continuation duplicates existing content, "
                        "response already complete, stopping auto-continue."
                    )
                    break

                # Remove any overlap where Leo repeated the tail before continuing.
                overlap = _strip_overlap(response_text, cont_stripped)
                # Non-advancing discard (prose): whitespace-only new content is
                # not progress; stop rather than append a blank line and loop.
                if not overlap.strip():
                    logger.warning(
                        "Continuation is non-advancing (no new prose), "
                        "discarding and stopping auto-continue."
                    )
                    break
                response_text = response_text.rstrip() + "\n" + overlap.lstrip()

            logger.debug(
                f"   ✅ Continuation received: +{len(overlap)} chars "
                f"(total: {len(response_text)})"
            )

        except Exception as e:
            logger.warning(f"Auto-continue failed: {e}")
            break

    return response_text


def _strip_overlap(existing: str, continuation: str, exact: bool = False) -> str:
    """Remove any leading part of continuation that already appears at the tail
    of existing, handling Leo repeating the last lines before continuing.

    Unified detector (fixes two latent bugs in the old prose-only version):
      - step 1 (was `range(max_window, 50, -50)`, whose real minimum is 100, so
        any overlap shorter than 100 chars was never tested → duplication).
      - exact=True: byte-for-byte tail match with no `.strip()`, required for
        patch/file content so whitespace at the cut point is not lost.
      - exact=False: preserves the original prose semantics (stripped tail
        matched against the continuation start), now with full step-1 coverage.

    Cost is O(n²) bounded by the 2000-char window (~4M comparisons worst case).
    """
    max_window = min(len(existing), len(continuation), 2000)
    for size in range(max_window, 0, -1):
        tail = existing[-size:] if exact else existing[-size:].strip()
        if tail and continuation.startswith(tail):
            return continuation[len(tail):]
    return continuation


async def _poll_completion_for_structured(
    leo_page: BraveLeoPage,
    expected_min_rowid: int = -1,
    timeout: int = 180,
    required_sentinel: Optional[str] = None,
) -> str:
    """Await a coherent, stable completion for structured output (patch + planner).

    Uses the coherent-read (`immutable=1`) + rowid-anchored + stabilization
    strategy in leo_read.wait_for_completion rather than the prose streaming
    heuristic, which can declare "done" mid-generation and truncate fragile
    structured payloads (unparseable JSON / corrupted patch).

    When ``required_sentinel`` is set, the stabilized entry is only returned once
    it actually contains the terminal marker, so a marker-less mid-commit
    snapshot never triggers a spurious downstream continuation.

    Falls back to DOM polling when no conversation UUID is available.
    """
    from leo_chat.db.leo_read import (
        wait_for_completion,
        LeoGenerationAborted,
        LeoNoRowTimeout,
    )

    uuid = leo_page.conversation_uuid
    if not uuid:
        logger.warning("⚠️ UUID unavailable, falling back to DOM polling.")
        return await leo_page.wait_for_response(timeout_ms=timeout * 1000)

    try:
        loop = asyncio.get_event_loop()
        full_response = await loop.run_in_executor(
            None,
            lambda: wait_for_completion(
                conversation_uuid=uuid,
                expected_min_rowid=expected_min_rowid,
                timeout=timeout,
                required_sentinel=required_sentinel,
            ),
        )
        logger.debug(
            f"✅ Structured completion finalized: {len(full_response)} characters."
        )
        return full_response

    except LeoGenerationAborted:
        # DB says the entry is a stabilized NULL. Trust it ONLY if the UI has
        # also released; a still-busy UI means it's a transient start-of-gen
        # window, so fall back to DOM polling instead of failing.
        if not await leo_page._is_leo_busy():
            logger.error(f"❌ Leo generation aborted (empty/NULL) for {uuid}.")
            raise
        logger.warning(
            "NULL entry_text while UI still busy; deferring to DOM polling."
        )
        return await leo_page.wait_for_response(timeout_ms=timeout * 1000)

    except LeoNoRowTimeout:
        # No DB row ever appeared (history-off / temporary chat). The DOM still
        # holds the live answer, so fail over to DOM polling rather than treating
        # this as a hard failure.
        logger.warning(
            f"⚠️ No assistant row for {uuid} within no-row window; "
            f"deferring to DOM polling."
        )
        return await leo_page.wait_for_response(timeout_ms=timeout * 1000)

    except Exception as e:
        logger.error(f"Structured completion poll failed: {e}")
        logger.debug("Falling back to DOM polling layer...")
        return await leo_page.wait_for_response(timeout_ms=timeout * 1000)


async def _poll_sqlite_for_response_streaming(
    leo_page: BraveLeoPage,
    timeout: int = 120,
    poll_interval: float = 1.5,
    after_rowid: int = -1,
) -> str:
    """Poll SQLite for a streamed response, falling back to DOM polling.

    When no conversation UUID is available, streaming cannot query by UUID, so
    it falls back to DOM polling instead of polling an empty result.

    `after_rowid` anchors the poll so it only observes the assistant entry for
    the FINAL turn. Without it, a multi-turn delivery (large file split across
    consecutive turns) would return the first "hold" acknowledgement instead of
    the real answer.

    Polls by UUID until the response stabilises or timeout is reached.
    Falls back to DOM polling when no UUID is available.
    """
    from leo_chat.db.leo_read import stream_response

    logger.debug("Initiating SQLite streaming sequence...")
    uuid = leo_page.conversation_uuid

    if not uuid:
        logger.warning("⚠️ UUID unavailable, falling back to DOM polling.")
        return await leo_page.wait_for_response(timeout_ms=timeout * 1000)

    def on_chunk(chunk: str):
        """Log each newly decrypted streamed chunk."""
        logger.debug(f"[STREAM CHUNK RECEIVED] Length: {len(chunk)}")

    try:
        loop = asyncio.get_event_loop()
        full_response = await loop.run_in_executor(
            None,
            lambda: stream_response(
                uuid=uuid,
                chunk_callback=on_chunk,
                timeout=timeout,
                poll_interval=poll_interval,
                after_rowid=after_rowid,
            ),
        )

        logger.debug(f"✅ Streaming finalized: {len(full_response)} characters.")
        return full_response

    except Exception as e:
        logger.error(f"Streaming poll failed: {e}")
        logger.debug("Falling back to DOM polling layer...")
        return await leo_page.wait_for_response(timeout_ms=timeout * 1000)