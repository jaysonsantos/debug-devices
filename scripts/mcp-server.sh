#!/usr/bin/env bash
# Start the debug-devices MCP server (stdio) with the dev-shell tools, fast.
#
# Usage: scripts/mcp-server.sh [--dev-reload] [server arguments...]
#
# --dev-reload (only as the first argument), or DEBUG_DEVICES_DEV_RELOAD=1: run `debug-devices-mcp-dev`, a stdio
# proxy that restarts the server when a file of mcp/debug_devices_mcp/ changes. The MCP client keeps its session and
# gets `notifications/tools/list_changed`. A tool call that runs during a reload gets an error: call it again.
# For development only.
#
# `nix develop` takes 5-30 s per start (flake evaluation, a copy of the dirty tree), and MCP clients time out
# (Claude Code: 30 s). This script caches the dev-shell environment (`nix print-dev-env`) and makes it again only
# when flake.nix or flake.lock changes. Stdout is the MCP channel: all other output goes to stderr.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/debug-devices"
FLAKE_FILES=("$ROOT/flake.nix" "$ROOT/flake.lock")
LOCK_TIMEOUT_SECONDS=300

mkdir -p "$CACHE_DIR"
key="$(cat "${FLAKE_FILES[@]}" | sha256sum | cut -d' ' -f1)"
env_file="$CACHE_DIR/dev-env-$key.sh"

if [ ! -s "$env_file" ]; then
    # One writer at a time: several MCP clients can start together.
    exec 9>"$CACHE_DIR/dev-env.lock"
    flock --timeout "$LOCK_TIMEOUT_SECONDS" 9
    if [ ! -s "$env_file" ]; then
        tmp="$(mktemp "$CACHE_DIR/dev-env.XXXXXX")"
        nix print-dev-env "$ROOT" >"$tmp" 2>/dev/stderr
        mv "$tmp" "$env_file"
        find "$CACHE_DIR" -name 'dev-env-*.sh' ! -name "dev-env-$key.sh" -delete
    fi
    exec 9>&-
fi

# The dev-shell script can print text; keep stdout clean for the MCP channel.
# shellcheck disable=SC1090 # The file is made above.
source "$env_file" >&2
entry_point="debug-devices-mcp"
if [ "${1:-}" = "--dev-reload" ]; then
    shift
    entry_point="debug-devices-mcp-dev"
elif [ "${DEBUG_DEVICES_DEV_RELOAD:-0}" = "1" ]; then
    entry_point="debug-devices-mcp-dev"
fi
exec uv run --directory "$ROOT" "$entry_point" "$@"
