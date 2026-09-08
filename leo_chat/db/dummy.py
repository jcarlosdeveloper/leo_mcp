#!/usr/bin/env python3
"""
Minimal smoke test for leo_read.py (DB / decryption layer).

Does NOT touch brave_leo_page or execution. Validates only:
  1. The DB file exists on disk.
  2. The Keychain returns a key.
  3. get_max_entry_rowid() resolves without error (baseline check).
  4. A conversation UUID can be resolved.
  5. The latest assistant response can be decrypted.

Usage:
  BRAVE_PROFILE="Default" python3 dummy.py
  BRAVE_PROFILE="Default" python3 dummy.py <optional-uuid>
"""

import os
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root resolution — works regardless of where the file is invoked.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from leo_chat.db.leo_read import (
    DB,
    get_key,
    get_latest_response,
    get_max_entry_rowid,
)

# ---------------------------------------------------------------------------
# Exit-code constants
# ---------------------------------------------------------------------------
_CHECK_DB_EXISTS: int = 1
_CHECK_KEYCHAIN: int = 2
_CHECK_BASELINE: int = 3
_CHECK_UUID: int = 4
_CHECK_DECRYPT: int = 5


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = 400) -> str:
    """Return *text* truncated to *limit* characters, appending '...' if cut."""
    return text[:limit] + ("..." if len(text) > limit else "")


def _pass(step: int, message: str) -> None:
    """Print a formatted pass line for *step* with *message*."""
    print(f"✅ [{step}] {message}")


def _fail(step: int, message: str) -> None:
    """Print a formatted fail line for *step* with *message*."""
    print(f"❌ [{step}] {message}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_latest_assistant_uuid() -> str | None:
    """Return the conversation UUID of the most recent assistant entry in the DB.

    Opens the DB in read-only / immutable mode to avoid WAL interference.
    Returns None if no assistant entries exist yet.
    """
    con = None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
        cur = con.cursor()
        cur.execute(
            """
            SELECT conversation_uuid
              FROM conversation_entry
             WHERE character_type = 1
             ORDER BY rowid DESC
             LIMIT 1
            """
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        if con is not None:
            con.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the DB/decryption smoke test and return a POSIX exit code.

    Each numbered step maps directly to one of the _CHECK_* exit-code
    constants so the caller can identify exactly which check failed.
    Returns 0 on full success.
    """
    print("=" * 60)
    print(f"BRAVE_PROFILE = {os.environ.get('BRAVE_PROFILE', 'Default')}")
    print(f"DB path       = {DB}")
    print("=" * 60)

    # 1. DB exists
    if not os.path.exists(DB):
        _fail(_CHECK_DB_EXISTS, "DB not found. Is the profile name correct?")
        return _CHECK_DB_EXISTS
    _pass(_CHECK_DB_EXISTS, "DB found")

    # 2. Keychain / encryption key
    key = get_key()
    if not key:
        _fail(_CHECK_KEYCHAIN, "get_key() returned empty (Keychain locked or permission denied)")
        return _CHECK_KEYCHAIN
    _pass(_CHECK_KEYCHAIN, f"Key obtained ({len(key)} bytes)")

    # 3. Baseline rowid — confirms the table and query work
    baseline = get_max_entry_rowid(None)
    _pass(_CHECK_BASELINE, f"get_max_entry_rowid(None) = {baseline}")

    # 4. UUID: from CLI argument or the most recent entry in the DB
    uuid: str | None = sys.argv[1] if len(sys.argv) > 1 else _get_latest_assistant_uuid()
    if not uuid:
        _fail(_CHECK_UUID, "No conversations found in DB (send a message in Leo first)")
        return _CHECK_UUID
    _pass(_CHECK_UUID, f"UUID = {uuid[:8]}...")

    # 5. Decrypt the latest response
    text = get_latest_response(uuid)
    if not text or not text.strip():
        _fail(_CHECK_DECRYPT, "Empty response after decryption (possible key rotation or incomplete entry)")
        return _CHECK_DECRYPT

    _pass(_CHECK_DECRYPT, f"Response decrypted ({len(text)} chars)")
    print("-" * 60)
    print(_truncate(text))
    print("-" * 60)
    print("\n🎉 DB LAYER OK — decryption works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())