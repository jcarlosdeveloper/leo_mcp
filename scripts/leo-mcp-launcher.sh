#!/bin/sh
# Leo MCP Server — LaunchAgent launcher wrapper.
#
# Invoked by launchd (see dev.duarte.leo-mcp.plist). It resolves the install
# directory relative to this script, then execs the server with the isolated
# uv virtualenv and a pinned Playwright browsers path.
#
# Environment handed to the server:
#   PLAYWRIGHT_BROWSERS_PATH  -> bundled Chromium for ask_leo_quick/extensive
#   BRAVE_PROFILE             -> user-overridable Brave profile (default: Default)

set -eu

# Resolve the install dir (this script lives at <install>/scripts/leo-mcp-launcher.sh).
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_DIR=$(dirname "$SCRIPT_DIR")

VENV_PYTHON="$INSTALL_DIR/venv/bin/python"
SERVER="$INSTALL_DIR/leo_mcp_server.py"

[ -x "$VENV_PYTHON" ] || {
    printf 'error: venv python not found at %s\n' "$VENV_PYTHON" >&2
    exit 1
}
[ -f "$SERVER" ] || {
    printf 'error: server not found at %s\n' "$SERVER" >&2
    exit 1
}

# Default Playwright browsers path, overridable by the user's environment.
PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$INSTALL_DIR/ms-playwright}"
export PLAYWRIGHT_BROWSERS_PATH

exec "$VENV_PYTHON" "$SERVER"