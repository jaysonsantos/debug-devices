#!/usr/bin/env bash
# Start Codex with the debug-devices MCP server. Add --browser to open the monitor page.
# Usage: scripts/codex.sh [--browser] [codex arguments...]
exec "$(dirname "${BASH_SOURCE[0]}")/agent.sh" codex "$@"
