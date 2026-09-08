# Leo MCP Server — macOS Installer

One-line install (standard `curl | sh` pattern):

```bash
curl -fsSL "https://raw.githubusercontent.com/tlalocaimx/leo_mcp/main/scripts/install.sh" | sh
```

> The `curl | sh` pattern runs a remote script. Review it first if you prefer:
> `curl -fsSL .../install.sh | less`, or clone the repo and run
> `sh scripts/install.sh` locally.

## What it does

1. **Installs `uv`** if missing — this bundles a standalone Python 3.12+, so
   the user does not need Python installed.
2. **Creates an isolated virtualenv** under `~/.local/share/leo-mcp/venv` and
   installs all pinned dependencies from `requirements.txt`.
3. **Installs Playwright Chromium** into
   `~/.local/share/leo-mcp/ms-playwright` so `ask_leo_quick` / `ask_leo_extensive`
   work offline (independent of the Brave CDP connection).
4. **Registers an always-on per-user LaunchAgent** (`dev.duarte.leo-mcp`) with
   `KeepAlive` so the server restarts on crash and survives logout/login.
   Per-user (not a system daemon) is required for Keychain + Brave CDP access.
5. **Auto-configures CLI MCP clients** that are present:
   - Claude Code → `claude mcp add --transport http --scope user leo ...`
   - OpenCode → `~/.config/opencode/opencode.json` `mcp.leo` (type `remote`)
   - Cline → `cline mcp add --transport http --yes leo ...`

## Architecture

```
curl | sh
   └─ uv (standalone Python 3.12)
       └─ ~/.local/share/leo-mcp/
            ├─ venv/                 isolated deps
            ├─ ms-playwright/        bundled Chromium
            ├─ leo_mcp_server.py     entry point
            └─ scripts/leo-mcp-launcher.sh   exec'd by launchd
   └─ ~/Library/LaunchAgents/dev.duarte.leo-mcp.plist
   └─ MCP transport  →  http://127.0.0.1:9223/mcp
```

The server itself bootstraps Brave with `--remote-debugging-port=9222` on
startup; the installer never force-quits Brave.

## Options

| Flag                | Effect                                      |
|---------------------|---------------------------------------------|
| `--skip-clients`    | Skip MCP client auto-config                 |
| `--skip-playwright` | Skip Playwright Chromium install            |
| `--skip-agent`      | Skip always-on LaunchAgent registration     |
| `--dry-run`         | Print commands without executing            |
| `--help` / `-h`     | Show help                                   |

Example:

```bash
sh scripts/install.sh --dry-run
```

## Post-install

- Transport: `http://127.0.0.1:9223/mcp`
- Logs: `~/Library/Logs/leo-mcp/stdout.log` and `stderr.log`
- First tool call triggers the macOS Keychain prompt — choose **Always Allow**.

## Uninstall

```bash
~/.local/share/leo-mcp/scripts/uninstall.sh          # keep logs
~/.local/share/leo-mcp/scripts/uninstall.sh --purge-logs
```

## Files

| File | Purpose |
|------|---------|
| `scripts/install.sh` | Main `curl \| sh` entry point |
| `scripts/leo-mcp-launcher.sh` | launchd wrapper (resolves venv + env) |
| `scripts/configure_clients.sh` | Idempotent MCP client config |
| `scripts/uninstall.sh` | Cleanup + client revert |
| `dev.duarte.leo-mcp.plist` | LaunchAgent template |