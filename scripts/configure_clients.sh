#!/bin/sh
# Leo MCP Server — MCP client auto-configuration.
#
# Registers the running server (http://127.0.0.1:9223/mcp) with any supported
# CLI MCP client that is present. Idempotent: re-running replaces the existing
# "leo" entry rather than duplicating it.
#
# Supported clients:
#   Claude Code  -> `claude mcp add --transport http --scope user leo <url>`
#   OpenCode     -> `~/.config/opencode/opencode.json`  mcp.leo (type "remote")
#   Cline        -> `cline mcp add --transport http --yes leo <url>`

set -eu

SERVER_URL="http://127.0.0.1:9223/mcp"
SERVER_NAME="leo"

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --server-url) shift; SERVER_URL=${1:-$SERVER_URL} ;;
            --server-command) shift ;;  # accepted for compat; HTTP transport is used
            *) ;;
        esac
        shift
    done
}

# ── Claude Code ──────────────────────────────────────────────────────────────
configure_claude() {
    command -v claude >/dev/null 2>&1 || return 0
    printf 'Configuring Claude Code...\n'
    claude mcp add --transport http --scope user "$SERVER_NAME" "$SERVER_URL"
}

# ── OpenCode ────────────────────────────────────────────────────────────────
configure_opencode() {
    command -v opencode >/dev/null 2>&1 || return 0
    cfg="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
    [ -f "$cfg" ] || return 0
    printf 'Configuring OpenCode (%s)...\n' "$cfg"

    python3 - "$cfg" "$SERVER_NAME" "$SERVER_URL" <<'PY'
import json, sys

cfg_path, name, url = sys.argv[1], sys.argv[2], sys.argv[3]
with open(cfg_path) as f:
    data = json.load(f)

data.setdefault("mcp", {})[name] = {
    "type": "remote",
    "url": url,
    "enabled": True,
}

with open(cfg_path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
}

# ── Cline ────────────────────────────────────────────────────────────────────
configure_cline() {
    command -v cline >/dev/null 2>&1 || return 0
    printf 'Configuring Cline...\n'
    cline mcp add --transport http --yes "$SERVER_NAME" "$SERVER_URL"
}

parse_args "$@"

configure_claude
configure_opencode
configure_cline

printf 'MCP client configuration complete (server: %s).\n' "$SERVER_URL"