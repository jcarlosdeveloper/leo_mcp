"""Staged validation of Leo FILE_PATCH envelopes before they reach disk."""

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from leo_chat.patch_writer import (
    PATCH_BEGIN,
    CONTENT_MARK,
    PATCH_END,
    TruncatedPatchError,
    _extract_patch_object,
    _resolve_first_existing_ancestor,
    _is_within_root,
    _is_forbidden,
)

RECOVERABLE_STAGES = frozenset({"syntactic", "extraction", "semantic"})

REQUIRED_HEADERS = frozenset({"file", "action", "risk", "summary"})
VALID_ACTIONS = frozenset({"modify", "create"})

SHRINK_MIN_BYTES = 1000
SHRINK_RATIO = 0.20

_STUB_PATTERNS = re.compile(
    r"\[\s*implement[^\]]*\]"
    r"|your code here"
    r"|rest of (the )?file unchanged"
    r"|\.\.\.\s*\((unchanged|rest|omitted)[^)]*\)"
    r"|<\s*unchanged\s*>",
    re.IGNORECASE,
)


class PatchValidationError(ValueError):
    """Signals a validation failure internal to a validator stage."""


def _raw_headers(payload: str) -> Dict[str, str]:
    """Parse the header block into a raw dict, without default-filling keys."""
    begin = payload.find(PATCH_BEGIN)
    content_at = payload.find(CONTENT_MARK, begin + len(PATCH_BEGIN))
    header_block = payload[begin + len(PATCH_BEGIN):content_at]
    header: Dict[str, str] = {}
    for line in header_block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        header[key.strip().lower()] = value.strip()
    return header


class PatchValidator:
    """Four-stage validator for Leo FILE_PATCH envelopes."""

    @classmethod
    def validate(
        cls, payload: str, allowed_root: str, precreated: bool = False
    ) -> Tuple[bool, Dict[str, Any]]:
        """Run all validation stages in order; return (ok, detail_dict).

        precreated: True when the caller pre-created the target as a stub
        before the Leo round-trip (new-file flow). Lets _check_safety accept
        either ACTION on that target instead of only the one the caller
        anticipated.
        """
        ok, detail = cls._check_structure(payload)
        if not ok:
            return False, {"stage": "syntactic", **detail}

        ok, detail = cls._check_extraction(payload)
        if not ok:
            return False, {"stage": "extraction", **detail}

        patch = detail["patch"]

        ok, detail = cls._check_safety(patch, allowed_root, precreated=precreated)
        if not ok:
            return False, {"stage": "safety", **detail}

        ok, detail = cls._check_semantics(patch, allowed_root)
        if not ok:
            return False, {"stage": "semantic", **detail}

        return True, {"patch": patch}

    @classmethod
    def _check_structure(cls, payload: str) -> Tuple[bool, Dict[str, Any]]:
        """Verify all three sentinels exist in the correct order."""
        begin = payload.find(PATCH_BEGIN)
        if begin == -1:
            return False, {"error": "no patch envelope found in payload"}

        content_at = payload.find(CONTENT_MARK, begin + len(PATCH_BEGIN))
        if content_at == -1:
            return False, {
                "error": "response truncated: patch envelope missing content marker"
            }

        end_at = payload.find(PATCH_END, content_at + len(CONTENT_MARK))
        if end_at == -1:
            return False, {
                "error": "response truncated: patch envelope not terminated"
            }

        return True, {}

    @classmethod
    def _check_extraction(cls, payload: str) -> Tuple[bool, Dict[str, Any]]:
        """Verify required headers are present and parseable."""
        try:
            patch = _extract_patch_object(payload)
        except (TruncatedPatchError, ValueError) as exc:
            return False, {"error": str(exc)}

        raw = _raw_headers(payload)
        missing = REQUIRED_HEADERS - set(raw.keys())
        if missing:
            return False, {"error": f"missing headers: {sorted(missing)}"}

        if patch["action"] not in VALID_ACTIONS:
            return False, {"error": f"invalid action: {patch['action']}"}

        return True, {"patch": patch}

    @classmethod
    def _check_semantics(
        cls, patch: Dict[str, Any], allowed_root: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """Verify content is non-empty, not a stub, and not a truncated shrink."""
        content = patch.get("content", "")
        if not content.strip():
            return False, {"error": "content section is empty"}

        if _STUB_PATTERNS.search(content):
            return False, {"error": "content contains a placeholder/stub marker"}

        if patch.get("action") == "modify" and allowed_root:
            target = _resolve_first_existing_ancestor(Path(patch["file"]))
            if target.exists():
                try:
                    current_size = target.stat().st_size
                except OSError:
                    current_size = 0
                new_size = len(content.encode("utf-8"))
                if current_size > SHRINK_MIN_BYTES and new_size < current_size * SHRINK_RATIO:
                    pct = round(100 * new_size / current_size)
                    return False, {
                        "error": f"content is {pct}% of the existing file; likely truncated"
                    }

        return True, {}

    @classmethod
    def _check_safety(
        cls, patch: Dict[str, Any], allowed_root: str, precreated: bool = False
    ) -> Tuple[bool, Dict[str, Any]]:
        """Verify target path is within root, not forbidden, and action-consistent."""
        if not allowed_root:
            return False, {"error": "no write root configured"}

        root = Path(allowed_root).expanduser().resolve(strict=False)
        if not root.is_dir():
            return False, {"error": f"allowed root is not a directory: {root}"}

        target = _resolve_first_existing_ancestor(Path(patch["file"]))

        if not _is_within_root(root, target):
            return False, {"error": f"{target} is outside allowed root {root}"}

        if _is_forbidden(target):
            return False, {"error": f"{target} is in a forbidden location"}

        action = patch["action"]
        if action == "modify" and not target.exists():
            return False, {"error": f"modify target does not exist: {target}"}
        if action == "create" and target.exists() and not precreated:
            return False, {"error": f"create target already exists: {target}"}

        return True, {}
