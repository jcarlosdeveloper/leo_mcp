#!/bin/sh
# Leo MCP Server — uninstaller.
#
# Removes the LaunchAgent, the install directory, and reverts client configs
# (the "leo" MCP entry). Logs under ~/Library/Logs/leo-mcp are left in place
# unless --purge-logs is passed.

set -eu

INSTALL_DIR="${LEO_MCP_INSTALL_DIR:-$HOME/.local/share/leo-mcp}"
LAUNCH_AGENT_LABEL="dev.duarte.leo-mcp"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$LAUNCH_AGENT_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/leo-mcp"

purge_logs=0

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --purge-logs) purge_logs=1 ;;
            --help|-h) printf 'Usage: uninstall.sh [--purge-logs]\n'; exit 0 ;;
            *) fail "unknown option: $1" ;;
        esac
        shift
    done
}

parse_args "$@"

printf 'Uninstalling Leo MCP Server...\n'

# 1. Stop and remove the LaunchAgent.
if [ -f "$LAUNCH_AGENT_PLIST" ]; then
    launchctl unload "$LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
    rm -f "$LAUNCH_AGENT_PLIST"
    printf 'Removed LaunchAgent %s\n' "$LAUNCH_AGENT_LABEL"
fi

# 2. Revert client configs (best effort; CLI tools may be absent).
revert_clients() {
    if command -v claude >/dev/null 2>&1; then
        claude mcp remove leo >/dev/null 2>&1 || true
    fi
    if command -v cline >/dev/null 2>&1; then
        cline mcp remove leo >/dev/null 2>&1 || true
    fi
    cfg="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
    if [ -f "$cfg" ] && command -v python3 >/dev/null 2>&1; then
        python3 - "$cfg" <<'PY'
import json, sys
cfg_path = sys.argv[1]
try:
    with open(cfg_path) as f:
        data = json.load(f)
    data.get("mcp", {}).pop("leo", None)
    with open(cfg_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
except Exception:
    pass
PY
    fi
}
revert_clients

# 3. Remove the install directory.
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    printf 'Removed %s\n' "$INSTALL_DIR"
fi

# 4. Optionally purge logs.
if [ "$purge_logs" -eq 1 ]; then
    rm -rf "$LOG_DIR"
    printf 'Removed %s\n' "$LOG_DIR"
else
    printf 'Logs kept at %s (pass --purge-logs to remove).\n' "$LOG_DIR"
fi

printf 'Leo MCP Server uninstalled.\n'