#!/bin/sh
# Leo MCP Server — macOS installer
#
# Usage (standard pattern):
#   curl -fsSL "https://raw.githubusercontent.com/jcarlosdeveloper/leo_mcp/main/scripts/install.sh" | sh
#
# This installer:
#   1. Ensures `uv` is present (bundles a standalone Python 3.12+).
#   2. Installs leo-mcp-server into an isolated uv environment.
#   3. Installs Playwright Chromium for the web-search tools.
#   4. Registers an always-on per-user LaunchAgent.
#   5. Auto-configures supported CLI MCP clients (Claude Code, OpenCode, Cline).
#
# Non-interactive by default; run with `sh -s -- --help` for options.

set -eu

# ── Tunables ────────────────────────────────────────────────────────────────
REPO_ARCHIVE_URL="https://github.com/jcarlosdeveloper/leo_mcp/archive/refs/heads/main.tar.gz"
PYTHON_VERSION="3.12"
MIN_UV_VERSION="0.5.0"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"
INSTALL_DIR="${LEO_MCP_INSTALL_DIR:-$HOME/.local/share/leo-mcp}"
LAUNCH_AGENT_LABEL="dev.duarte.leo-mcp"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$LAUNCH_AGENT_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/leo-mcp"

# ── Flags ───────────────────────────────────────────────────────────────────
dry_run=0
skip_clients=0
skip_playwright=0
skip_agent=0

show_usage() {
    cat <<'USAGE'
Usage: install.sh [options]

Installs or updates Leo MCP Server on macOS.

Options:
  --skip-clients      Do not auto-configure MCP client configs.
  --skip-playwright   Do not install Playwright Chromium.
  --skip-agent        Do not register the always-on LaunchAgent.
  --dry-run           Print commands without running them.
  --help, -h          Show this help text.
USAGE
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

step() {
    printf '\n==> %s\n' "$1"
}

require_command() {
    if [ "$dry_run" -eq 0 ] && ! command -v "$1" >/dev/null 2>&1; then
        fail "$1 is required. Install it first, then rerun this installer."
    fi
}

# ── uv helpers (mirrors astral's conventions) ───────────────────────────────
uv_bin_dir() {
    if [ -n "${UV_INSTALL_DIR:-}" ]; then
        printf '%s\n' "$UV_INSTALL_DIR"
    elif [ -n "${XDG_BIN_HOME:-}" ]; then
        printf '%s\n' "$XDG_BIN_HOME"
    else
        printf '%s/.local/bin\n' "${HOME:-$HOME}"
    fi
}

current_uv_version() {
    if out=$(uv --version 2>/dev/null); then
        case "$out" in
            uv\ *) out=${out#uv } ;;
        esac
        printf '%s\n' "${out%% *}"
    else
        return 1
    fi
}

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        printf 'uv already found; leaving it unchanged.\n'
        return 0
    fi
    step "Installing uv (standalone Python toolchain)"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ curl -LsSf %s | sh\n' "$UV_INSTALL_URL"
        return 0
    fi
    curl -LsSf "$UV_INSTALL_URL" | sh
    export PATH="$(uv_bin_dir):$PATH"
}

# ── Install the server ──────────────────────────────────────────────────────
install_server() {
    step "Installing leo-mcp-server"

    if [ "$dry_run" -eq 1 ]; then
        printf '+ mkdir -p %s\n' "$INSTALL_DIR"
        printf '+ curl -fsSL %s | tar -xz -C <tmp>\n' "$REPO_ARCHIVE_URL"
        printf '+ uv venv --python %s %s/venv\n' "$PYTHON_VERSION" "$INSTALL_DIR"
        printf '+ uv pip install -r %s/requirements.txt\n' "$INSTALL_DIR"
        return 0
    fi

    mkdir -p "$INSTALL_DIR"

    tmp=$(mktemp -d "${TMPDIR:-/tmp}/leo-mcp.XXXXXX") || fail "mktemp failed"
    trap 'rm -rf "$tmp"' EXIT

    printf 'Downloading %s\n' "$REPO_ARCHIVE_URL"
    curl -fsSL "$REPO_ARCHIVE_URL" | tar -xz -C "$tmp" --strip-components=1 \
        || fail "Could not download and extract the leo-mcp source."

    # Preserve any existing venv; rsync source files over it.
    cp -R "$tmp"/. "$INSTALL_DIR"/

    printf 'Creating isolated virtualenv (Python %s)\n' "$PYTHON_VERSION"
    uv venv --python "$PYTHON_VERSION" "$INSTALL_DIR/venv"

    printf 'Installing dependencies\n'
    uv pip install --python "$INSTALL_DIR/venv/bin/python" -r "$INSTALL_DIR/requirements.txt"
}

# ── Playwright Chromium (web-search tools) ──────────────────────────────────
install_playwright() {
    [ "$skip_playwright" -eq 0 ] || return 0
    step "Installing Playwright Chromium"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ PLAYWRIGHT_BROWSERS_PATH=%s/ms-playwright %s/venv/bin/playwright install chromium\n' \
            "$INSTALL_DIR" "$INSTALL_DIR"
        return 0
    fi
    PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/ms-playwright" \
        "$INSTALL_DIR/venv/bin/playwright" install chromium
}

# ── Always-on LaunchAgent ───────────────────────────────────────────────────
install_launch_agent() {
    [ "$skip_agent" -eq 0 ] || return 0
    [ "$(uname -s)" = "Darwin" ] || return 0

    step "Registering always-on LaunchAgent"

    if [ "$dry_run" -eq 1 ]; then
        printf '+ write %s\n' "$LAUNCH_AGENT_PLIST"
        printf '+ launchctl unload %s (ignore failure)\n' "$LAUNCH_AGENT_PLIST"
        printf '+ launchctl load %s\n' "$LAUNCH_AGENT_PLIST"
        return 0
    fi

    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

    cat > "$LAUNCH_AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LAUNCH_AGENT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/scripts/leo-mcp-launcher.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/stderr.log</string>
</dict>
</plist>
PLIST

    # Reload: unload any prior instance, then load the new one.
    launchctl unload "$LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || true
    launchctl load "$LAUNCH_AGENT_PLIST"
    printf 'Loaded %s\n' "$LAUNCH_AGENT_LABEL"
}

# ── Client config (delegated) ───────────────────────────────────────────────
configure_clients() {
    [ "$skip_clients" -eq 0 ] || return 0
    step "Configuring MCP clients"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ %s/scripts/configure_clients.sh\n' "$INSTALL_DIR"
        return 0
    fi
    if [ -x "$INSTALL_DIR/scripts/configure_clients.sh" ]; then
        sh "$INSTALL_DIR/scripts/configure_clients.sh" \
            --server-url "http://127.0.0.1:9223/mcp" \
            --server-command "$INSTALL_DIR/venv/bin/python $INSTALL_DIR/leo_mcp_server.py"
    else
        printf 'configure_clients.sh not found; skipping client auto-config.\n'
    fi
}

# ── Entry ───────────────────────────────────────────────────────────────────
parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --skip-clients) skip_clients=1 ;;
            --skip-playwright) skip_playwright=1 ;;
            --skip-agent) skip_agent=1 ;;
            --dry-run) dry_run=1 ;;
            --help|-h) show_usage; exit 0 ;;
            *) show_usage >&2; fail "unknown option: $1" ;;
        esac
        shift
    done
}

parse_args "$@"

step "Checking prerequisites"
require_command curl
require_command tar

ensure_uv
install_server
install_playwright
install_launch_agent
configure_clients

if [ "$dry_run" -eq 1 ]; then
    printf '\nDry run complete. No changes were made.\n'
    exit 0
fi

cat <<'DONE'

Leo MCP Server is installed and running as a background service.

  - Transport:  http://127.0.0.1:9223/mcp
  - Logs:       ~/Library/Logs/leo-mcp/
  - Uninstall:  $INSTALL_DIR/scripts/uninstall.sh

Next steps:
  1. Make sure Brave Browser is installed. The server auto-bootstraps it with
     the debug port (9222) on first run.
  2. On first tool call, macOS will prompt for Keychain access — choose
     "Always Allow".
DONE