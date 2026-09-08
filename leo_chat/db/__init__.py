"""leo_chat.db — read-only access to Brave's AIChat SQLite store.

Exposes the classified-state reader (AssistantState / latest_assistant_state)
alongside the historical is_response_complete wrapper and the polling helpers.
"""

from leo_chat.db.leo_read import (
    # Config / crypto
    DB,
    get_key,
    decrypt,
    # Classified state (Layer 0 — canonical entry_text reader)
    AssistantState,
    latest_assistant_state,
    is_response_complete,       # thin wrapper: returns str | None
    # Completion / streaming
    wait_for_completion,
    stream_response,
    get_latest_response,
    # Rowid + UUID anchors
    get_max_entry_rowid,
    get_max_rowid_any,
    get_latest_conversation_uuid,
    get_uuid_by_user_entry,
)

__all__ = [
    "DB",
    "get_key",
    "decrypt",
    "AssistantState",
    "latest_assistant_state",
    "is_response_complete",
    "wait_for_completion",
    "stream_response",
    "get_latest_response",
    "get_max_entry_rowid",
    "get_max_rowid_any",
    "get_latest_conversation_uuid",
    "get_uuid_by_user_entry",
]