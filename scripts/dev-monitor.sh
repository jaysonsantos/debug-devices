#!/usr/bin/env bash
# Run the MCP server with the monitor page and restart it when the code changes.
# --ui-start eager: the page and the webcam stream start at once (the default is lazy, for agent sessions).
# Stdin stays open with no input, so the stdio server does not exit.
# Open the page once yourself: http://127.0.0.1:18766/ (it reconnects after each restart).
set -euo pipefail

cd "$(dirname "$0")/.."

WATCH_DIR="mcp/debug_devices_mcp"
WATCH_EXTENSIONS="py,html,js,css"

exec watchexec --restart --watch "$WATCH_DIR" --exts "$WATCH_EXTENSIONS" --stop-signal SIGTERM --shell=none -- \
  sh -c 'sleep infinity | uv run debug-devices-mcp --ui-start eager --no-ui-open-browser "$@"' sh "$@"
