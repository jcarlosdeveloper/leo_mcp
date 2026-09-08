"""Single-file patch application for the Leo MCP write-to-disk feature.

Parses Leo's LEO_PATCH envelope (a sentinel-delimited plain-text block whose
content is the verbatim file, never JSON-escaped) and writes the file
atomically, enforcing a symlink-safe path allow-list. Completeness is detected
by the terminal sentinel, so a truncated response is reported clearly instead of
failing as unparseable. Only a compact summary is returned so the calling model
never re-ingests file contents.
"""

import os
import tempfile
import re 
import difflib
from pathlib import Path
from typing import Dict, Any, Optional


# Envelope sentinels. Chosen to be extremely unlikely to occur in source code,
# so surrounding prose and the file content itself never collide with them.
PATCH_BEGIN = "<<<LEO_PATCH>>>"
CONTENT_MARK = "<<<CONTENT>>>"
PATCH_END = "<<<END_LEO_PATCH>>>"

# LEO_EDIT envelope sentinels: targeted str_replace edits instead of whole-file
# replacement. Exactly one OLD_STR/NEW_STR pair per envelope (one edit per
# response). Distinct from the patch sentinels so the two output modes never
# collide in parser detection.
EDIT_BEGIN = "<<<LEO_EDIT>>>"
EDIT_OLD_MARK = "<<<OLD_STR>>>"
EDIT_NEW_MARK = "<<<NEW_STR>>>"
EDIT_END = "<<<END_LEO_EDIT>>>"

# Terminal token for NON-patch structured output (e.g. the planner's JSON plan).
# Distinct from the patch sentinels so overloading never couples truncation-
# detection semantics to patch-writing semantics. Skills that produce structured
# (non-patch) output MUST terminate their response with this token so completion
# is authoritative rather than heuristic.
TERMINAL_TOKEN = "<<<LEO_DONE>>>"


class TruncatedPatchError(ValueError):
    """Raised when the envelope starts but its terminal sentinel is missing.

    Distinguishes a cut-off (streaming-truncated) response, which can be
    recovered by continuation, from a payload that never contained an envelope.
    """


def patch_response_complete(text: str) -> bool:
    """Return True when text holds a fully terminated patch envelope.

    Completeness is defined solely by the terminal sentinel appearing after the
    opening one. Used both to gate auto-continuation upstream and to validate
    before parsing here.
    """
    begin = text.find(PATCH_BEGIN)
    if begin == -1:
        return False
    return text.find(PATCH_END, begin + len(PATCH_BEGIN)) != -1


def response_has_terminal_token(text: str, token: str = TERMINAL_TOKEN) -> bool:
    """Return True when ``text`` ends (or contains, after any trailing fence)
    with ``token``.

    Generalizes ``patch_response_complete``'s sentinel idea to any structured
    output: the presence of the terminal token is the authoritative "complete"
    signal, replacing heuristic truncation detection. ``text`` may be empty.
    """
    if text is None:
        return False
    stripped = text.rstrip()
    if not stripped:
        return False
    # The token must appear at (or very near) the very end of the response. A
    # trailing markdown fence line is tolerated; anything else means the token
    # is not a genuine terminator (it could appear mid-document by coincidence).
    return stripped.endswith(token)


def strip_terminal_token(text: str, token: str = TERMINAL_TOKEN) -> str:
    """Return ``text`` with a trailing ``token`` removed (idempotent for prose).

    Used only where the caller wants the raw payload WITHOUT the completeness
    marker leaking into machine-parsed output. Safe no-op when the token is
    absent.
    """
    if text is None:
        return ""
    if response_has_terminal_token(text, token):
        return text.rstrip()[: -len(token)].rstrip()
    return text


def edit_response_complete(text: str) -> bool:
    """Return True when ``text`` holds a fully terminated LEO_EDIT envelope.

    Mirrors ``patch_response_complete``: completeness is defined solely by the
    terminal sentinel appearing after the opening one. Used to gate
    auto-continuation for the edit path.
    """
    if text is None:
        return False
    begin = text.find(EDIT_BEGIN)
    if begin == -1:
        return False
    return text.find(EDIT_END, begin + len(EDIT_BEGIN)) != -1


def _extract_edit_object(payload: str) -> Dict[str, Any]:
    """Extract the single LEO_EDIT envelope from Leo's payload.

    Tolerates surrounding prose by locating sentinels. Raises
    TruncatedPatchError when the envelope opens but never terminates, and
    ValueError when no envelope, no file path, or a missing OLD_STR/NEW_STR
    block is present.
    """
    begin = payload.find(EDIT_BEGIN)
    if begin == -1:
        raise ValueError("no edit envelope found in payload")

    old_at = payload.find(EDIT_OLD_MARK, begin + len(EDIT_BEGIN))
    if old_at == -1:
        raise TruncatedPatchError(
            "response truncated: edit envelope missing old_str marker"
        )

    new_at = payload.find(EDIT_NEW_MARK, old_at + len(EDIT_OLD_MARK))
    if new_at == -1:
        raise TruncatedPatchError(
            "response truncated: edit envelope missing new_str marker"
        )

    end_at = payload.find(EDIT_END, new_at + len(EDIT_NEW_MARK))
    if end_at == -1:
        raise TruncatedPatchError(
            "response truncated: edit envelope not terminated"
        )

    # Header lines live between the opening sentinel and the old_str marker.
    header_block = payload[begin + len(EDIT_BEGIN):old_at]
    header: Dict[str, str] = {}
    for line in header_block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        header[key.strip().lower()] = value.strip()

    if "file" not in header or not header["file"]:
        raise ValueError("edit envelope missing required FILE header")

    old_str = _strip_one_newline_edges(
        payload[old_at + len(EDIT_OLD_MARK):new_at]
    )
    new_str = _strip_one_newline_edges(
        payload[new_at + len(EDIT_NEW_MARK):end_at]
    )

    return {
        "file": header["file"],
        "action": header.get("action", "edit"),
        "risk": header.get("risk", "?"),
        "summary": header.get("summary", ""),
        "old_str": old_str,
        "new_str": new_str,
    }


def _strip_one_newline_edges(text: str) -> str:
    """Remove exactly one leading and one trailing newline, nothing else.

    Preserves the file's own indentation and any intentional blank lines; only
    the newlines that separate the content from the surrounding sentinels are
    dropped.
    """
    if text.startswith("\r\n"):
        text = text[2:]
    elif text.startswith("\n"):
        text = text[1:]
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return text


def _extract_patch_object(payload: str) -> Dict[str, Any]:
    """Extract the single FILE_PATCH envelope from Leo's payload.

    Tolerates surrounding prose by locating the sentinels rather than requiring
    the payload to be exactly the envelope. Raises TruncatedPatchError when the
    envelope opens but never terminates, and ValueError when no envelope or no
    file path is present.
    """
    begin = payload.find(PATCH_BEGIN)
    if begin == -1:
        raise ValueError("no patch envelope found in payload")

    content_at = payload.find(CONTENT_MARK, begin + len(PATCH_BEGIN))
    if content_at == -1:
        raise TruncatedPatchError(
            "response truncated: patch envelope missing content marker"
        )

    end_at = payload.find(PATCH_END, content_at + len(CONTENT_MARK))
    if end_at == -1:
        raise TruncatedPatchError(
            "response truncated: patch envelope not terminated"
        )

    # Header lines live between the opening sentinel and the content marker.
    header_block = payload[begin + len(PATCH_BEGIN):content_at]
    header: Dict[str, str] = {}
    for line in header_block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        header[key.strip().lower()] = value.strip()

    if "file" not in header or not header["file"]:
        raise ValueError("patch envelope missing required FILE header")

    # Extract raw content between sentinels
    raw_content = _strip_one_newline_edges(
        payload[content_at + len(CONTENT_MARK):end_at]
    )

    # STRIP THE INNER FENCE: Remove the ```<lang> ... ``` wrapper
    # The model wraps the file content in an inner triple-backtick block.
    # We need to extract just the file content, not the fence markers.
    import re
    fence_pattern = re.compile(r"^```[a-z]+\s*\n(.*?)\n```$", re.DOTALL)
    match = fence_pattern.match(raw_content)
    
    if match:
        # Successfully stripped the fence, use the captured group
        content = match.group(1)
    else:
        # Fallback: if no fence found, use as-is (might be unexpected)
        content = raw_content

    patch: Dict[str, Any] = {
        "file": header["file"],
        "action": header.get("action", "modify"),
        "content": content,
        "risk": header.get("risk", "?"),
        "summary": header.get("summary", ""),
    }
    return patch


def _resolve_first_existing_ancestor(path: Path) -> Path:
    """Resolve the nearest existing ancestor of path with symlinks followed.

    For create actions the target does not exist yet, so resolve(strict=True)
    would fail. Walks up to the first existing component, canonicalizes it
    (following symlinks), then re-appends the non-existent tail.
    """
    path = path.expanduser()
    tail_parts = []
    current = path

    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        tail_parts.append(current.name)
        current = parent

    resolved_base = current.resolve(strict=False)
    for name in reversed(tail_parts):
        resolved_base = resolved_base / name
    return resolved_base

def _is_within_root(root: Path, target: Path) -> bool:
    """Return True if target resolves inside root (or equals it), symlink-safe.

    Compares canonical absolute paths via commonpath so that '..' traversal
    and symlink escapes cannot place the write outside the permitted root.
    """
    try:
        common = os.path.commonpath([str(root), str(target)])
    except ValueError:
        # Different anchors/drives, definitively not contained.
        return False
    return common == str(root)


def _forbidden_dirs() -> list[Path]:
    """Sensitive directories writes must never touch, even inside a valid root.

    Defense-in-depth: the per-request root resolution should already keep
    writes inside a project or temp dir, but this denylist guarantees that
    credential stores, shell/system config, and OS directories are rejected
    regardless of how the root was derived or what path Leo returned.
    """
    home = Path.home().resolve(strict=False)
    return [
        home / ".ssh",
        home / ".aws",
        home / ".config",
        home / ".gnupg",
        home / "Library",
        Path("/etc"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/var"),
        Path("/System"),
    ]


def _temp_roots() -> list[Path]:
    """Canonical temp directories that are always permitted for writes.

    On macOS the system temp dir lives under /var/folders and /tmp is a symlink
    into /private/tmp, so both must be resolved to their real paths to be
    recognized despite the /var entry in the denylist.
    """
    roots = {Path(tempfile.gettempdir()).resolve(strict=False), Path("/tmp").resolve(strict=False)}
    return list(roots)


def _is_forbidden(target: Path) -> bool:
    """Return True if target is in a sensitive location writes must never reach.

    Rejects (a) writing a file directly into the bare home directory (a project
    is expected to have its own subdirectory) and (b) anything within a known
    sensitive directory. Temp directories are explicitly permitted even though
    they may live under a denied prefix (e.g. macOS temp under /var).
    Comparison is symlink-safe via canonical paths.
    """
    resolved = target.expanduser().resolve(strict=False)

    # Temp directories are an allow-exception to the denylist below.
    for temp_root in _temp_roots():
        if resolved == temp_root or _is_within_root(temp_root, resolved):
            return False

    home = Path.home().resolve(strict=False)

    # Bare home: reject a file written directly under $HOME (no project subdir).
    if resolved.parent == home:
        return True

    for forbidden in _forbidden_dirs():
        forbidden = forbidden.resolve(strict=False)
        if resolved == forbidden or _is_within_root(forbidden, resolved):
            return True
    return False


def apply_single_patch(
    payload: str,
    allowed_root: str,
    dry_run: bool = False,
    expected_mtime_ns: Optional[int] = None,
    precreated: bool = False,
) -> Dict[str, Any]:
    """Apply one file patch, returning a compact summary for the caller.

    Validates the payload, resolves the target through its first existing
    ancestor (symlink-safe), enforces containment within allowed_root and a
    sensitive-location denylist, then writes the complete file atomically via a
    temp file and os.replace. A prior version is backed up before overwriting.

    When expected_mtime_ns is provided, the target's current modification time
    is checked just before writing; a mismatch means the file changed after its
    contents were captured for Leo (a TOCTOU race), so the stale write is
    aborted rather than clobbering newer content.
    """
    # Parse and validate the structured payload first; never write on failure.
    from leo_chat.patch_validator import PatchValidator

    ok, detail = PatchValidator.validate(payload, allowed_root, precreated=precreated)
    if not ok:
        return {
            "status": "error",
            "error": detail.get("error", "validation failed"),
            "validation_stage": detail.get("stage", "unknown"),
        }

    patch = detail["patch"]
    target = _resolve_first_existing_ancestor(Path(patch["file"]))
    action = patch["action"]
    content = patch["content"]
    encoded_len = len(content.encode("utf-8"))

    # Anti-TOCTOU: the file must not have changed since its content was read to
    # build Leo's context, or the refactor would be based on a stale version.
    if expected_mtime_ns is not None and target.exists():
        try:
            current_mtime_ns = target.stat().st_mtime_ns
        except OSError as exc:
            return {"status": "error", "error": f"cannot stat target: {exc}"}
        if current_mtime_ns != expected_mtime_ns:
            return {
                "status": "error",
                "error": (
                    f"{target} changed on disk during processing; "
                    "aborting to avoid overwriting newer content"
                ),
            }

    if dry_run:
        return {
            "status": "success",
            "mode": "dry_run",
            "file": str(target),
            "action": action,
            "risk": patch.get("risk", "?"),
            "summary": patch.get("summary", ""),
            "bytes": encoded_len,
        }

    # Ensure parent directories exist for create actions.
    target.parent.mkdir(parents=True, exist_ok=True)

    # Preserve the previous version for recovery before overwriting. The first
    # backup uses '.bak'; if that already exists (a prior write), a numbered
    # suffix is used so the original backup is never clobbered.
    if target.exists():
        backup = _next_backup_path(target)
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")

    # Atomic write: stage to a temp file in the same directory, then replace.
    # os.replace is atomic on the same filesystem and avoids partial writes.
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        # Clean up the staged temp file so no partial artifact remains.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return {"status": "error", "error": f"write failed: {exc}"}

    return {
        "status": "success",
        "mode": "write",
        "file": str(target),
        "action": action,
        "risk": patch.get("risk", "?"),
        "summary": patch.get("summary", ""),
        "bytes": encoded_len,
    }


def _change_region_count(old_str: str, new_str: str) -> int:
    """Count distinct change regions between old and new text, at line level.

    A true "targeted" edit changes exactly one region: one or more CONTIGUOUS
    lines (e.g. a variable renamed across two adjacent lines of one function)
    count as a single region. Over-reach — where the model widened old_str to
    disambiguate a repeated token and accidentally swallowed a second occurrence
    — touches two groups of lines separated by an unchanged line (e.g. the same
    rename applied inside two different functions), which yields >1 region.

    We work at line granularity because token-level diffs fragment a single
    rename into several opcodes (delete/insert on either side of the token),
    which would over-report. Line groups cleanly separate "one logical change"
    from "multiple separated changes".
    """
    old_lines = old_str.splitlines()
    new_lines = new_str.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    changed = set()
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        changed.update(range(i1, i2))

    if not changed:
        return 0

    ordered = sorted(changed)
    groups = 1
    for prev, curr in zip(ordered, ordered[1:]):
        if curr - prev > 1:
            groups += 1
    return groups


def _next_backup_path(target: Path) -> Path:
    """Return a backup path that does not overwrite an existing backup.

    The first backup is '<target>.bak'; subsequent writes use '<target>.bak.1',
    '<target>.bak.2', ... so the original pre-edit version is always preserved.
    """
    base = target.with_suffix(target.suffix + ".bak")
    if not base.exists():
        return base
    n = 1
    while True:
        candidate = target.with_suffix(target.suffix + f".bak.{n}")
        if not candidate.exists():
            return candidate
        n += 1


def apply_edit_patch(
    payload: str,
    allowed_root: str,
    dry_run: bool = False,
    expected_mtime_ns: Optional[int] = None,
) -> Dict[str, Any]:
    """Apply one str_replace edit, returning a compact summary for the caller.

    Parses a LEO_EDIT envelope (FILE + OLD_STR/NEW_STR), enforces containment
    within ``allowed_root`` and the sensitive-location denylist, requires the
    old_str to match EXACTLY ONCE (zero or multiple matches fail with
    ``status:error``, never a partial write), then applies it — atomically via
    temp-file + os.replace with a ``.bak`` backup of the prior version.

    When ``expected_mtime_ns`` is provided, the target's mtime is checked just
    before writing; a mismatch (the file changed after its contents were read
    for Leo) aborts the edit rather than clobbering newer content.
    """
    # Parse and validate the envelope first; never write on failure.
    try:
        edit = _extract_edit_object(payload)
    except (TruncatedPatchError, ValueError) as exc:
        stage = "syntactic" if isinstance(exc, TruncatedPatchError) else "extraction"
        return {"status": "error", "error": str(exc), "validation_stage": stage}

    target = _resolve_first_existing_ancestor(Path(edit["file"]))

    # Safety: containment + denylist (mirrors apply_single_patch).
    if not allowed_root:
        return {"status": "error", "error": "no write root configured"}
    root = Path(allowed_root).expanduser().resolve(strict=False)
    if not _is_within_root(root, target):
        return {"status": "error", "error": f"{target} is outside allowed root {root}"}
    if _is_forbidden(target):
        return {"status": "error", "error": f"{target} is in a forbidden location"}
    if not target.exists():
        return {"status": "error", "error": f"edit target does not exist: {target}"}

    # Read the current file bytes.
    try:
        current = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "error": f"cannot read target: {exc}"}

    old_str = edit["old_str"]
    new_str = edit["new_str"]

    # Exactly-one-unique-match rule: zero or multiple matches are an error.
    if old_str == "":
        return {"status": "error", "error": "old_str is empty (refusing to insert)"}
    count = current.count(old_str)
    if count != 1:
        return {
            "status": "error",
            "error": f"old_str matched {count} occurrence(s) (exactly 1 required)",
        }

    # Over-reach guard: a targeted edit changes exactly one contiguous region.
    # Reject edits whose old_str/new_str differ in multiple separated regions
    # (the model widened the span and mutated more than one spot, e.g. two
    # `result -> total` renames inside a single unique span). This is a semantic
    # safety net beyond the match-count rule, catching the "summary lies / edit
    # over-reaches" failure mode even when old_str is technically unique.
    region_count = _change_region_count(old_str, new_str)
    if region_count != 1:
        return {
            "status": "error",
            "error": (
                f"edit spans {region_count} change region(s) (exactly 1 "
                "contiguous change required); old_str was widened to include "
                "more than one occurrence of the target"
            ),
        }

    new_content = current.replace(old_str, new_str, 1)
    encoded_len = len(new_content.encode("utf-8"))

    # Anti-TOCTOU: abort if the file changed after its content was captured.
    if expected_mtime_ns is not None:
        try:
            if target.stat().st_mtime_ns != expected_mtime_ns:
                return {
                    "status": "error",
                    "error": (
                        f"{target} changed on disk during processing; "
                        "aborting to avoid overwriting newer content"
                    ),
                }
        except OSError as exc:
            return {"status": "error", "error": f"cannot stat target: {exc}"}

    if dry_run:
        return {
            "status": "success",
            "mode": "dry_run",
            "file": str(target),
            "action": edit.get("action", "edit"),
            "risk": edit.get("risk", "?"),
            "summary": edit.get("summary", ""),
            "bytes": encoded_len,
        }

    # Backup prior version, then atomic write via temp + os.replace.
    backup = _next_backup_path(target)
    backup.write_text(current, encoding="utf-8")

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return {"status": "error", "error": f"write failed: {exc}"}

    return {
        "status": "success",
        "mode": "edit",
        "file": str(target),
        "action": edit.get("action", "edit"),
        "risk": edit.get("risk", "?"),
        "summary": edit.get("summary", ""),
        "bytes": encoded_len,
    }