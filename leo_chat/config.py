"""Runtime configuration for the Leo MCP write-to-disk feature.

Write behavior is a global policy resolved once at server startup from the
environment injected during MCP setup. The directory a write is confined to is
NOT global: it is derived per-request from the target file (its project root or
a temp directory), so a single server can safely serve multiple projects.
"""

import os


class WriteMode:
    """Disk-write policy for patch-capable skills."""
    DISABLED = "disabled"   # Leo returns output only; nothing is written.
    DRY_RUN = "dry_run"     # Report what would be written without touching disk.
    ENABLED = "enabled"     # Write the modified file to disk.


def resolve_write_mode() -> str:
    """Resolve the active write mode from the environment.

    Defaults to ENABLED so the token-saving delegation path (Leo writes the
    file, the caller receives only a compact summary) is the out-of-the-box
    behavior. Any explicitly set, recognized value wins; an unrecognized value
    falls back to the ENABLED default. Per-request path confinement and the
    patch_writer allow/deny lists keep this safe.
    """
    mode = os.environ.get("LEO_WRITE_MODE", "").lower().strip()
    if mode in (WriteMode.DISABLED, WriteMode.DRY_RUN, WriteMode.ENABLED):
        return mode
    return WriteMode.ENABLED