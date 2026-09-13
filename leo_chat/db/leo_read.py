#!/usr/bin/env python3
"""
Leo Read v2.1 - Decrypt Leo responses from Brave's SQLite DB

CHANGES v2.0 → v2.1:
  [FIX] Literal "\\n" → "\n" in main() error block (was printing escaped newlines)
  [FIX] Indentation of `if not row:` body (12 → 8 spaces)
  [FIX] get_key() failure now checked in main() before decrypt (exit code 4)
  [FIX] All SQLite connections wrapped in try/finally (no leaked connections)
  [FIX] Removed duplicate `import time`
  [CLARIFY] is_response_complete returns the completed TEXT (or None), not a
            bool. Name kept for backward compatibility, behavior documented.

CHANGES v1.0 → v2.0:
  [ADD] Key cache to handle key rotation gracefully
  [FIX] Silent decryption failures - now returns error code with diagnostic
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable

# --- DYNAMIC PROFILE CONFIG ---
# Allows passing the profile via env var, e.g. BRAVE_PROFILE="Profile 1"
BRAVE_PROFILE = os.environ.get("BRAVE_PROFILE", "Default")

DB = os.path.expanduser(
    f"~/Library/Application Support/BraveSoftware/Brave-Browser/{BRAVE_PROFILE}/AIChat"
)

# Key cache - loaded ONCE at startup (failures are NOT cached)
_cache: Optional[bytes] = None
_cached_at: float = 0.0


class LeoGenerationAborted(Exception):
    """Raised when the anchored assistant entry is a terminal, stabilized
    ABORTED state (entry_text is NULL and has stopped changing across polls).

    Signals the caller to fail fast instead of waiting out the full timeout.
    The caller SHOULD cross-check the live UI busy flag before acting, since a
    sub-second NULL window can occur at the very start of generation.
    """
    def __init__(self, conversation_uuid: str):
        self.conversation_uuid = conversation_uuid
        super().__init__(f"Leo generation aborted (empty/NULL) for {conversation_uuid}")


class LeoNoRowTimeout(Exception):
    """Raised when NO anchored assistant entry appears within a short window.

    Distinct from ``LeoGenerationAborted`` (which means a NULL entry EXISTS).
    Here the DB never produced an assistant row at all — most likely history is
    disabled or Leo is in a temporary chat — so the caller should fail over to
    the DOM/CDP path immediately instead of burning the full budget.
    """
    def __init__(self, conversation_uuid: str, no_row_timeout: float):
        self.conversation_uuid = conversation_uuid
        self.no_row_timeout = no_row_timeout
        super().__init__(
            f"No assistant row for {conversation_uuid} after {no_row_timeout}s"
        )


def get_key(force_reload: bool = False) -> bytes:
    """
    Get Leo decryption key from the macOS Keychain.

    Args:
        force_reload: Force a fresh read from the Keychain.

    Returns:
        Decryption key bytes (empty b"" on failure).

    Note:
        Loaded ONCE at startup by default. Failures are NOT cached (return b""
        but keep _cache=None), so a transient Keychain lock doesn't
        permanently poison the process.
    """
    global _cache, _cached_at

    if force_reload:
        _cache = None
        _cached_at = 0.0

    # Return cache ONLY if it's a valid (non-empty) key with a timestamp
    if _cache and _cached_at > 0:
        return _cache

    try:
        pw = subprocess.run(
            (
                "security",
                "find-generic-password",
                "-gs",
                "Brave Safe Storage",
                "-w",
            ),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        if not pw:
            sys.stderr.write("[ERROR] Brave Safe Storage returned empty password\n")
            sys.stderr.write("[ACTION] Ensure Brave has Keychain access\n")
            # Do NOT cache the failure as a key. Keep _cache=None.
            _cache = None
            _cached_at = 0.0
            return b""

        # Chromium/Brave Safe Storage salt is b"saltysalt"
        _cache = hashlib.pbkdf2_hmac(
            "sha1", pw.encode("utf8"), b"saltysalt", 1003, 16
        )
        _cached_at = time.time()

        return _cache

    except subprocess.CalledProcessError as e:
        # Do NOT cache the failure as a key. Keep _cache=None to retry later.
        _cache = None
        _cached_at = 0.0
        sys.stderr.write("✗ [ERROR] Could not obtain the decryption key from Keychain.\n")
        sys.stderr.write(
            f"   DETAIL: {e.stderr.strip() if e.stderr else 'Unknown error'}\n"
        )
        sys.stderr.write("   CAUSE: Brave Safe Storage did not respond or is locked.\n")
        sys.stderr.write("   FIX (run in order):\n")
        sys.stderr.write("     (1) Unlock your Mac (press any key)\n")
        sys.stderr.write("     (2) Open Brave and use Leo once\n")
        sys.stderr.write("     (3) Check permissions: System Settings → Privacy → Keychain\n")
        sys.stderr.write("     (4) Restart leo_mcp_server.py\n")
        sys.stderr.write("\n")
        sys.stderr.write("   ⚠ THIS IS A CRITICAL HEALTH CHECK - DO NOT IGNORE ⚠\n")
        return b""


def decrypt(blob, key, max_attempts: int = 3, base_delay: float = 2.0) -> str:
    """
    Decrypt an AES-CBC blob with PKCS7 padding, with retry logic for transient
    failures (e.g. key rotation).

    Args:
        blob: Encrypted bytes (may be None, a bytes-repr string, or plain text).
        key: 16-byte AES key.
        max_attempts: Maximum number of decryption attempts (default: 3).
        base_delay: Base delay in seconds for exponential backoff (default: 2.0).

    Returns:
        Decrypted string, or empty string on failure after all retries.
    """
    global _cache  # needed so cache invalidation on retry takes effect

    # Handle Python repr pattern b'v10...' — ciphertext leaked into a str.
    if isinstance(blob, str):
        import re

        match = re.search(r"b['\"](v10(?:\\x[0-9a-fA-F]{2})+)['\"]", blob)
        if match:
            hex_part = match.group(1)
            try:
                blob = hex_part.encode().decode("unicode_escape").encode("latin1")
            except Exception:
                return blob if blob else ""
        else:
            return blob if blob else ""

    if not isinstance(blob, bytes):
        return str(blob) if blob else ""

    if not blob.startswith(b"v10"):
        try:
            return blob.decode("utf8", "ignore")
        except Exception:
            return ""

    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            from Crypto.Cipher import AES

            cipher = AES.new(key, AES.MODE_CBC, b" " * 16)
            dec = cipher.decrypt(blob[3:])
            pad = dec[-1]

            if 0 < pad <= 16:
                decrypted = dec[:-pad]
            else:
                decrypted = dec

            result = decrypted.decode("utf8", "ignore")

            if result:
                return result

            # Empty result might indicate key rotation - retry after delay
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))  # 2s, 4s
                sys.stderr.write(
                    f"[WARN] Decryption attempt {attempt} produced empty result, "
                    f"retrying in {delay}s...\n"
                )
                time.sleep(delay)
                _cache = None  # invalidate cache (possible key rotation)
                key = get_key(force_reload=True)
            else:
                sys.stderr.write(
                    "[ERROR] Decryption produced empty result after all retries - "
                    "key may have rotated\n"
                )
                sys.stderr.write(
                    "Hint: Restart leo_mcp_server.py or reload the key\n"
                )
                return ""

        except ImportError:
            sys.stderr.write(
                "[ERROR] pycryptodome not installed. Run: pip3 install pycryptodome\n"
            )
            sys.exit(5)

        except Exception as e:
            last_exception = e
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))  # 2s, 4s
                sys.stderr.write(
                    f"[WARN] Decryption attempt {attempt} failed: {e}, "
                    f"retrying in {delay}s...\n"
                )
                time.sleep(delay)
                _cache = None  # invalidate cache (possible key rotation)
                key = get_key(force_reload=True)
            else:
                sys.stderr.write(
                    f"[ERROR] Decryption failed after {max_attempts} attempts: {e}\n"
                )
                sys.stderr.write(
                    "Hint: Key may have rotated - restart server or reload the key\n"
                )
                return ""

    sys.stderr.write(f"[ERROR] Unexpected exit from retry loop: {last_exception}\n")
    return ""



def _connect_ro() -> sqlite3.Connection:
    """
    Open a coherent read-only connection to Brave's AIChat DB.

    Uses `immutable=1` (not `nolock=1`): each open is a coherent snapshot, so a
    reader sees an entry fully or not at all — this removes the partial reads
    that caused the false "progressive" behavior with `nolock=1`.

    Safe here because Brave's AIChat DB runs `journal_mode=delete` (rollback
    journal), NOT WAL — there is no separate `-wal` file that `immutable=1`
    would ignore. If Brave ever switches to WAL, `immutable=1` could miss
    freshly-committed entries and this must be revisited (plain `mode=ro`).
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
    con.execute("PRAGMA query_only=true")  # extra safety (read-only)
    return con


def probe_encryption_format() -> str:
    """Report the on-disk format of Brave's AIChat `entry_text` blobs.

    Observability-only (Layer 6): `decrypt()` already branches on the `v10`
    prefix, but a NON-`v10` build was previously detected silently — the first
    symptom was an empty/plaintext read. This samples the most recent assistant
    entry and reports its format loudly at startup so a format change is caught
    before it corrupts a live flow.

    Returns:
        One of "v10" (AES-CBC encrypted, current format), "plaintext" (raw
        bytes/blob already decoded, no encryption), "unavailable" (no entry
        found / DB unreadable). Never raises.
    """
    con = None
    try:
        con = _connect_ro()
        cur = con.cursor()
        cur.execute(
            """
            SELECT entry_text FROM conversation_entry
            WHERE character_type = 1 AND entry_text IS NOT NULL
            ORDER BY rowid DESC LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return "unavailable"

        blob = row[0]
        if isinstance(blob, bytes):
            return "v10" if blob.startswith(b"v10") else "plaintext"
        # String: could be a b'v10...' repr or genuine plaintext.
        if isinstance(blob, str):
            import re
            return "v10" if re.search(r"b['\"]v10", blob) else "plaintext"
        return "plaintext"

    except Exception as exc:
        sys.stderr.write(f"[ERROR] probe_encryption_format failed: {exc}\n")
        return "unavailable"
    finally:
        if con is not None:
            con.close()


def get_max_entry_rowid(uuid: Optional[str] = None) -> int:
    """
    Return the current maximum rowid of assistant entries.

    Captured BEFORE injecting a prompt so polling can anchor by
    `rowid > expected_min_rowid` and never jump to a different (older or newer)
    conversation entry mid-poll.

    Args:
        uuid: If given, scope to that conversation. If None, take the GLOBAL max
            assistant-entry rowid — used for brand-new conversations whose UUID
            is not known until after injection; the new entry is guaranteed to
            get a rowid greater than this baseline.

    Returns:
        The max rowid, or -1 if none exist / on error (means "no baseline",
        i.e. accept any new entry).
    """
    con = None
    try:
        con = _connect_ro()
        cur = con.cursor()
        if uuid is None:
            cur.execute(
                "SELECT MAX(rowid) FROM conversation_entry WHERE character_type = 1"
            )
        else:
            cur.execute(
                """
                SELECT MAX(rowid) FROM conversation_entry
                WHERE conversation_uuid = ? AND character_type = 1
                """,
                (str(uuid),),
            )
        row = cur.fetchone()
        if not row or row[0] is None:
            return -1
        return int(row[0])
    except Exception as e:
        sys.stderr.write(f"[ERROR] get_max_entry_rowid failed: {e}\n")
        return -1
    finally:
        if con is not None:
            con.close()


def get_latest_response(uuid: str, after_rowid: int = -1) -> Optional[str]:
    """
    Get the most recent assistant response for a given conversation UUID.

    FIX: connection is now closed via try/finally (no leaked connections).

    Args:
        uuid: Conversation UUID.
        after_rowid: Only consider assistant entries with rowid > after_rowid.
            Default -1 = any. Anchoring the poll with a baseline captured just
            before the final injection prevents returning a STALE earlier turn
            (e.g. a multi-turn delivery's "hold" acknowledgement) instead of the
            freshly generated answer.

    Returns:
        Decrypted response text, or None if not found / on error.
    """
    con = None
    try:
        con = _connect_ro()
        cur = con.cursor()

        cur.execute(
            """
            SELECT entry_text FROM conversation_entry
            WHERE conversation_uuid = ? AND character_type = 1
              AND rowid > ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (str(uuid), int(after_rowid)),
        )

        row = cur.fetchone()
        if not row:
            return None

        key = get_key()
        if not key:
            sys.stderr.write("[ERROR] No decryption key available\n")
            return None

        return decrypt(row[0], key)

    except Exception as e:
        sys.stderr.write(f"[ERROR] get_latest_response failed: {e}\n")
        return None
    finally:
        if con is not None:
            con.close()


@dataclass(frozen=True)
class AssistantState:
    """
    Classified snapshot of the latest anchored assistant entry.

    ``state`` is one of the nested :class:`AssistantState.State` enum values;
    ``text`` is the decrypted content (None unless COMPLETE); ``entry_uuid``
    is the assistant entry UUID (None only when no anchored entry exists).
    """

    class State(Enum):
        MISSING = "missing"    # no assistant entry above the anchor yet
        ABORTED = "aborted"    # entry exists but empty/NULL (terminal)
        COMPLETE = "complete"  # entry exists with non-empty decrypted text
        ERROR = "error"        # read/decrypt failure (treat as retryable)

    state: State
    text: Optional[str] = None
    entry_uuid: Optional[str] = None


def latest_assistant_state(
    conversation_uuid: str,
    expected_min_rowid: int = -1,
) -> "AssistantState":
    """
    Read the latest anchored assistant entry and classify its state.

    Reads `entry_text` directly from the assistant entry — the canonical value
    Brave maintains — instead of reconstructing text from the
    `conversation_entry_event_completion` table. Empirically, `entry_text`
    equals the MAX(event_order) completion event in 177/178 multi-event entries
    (and the sole event in 1169/1169 single-event entries), whereas joining ALL
    events matches 0 entries and silently corrupts ~13% of assistant turns.
    `event_order` indexes alternative completions (draft/regeneration), NOT
    chunks, so it must never be concatenated.

    A NULL `entry_text` on an assistant entry (character_type=1) is a terminal
    "aborted/empty" state, not a mid-stream transient: in the observed DB only
    2/1349 assistant entries are NULL and neither was ever back-filled (one was
    superseded by a later sibling that completed, the other is the sole turn of
    a dead conversation). Callers that need to distinguish "aborted" from
    "still generating" must cross-reference the live UI busy flag rather than
    infer it from this function alone.

    Args:
        conversation_uuid: Conversation UUID.
        expected_min_rowid: If >= 0, only consider entries with
            `rowid > expected_min_rowid`. Anchors polling to the entry created
            by the current request so it never jumps to a different (older or
            concurrently-created) conversation entry mid-poll.

    Returns:
        An AssistantState — always populated; `text` is the decrypted content
        (possibly None for the MISSING/ABORTED states) and `entry_uuid` is None
        only when no anchored assistant entry exists at all.
    """
    con = None
    try:
        con = _connect_ro()
        cur = con.cursor()

        cur.execute(
            """
            SELECT uuid, entry_text FROM conversation_entry
            WHERE conversation_uuid = ? AND character_type = 1
              AND rowid > ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (str(conversation_uuid), int(expected_min_rowid)),
        )
        row = cur.fetchone()

        if not row:
            return AssistantState(
                state=AssistantState.State.MISSING,
                text=None,
                entry_uuid=None,
            )

        entry_uuid, entry_text_blob = row[0], row[1]

        # NULL entry_text = aborted/empty generation (terminal, see docstring).
        if entry_text_blob is None:
            return AssistantState(
                state=AssistantState.State.ABORTED,
                text=None,
                entry_uuid=entry_uuid,
            )

        key = get_key()
        if not key:
            sys.stderr.write("[ERROR] No decryption key available\n")
            return AssistantState(
                state=AssistantState.State.ERROR,
                text=None,
                entry_uuid=entry_uuid,
            )

        full = decrypt(entry_text_blob, key)
        if not full or not full.strip():
            return AssistantState(
                state=AssistantState.State.ABORTED,
                text=None,
                entry_uuid=entry_uuid,
            )

        return AssistantState(
            state=AssistantState.State.COMPLETE,
            text=full,
            entry_uuid=entry_uuid,
        )

    except Exception as e:
        sys.stderr.write(f"[ERROR] latest_assistant_state failed: {e}\n")
        return AssistantState(
            state=AssistantState.State.ERROR,
            text=None,
            entry_uuid=None,
        )
    finally:
        if con is not None:
            con.close()


def is_response_complete(
    conversation_uuid: str,
    expected_min_rowid: int = -1,
) -> Optional[str]:
    """
    Backward-compatible wrapper around :func:`latest_assistant_state`.

    Returns the completed assistant TEXT (str) or None — not a bool. None
    covers "no entry yet", "aborted/empty", and "error" alike; callers that
    need to distinguish those should use :func:`latest_assistant_state`
    directly. Kept under its historical name so existing callers
    (``wait_for_completion``, ``_poll_completion_for_structured``, tests)
    behave identically.
    """
    state = latest_assistant_state(conversation_uuid, expected_min_rowid)
    return state.text


def _contains_sentinel(text: Optional[str], sentinel: str) -> bool:
    """Return True when ``text`` contains the terminal ``sentinel``.

    Structured outputs (patch / edit / plan) terminate with an authoritative
    marker. Presence anywhere in the body is the signal that the entry is
    genuinely complete (Brave may split the JSON body and the marker across its
    own commit sequence), so this deliberately does NOT require the marker to be
    the final few characters — downstream token-detection handles end-of-text
    strictness where needed.
    """
    if not text or not sentinel:
        return False
    return sentinel in text


def wait_for_completion(
    conversation_uuid: str,
    expected_min_rowid: int = -1,
    timeout: int = 180,
    poll_interval: float = 1.0,
    stable_needed: int = 2,
    no_row_timeout: float = 8.0,
    required_sentinel: Optional[str] = None,
) -> str:
    """
    Wait for a coherent, stable completion of the assistant response.

    The schema has NO deterministic end-of-generation flag, so this is
    "coherent read + minimal stabilization", not a pure deterministic signal:

      1. Read coherently (`immutable=1` via `_connect_ro`) — no partial reads.
      2. Anchor by `rowid > expected_min_rowid` — never jump to a different
         conversation entry (e.g. an older entry or a concurrently-created one).
      3. Require the completion text to be identical across `stable_needed`
         consecutive polls — a cheap guard against the rare long/multi-chunk
         response that Brave commits across several transactions and could be
         seen partially between commits.
      4. If `required_sentinel` is given (structured output), ONLY declare the
         entry complete once the text actually contains that sentinel. Brave
         commits `entry_text` in stages (JSON body first, terminal marker last),
         so a stabilized-but-marker-less snapshot must NOT be returned as final:
         doing so makes downstream terminal-token detection spuriously fire a
         "continue" even though the plan was fully delivered.
      5. Fail fast when NO anchored entry appears within `no_row_timeout`
         seconds (history-off / temporary chat) — raise LeoNoRowTimeout so the
         caller fails over to DOM polling instead of burning the full budget.

    Args:
        conversation_uuid: Conversation UUID.
        expected_min_rowid: Max assistant-entry rowid captured BEFORE injecting
            the prompt (see get_max_entry_rowid). -1 accepts any entry.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        stable_needed: Consecutive identical polls required to declare "stable".
        no_row_timeout: Seconds with NO anchored entry before raising
            LeoNoRowTimeout (fast-fail). Only tracks the MISSING state; an
            ABORTED entry (NULL) is handled by LeoGenerationAborted instead.
        required_sentinel: Optional terminal marker (e.g. "<<<LEO_DONE>>>") that
            the stabilized text MUST contain before it is returned as complete.
            When set, a stabilized snapshot missing it continues polling.

    Returns:
        The completed response text, or whatever the latest anchored entry
        holds on timeout (may be empty string).

    Raises:
        LeoGenerationAborted: stabilized NULL entry.
        LeoNoRowTimeout: no entry ever appeared within no_row_timeout.
    """
    start = time.time()
    last_text: Optional[str] = None
    stable_count = 0
    aborted_count = 0
    missing_since: Optional[float] = None

    while time.time() - start < timeout:
        snapshot = latest_assistant_state(conversation_uuid, expected_min_rowid)

        if snapshot.state is AssistantState.State.COMPLETE:
            aborted_count = 0
            missing_since = None
            result = snapshot.text
            # Sentinel gate: a structured response must contain its terminal
            # marker before it is treated as final. If present but the marker is
            # missing, treat it as "still stabilizing" (reset the streak) so we
            # keep polling until Brave commits the marker tail.
            if required_sentinel and not _contains_sentinel(result, required_sentinel):
                stable_count = 0
                last_text = result
                time.sleep(poll_interval)
                continue
            if result == last_text:
                stable_count += 1
                if stable_count >= stable_needed:
                    return result
            else:
                last_text = result
                stable_count = 1
                if stable_needed <= 1:
                    return result

        elif snapshot.state is AssistantState.State.ABORTED:
            # Terminal NULL. Require it to persist across stable_needed polls so a
            # sub-second NULL window at the start of generation can't false-fire.
            # (Caller still cross-checks the live UI busy flag before acting.)
            aborted_count += 1
            stable_count = 0
            missing_since = None
            if aborted_count >= stable_needed:
                raise LeoGenerationAborted(conversation_uuid)

        else:
            # MISSING / ERROR -> keep polling; reset abort streak. Track how
            # long we've seen no entry so a history-off / temporary chat fails
            # over quickly instead of burning the full budget.
            aborted_count = 0
            if missing_since is None:
                missing_since = time.time()
            elif time.time() - missing_since >= no_row_timeout:
                raise LeoNoRowTimeout(conversation_uuid, no_row_timeout)

        time.sleep(poll_interval)

    # Timeout fallback: prefer the last stable completion text we saw, else the
    # current anchored entry text.
    if last_text:
        return last_text
    return get_latest_response(conversation_uuid) or ""


def stream_response(
    uuid: str,
    callback: Optional[Callable[[str], None]] = None,
    timeout: int = 120,
    poll_interval: float = 1.5,
    chunk_callback: Optional[Callable[[str], None]] = None,  # compat alias
    after_rowid: int = -1,
    required_sentinel: Optional[str] = None,
) -> str:
    """
    Stream a response by polling SQLite — emits chunks in near real time.
    Accepts either 'callback' or 'chunk_callback' (alias for compatibility).

    NOTE: This polls by UUID, so a valid conversation UUID is required.
    Passing a bogus UUID would only poll an empty result until timeout —
    callers should fall back to DOM polling when no UUID is available.

    Args:
        uuid: Conversation UUID.
        callback: Function called with each new text chunk.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        chunk_callback: Alias for callback (compatibility).
        after_rowid: Only consider assistant entries with rowid > after_rowid.
            Anchoring with a baseline captured just before the final injection
            prevents returning a STALE earlier turn (a multi-turn delivery's
            "hold" acknowledgement) instead of the freshly generated answer.
            Default -1 = any entry.
        required_sentinel: Optional terminal marker (e.g. "<<<LEO_DONE>>>") that
            the stabilized text MUST contain before it is returned as complete.
            When set, a stabilized snapshot missing it continues polling — this
            mirrors ``wait_for_completion`` for structured skills and prevents a
            premature "complete" that would spuriously trigger auto-continue.

    Returns:
        The full response text once generation stabilizes AND contains the
        required sentinel (if provided), or on timeout.
    """
    cb = callback or chunk_callback
    if cb is None:
        cb = lambda _chunk: None  # no-op if no callback provided

    start_time = time.time()
    last_text = ""
    stable_count = 0
    stable_needed = 3

    while time.time() - start_time < timeout:
        current = get_latest_response(uuid, after_rowid=after_rowid) or ""

        # Emit only the newly appended text
        if len(current) > len(last_text):
            chunk = current[len(last_text):]
            cb(chunk)
            last_text = current
            stable_count = 0
        else:
            stable_count += 1

        # Considered done after N stable polls (and non-empty text), AND — when
        # a sentinel is required — only once the sentinel is actually present.
        if stable_count >= stable_needed and current:
            if required_sentinel and not _contains_sentinel(current, required_sentinel):
                # Stabilized but marker missing: Brave may commit the body before
                # the terminal marker tail. Reset the streak and keep polling.
                stable_count = 0
                time.sleep(poll_interval)
                continue
            return current

        time.sleep(poll_interval)

    return last_text

def get_latest_conversation_uuid(after_rowid: int = -1) -> Optional[str]:
    """
    Return the conversation_uuid of the most recent assistant entry.

    If after_rowid is provided (the baseline captured BEFORE sending), only
    entries with a greater rowid are considered — so this never returns the
    UUID of an older conversation if the new one hasn't been written yet.

    Args:
        after_rowid: Only entries with rowid > after_rowid. -1 = any.

    Returns:
        The conversation_uuid (str), or None if none found / on error.
    """
    con = None
    try:
        con = _connect_ro()
        cur = con.cursor()
        cur.execute(
            """
            SELECT conversation_uuid FROM conversation_entry
            WHERE character_type = 1 AND rowid > ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (int(after_rowid),),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        sys.stderr.write(f"[ERROR] get_latest_conversation_uuid failed: {e}\n")
        return None
    finally:
        if con is not None:
            con.close()


def get_max_rowid_any() -> int:
    """
    Return the global max rowid across ALL conversation_entry types.

    Use this as the baseline snapshot BEFORE injecting a prompt so that
    both user (character_type=0) and assistant (character_type=1) entries
    created by this request are guaranteed to have a rowid greater than
    the returned value.

    Returns:
        Max rowid (int), or -1 if the table is empty / on error.
    """
    con = None
    try:
        con = _connect_ro()
        cur = con.cursor()
        cur.execute("SELECT MAX(rowid) FROM conversation_entry")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else -1
    except Exception as e:
        sys.stderr.write(f"[ERROR] get_max_rowid_any failed: {e}\n")
        return -1
    finally:
        if con is not None:
            con.close()


def get_uuid_by_user_entry(after_rowid: int = -1) -> Optional[str]:
    """
    Return the conversation_uuid of the most recent USER entry after the baseline.

    The user entry (character_type=0) is written to the DB immediately on send,
    well before the assistant response — so the UUID is resolvable almost
    instantly after clicking submit, without waiting for generation to complete.

    Anchoring by after_rowid ensures parallel instances never steal each
    other's UUID: each instance captures its own baseline just before sending,
    so its user entry is always the first one above that baseline.

    Args:
        after_rowid: Only entries with rowid > after_rowid. -1 = any.

    Returns:
        The conversation_uuid (str), or None if not found / on error.
    """
    con = None
    try:
        con = _connect_ro()
        cur = con.cursor()
        cur.execute(
            """
            SELECT conversation_uuid FROM conversation_entry
            WHERE character_type = 0
              AND rowid > ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (int(after_rowid),),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        sys.stderr.write(f"[ERROR] get_uuid_by_user_entry failed: {e}\n")
        return None
    finally:
        if con is not None:
            con.close()

def main():
    args = list(sys.argv)
    conv_id = None

    # 1. Get conversation UUID (from arg or saved URL file)
    if len(args) > 1:
        conv_id = args.pop()
    else:
        try:
            url = (
                open(os.path.expanduser("~/.hermes/leo_conversation_url.txt"))
                .read()
                .strip()
            )
            parts = url.rstrip("%").split("/")
            conv_id = parts.pop()
        except Exception as e:
            sys.stderr.write(f"[ERROR] Cannot read UUID from file or args: {e}\n")
            sys.exit(3)

    if not conv_id:
        sys.stderr.write("[ERROR] No conversation UUID provided\n")
        sys.exit(3)

    # 2. Connect to DB (read-only, coherent snapshot via immutable=1)
    con = None
    row = None
    try:
        con = _connect_ro()
        cur = con.cursor()

        # 3. Query ONLY the latest assistant response (character_type=1)
        cur.execute(
            """
            SELECT entry_text, character_type
            FROM conversation_entry
            WHERE conversation_uuid = ? AND character_type = 1
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (str(conv_id),),
        )
        row = cur.fetchone()

    except sqlite3.OperationalError as e:
        sys.stderr.write("❌ [ERROR] Could not connect to Brave's DB.\n")
        sys.stderr.write(f"   DETAIL: {e}\n")
        sys.stderr.write("   CAUSE: The DB is locked by Brave or another process.\n")
        sys.stderr.write("   FIX:\n")
        sys.stderr.write("     (1) Quit Brave completely (Cmd+Q)\n")
        sys.stderr.write("     (2) Wait 5s for it to release the DB\n")
        sys.stderr.write("     (3) Retry the script\n")
        sys.exit(1)
    finally:
        if con is not None:
            con.close()

    # 4. Verify the response exists
    # FIX: proper 8-space indentation + real "\n" (was "\\n" printing literally)
    if not row:
        sys.stderr.write(
            f"❌ [ERROR] No Leo response found for UUID: {conv_id}\n"
        )
        sys.stderr.write("   POSSIBLE CAUSES:\n")
        sys.stderr.write(
            "     (1) The response isn't generated yet (Brave/Leo still processing)\n"
        )
        sys.stderr.write("     (2) The UUID is incorrect or expired\n")
        sys.stderr.write("     (3) Brave closed before saving to the DB\n")
        sys.stderr.write(
            "   FIX: Wait 10s and retry, or verify the UUID in brave://leo-ai/\n"
        )
        sys.exit(3)

    last_text, last_type = row

    # 5. Validate that this is an assistant response
    if last_type != 1:
        sys.stderr.write(
            "[ERROR] Last entry is not an assistant response (character_type != 1)\n"
        )
        sys.exit(2)

    # 6. Decrypt and output
    # FIX: check key before decrypting (get_key may return b"")
    key = get_key()
    if not key:
        sys.stderr.write("[ERROR] No decryption key available\n")
        sys.exit(4)

    decrypted = decrypt(last_text, key)

    if not decrypted:
        sys.stderr.write("[ERROR] Decrypted response is empty or corrupted\n")
        sys.exit(1)

    print(decrypted)


if __name__ == "__main__":
    main()