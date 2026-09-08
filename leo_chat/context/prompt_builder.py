"""
Prompt Builder - Build contexts from files and conversation history
"""
import time
import os
import logging
from pathlib import Path
from typing import List, Dict, Optional

from leo_chat.helpers import _log_json

logger = logging.getLogger(__name__)

# ── Module-level constants ────────────────────────────────────────────────────

# Maximum file size to read (100KB). Larger files are skipped with a warning.
MAX_FILE_SIZE = 100_000

# Max directory depth to search upwards for the project root.
_MAX_PROJECT_DEPTH = 6

# Single source of truth for project context files. Used BOTH to detect the
# project root AND to decide which files to auto-inject. Keeping one list
# prevents the previous desync (e.g. Makefile-only projects weren't detected).
_PROJECT_CONTEXT_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    "pyproject.toml",
    "Cargo.toml",
    "package.json",
    "go.mod",
    "Makefile",
    "CMakeLists.txt",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    ".gitignore",
]

def assemble_context(filepaths: List[str], raw: bool = False) -> str:
    """
    Iterate over the paths, expand ~, and read the content.

    Expected output format per file:
        --- FILE START: {path} ---
        [content]
        --- FILE END ---

    If reading a file fails, the exception is caught and a warning text block
    is inserted instead of crashing.

    Additionally, auto-detects and injects project-level context files
    (AGENTS.md, pyproject.toml, .cursorrules, etc.) found by walking up from
    the directories of the requested filepaths.

    Args:
        filepaths: List of file paths (absolute, relative, or with ~).
        raw: When True, read each file byte-faithfully — no line-number
            prefixes, no metadata header, no trailing-whitespace stripping —
            and skip project-context auto-injection. Required by the patch
            path so the model receives (and can reproduce) the exact file on
            disk. When False (default, advisory/prose use) files are annotated
            with line numbers and metadata for easier human-style reference.

    Returns:
        Concatenated content of all files with standardized formatting.
    """
    if not filepaths:
        return ""

    context_parts = []

    # ── Auto-inject project context (AGENTS.md, pyproject.toml, etc.) ─────────
    # Skipped in raw mode: mixing convention docs into a byte-faithful patch
    # context would pollute the single file the model must reproduce verbatim.
    if not raw:
        project_context_files = _detect_project_context(filepaths)
        if project_context_files:
            context_parts.append(
                "=== PROJECT CONTEXT (auto-injected) ===\n"
                "The following files define the project conventions, dependencies, "
                "and instructions. Read them before answering.\n"
            )
            for pc_path, pc_content in project_context_files.items():
                context_parts.append(
                    f"--- PROJECT FILE: {pc_path} ---\n"
                    f"{pc_content}\n"
                    f"--- END PROJECT FILE ---\n"
                )
            context_parts.append("=== END PROJECT CONTEXT ===\n")

    # ── User-requested files ──────────────────────────────────────────────────
    for filepath in filepaths:
        try:
            # 1. Expand ~ and resolve absolute path
            path = Path(filepath).expanduser().resolve()

            # 2. Verify existence
            if not path.exists():
                warning = f"⚠️ WARNING: File not found: {filepath}"
                logger.warning(warning)
                context_parts.append(
                    f"--- FILE START: {filepath} ---\n{warning}\n--- FILE END ---\n"
                )
                continue

            if not path.is_file():
                warning = f"⚠️ WARNING: Not a file: {filepath}"
                logger.warning(warning)
                context_parts.append(
                    f"--- FILE START: {filepath} ---\n{warning}\n--- FILE END ---\n"
                )
                continue

            # 3. Read content
            content = _read_file_safe(path, raw=raw)

            # 4. Format output
            if content is not None:
                header = f"--- FILE START: {path} ---"
                footer = "--- FILE END ---"
                context_parts.append(f"{header}\n{content}\n{footer}\n")
            else:
                warning = f"⚠️ WARNING: Empty or unreadable file: {filepath}"
                context_parts.append(
                    f"--- FILE START: {filepath} ---\n{warning}\n--- FILE END ---\n"
                )

        except Exception as e:
            # Catch ANY exception and continue with the next file
            error_msg = (
                f"⚠️ ERROR reading {filepath}: {type(e).__name__}: {str(e)}"
            )
            logger.error(error_msg, exc_info=True)
            context_parts.append(
                f"--- FILE START: {filepath} ---\n{error_msg}\n--- FILE END ---\n"
            )

    return "\n".join(context_parts)

def _read_file_safe(path: Path, raw: bool = False) -> Optional[str]:
    """
    Read a file with safe handling of encodings and size.

    In annotated mode (raw=False) returns the content with line numbers
    prefixed (format: 'LINE|CONTENT') and a metadata header including the
    detected language. In raw mode (raw=True) returns the decoded file content
    verbatim — no header, no numbering, no whitespace stripping — so it can be
    reproduced byte-for-byte.

    Uses the module-level MAX_FILE_SIZE constant (no longer a local value).

    Args:
        path: File path.
        raw: When True, return verbatim content with no annotations.

    Returns:
        File content (verbatim in raw mode, annotated otherwise), or None on
        error. (Note: oversized/undecodable files return a ⚠️ warning STRING,
        not None — callers treat any non-None value as displayable content.)
    """
    try:
        # Check size
        file_size = path.stat().st_size

        if file_size > MAX_FILE_SIZE:
            logger.warning(
                f"File too large ({file_size:,} bytes > {MAX_FILE_SIZE:,}): {path}"
            )
            return f"⚠️ File exceeds limit of {MAX_FILE_SIZE:,} bytes"

        if file_size == 0:
            return ""

        # Try several encodings (most common to least common)
        encodings = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]

        for encoding in encodings:
            try:
                if raw:
                    # Verbatim read: preserve every byte-level detail (trailing
                    # whitespace, blank lines, final-newline presence) so the
                    # model reproduces the file exactly as it exists on disk.
                    with open(path, "r", encoding=encoding, newline="") as f:
                        return f.read()

                with open(path, "r", encoding=encoding) as f:
                    lines = f.readlines()
                    total_lines = len(lines)
                    logger.debug(
                        f"✅ Read {path.name} with encoding {encoding} "
                        f"({total_lines} lines)"
                    )

                    # ── Build annotated output ──────────────────────────────
                    language = _detect_language(path)
                    header = (
                        f"# File: {path.name}\n"
                        f"# Language: {language}\n"
                        f"# Lines: {total_lines}\n"
                        f"# Size: {_format_size(file_size)}\n"
                    )
                    # Line-numbered content
                    numbered = "\n".join(
                        f"{i + 1:>5}|{line.rstrip()}"
                        for i, line in enumerate(lines)
                    )
                    return f"{header}\n{numbered}"

            except UnicodeDecodeError:
                continue

        # No encoding worked
        logger.error(
            f"❌ Could not decode {path} with any supported encoding"
        )
        return f"⚠️ ERROR: Unsupported encoding for {path.name}"

    except PermissionError:
        logger.error(f"❌ Permission denied: {path}")
        return f"⚠️ ERROR: Permission denied for {path}"

    except Exception as e:
        logger.error(f"❌ Error reading {path}: {e}")
        return f"⚠️ ERROR: {type(e).__name__}: {str(e)}"


# ── File Metadata Helpers ────────────────────────────────────────────────────

# Extension → language name mapping
_EXT_TO_LANG = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (React)",
    ".jsx": "JavaScript (React)",
    ".rs": "Rust",
    ".go": "Go",
    ".mod": "Go Module",
    ".java": "Java",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".c": "C",
    ".cpp": "C++",
    ".h": "C/C++ Header",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell",
    ".bash": "Bash",
    ".zsh": "Zsh",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".less": "Less",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".txt": "Plain Text",
    ".cfg": "Config",
    ".ini": "INI",
    ".env": "Environment",
    ".dockerfile": "Dockerfile",
    ".lock": "Lockfile",
}


def _detect_language(path: Path) -> str:
    """Detect programming language from file extension or special filename."""
    suffix = path.suffix.lower()
    if not suffix:
        # No extension → check for well-known filenames
        name = path.name.lower()
        if name == "dockerfile":
            return "Dockerfile"
        if name == "makefile":
            return "Makefile"
        if name in ("agents.md", "agentes.md"):
            return "Markdown"
        return "Unknown"
    return _EXT_TO_LANG.get(suffix, f"Unknown ({suffix})")


def _format_size(size_bytes: int) -> str:
    """Format a file size in human-readable form (B / KB / MB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"



class PromptBuilder:
    """
    Full prompt builder for integration with Skills.

    Usage:
        builder = PromptBuilder()
        full_prompt = builder.build(skill, user_prompt, files=['~/file.py'])
    """

    def __init__(self):
        # TODO: context_cache is defined but not yet used. Future: cache
        # assemble_context() results keyed by filepath + mtime to avoid
        # re-reading unchanged files across repeated builds.
        self.context_cache = {}

    def load_files(self, filepaths: List[str]) -> str:
        """Alias of assemble_context for compatibility with the Skills API."""
        return assemble_context(filepaths)

    def build(
        self,
        skill,
        user_prompt: str,
        files: Optional[List[str]] = None,
        extra_context: str = "",
    ) -> str:
        """
        Build the full prompt using a skill.

        Args:
            skill: BaseSkill instance.
            user_prompt: User prompt.
            files: List of files to include.
            extra_context: Additional context to prepend.

        Returns:
            Fully formatted prompt (delegated to skill.get_full_prompt).
        """
        # Load file context
        file_context = self.load_files(files) if files else ""

        # Combine contexts (extra_context first, then file context)
        full_context = extra_context
        if file_context:
            full_context = (
                f"{full_context}\n\n{file_context}"
                if full_context
                else file_context
            )

        # Delegate final formatting to the skill
        return skill.get_full_prompt(user_prompt, full_context)


# ── Project Context Auto-Detection ───────────────────────────────────────────


def _find_project_root(filepath: str) -> Optional[Path]:
    """Walk up from a single filepath to the nearest project root.

    Tolerates a non-existent target (a create action, or a modify on a file
    that was deleted): the walk-up starts from the nearest existing ancestor
    directory, so the project root is still found by its markers.

    A directory qualifies as a project root when it contains any marker in
    _PROJECT_CONTEXT_FILES. Returns the resolved root Path, or None if no
    marker is found within _MAX_PROJECT_DEPTH levels.
    """
    # strict=False: do NOT require the target to exist (create / deleted file).
    path = Path(filepath).expanduser().resolve(strict=False)

    # Start from the file's parent, then climb to the first existing directory.
    candidate = path.parent
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent

    for _ in range(_MAX_PROJECT_DEPTH):
        if any((candidate / m).exists() for m in _PROJECT_CONTEXT_FILES):
            return candidate
        parent = candidate.parent
        if parent == candidate:  # filesystem root
            break
        candidate = parent
    return None


def _detect_project_context(filepaths: List[str]) -> Dict[str, str]:
    """
    Walk up from each filepath's directory looking for project context files.

    Only the first project root found is used (all filepaths are assumed to
    belong to the same project). Files already present in the user's explicit
    filepaths list are skipped to avoid duplication.

    FIX (#5): both root detection and file injection now use the SINGLE
    _PROJECT_CONTEXT_FILES list. Previously a separate inline `markers` list
    omitted Makefile/CMakeLists.txt, so projects defined only by those files
    were never detected.

    Args:
        filepaths: List of user-requested file paths.

    Returns:
        Dict mapping relative file names to their contents.
    """
    if not filepaths:
        return {}

    # Find a project root by walking up from the first valid filepath.
    project_root = None
    for fp in filepaths:
        project_root = _find_project_root(fp)
        if project_root:
            break

    if project_root is None:
        return {}

    # Collect project context files (avoid duplicating user-requested files)
    user_resolved = {
        Path(fp).expanduser().resolve() for fp in filepaths
    }
    result: Dict[str, str] = {}

    for ctx_file in _PROJECT_CONTEXT_FILES:
        ctx_path = project_root / ctx_file
        if not ctx_path.exists() or not ctx_path.is_file():
            continue
        if ctx_path in user_resolved:
            continue  # User already requested this explicitly

        content = _read_file_safe(ctx_path)
        if content is not None and content.strip():
            # Use a relative path from the project root for cleaner output
            try:
                rel = ctx_path.relative_to(project_root)
            except ValueError:
                rel = ctx_path
            result[str(rel)] = content

    return result