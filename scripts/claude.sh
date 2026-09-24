#!/usr/bin/env bash
# Start Claude Code with the debug-devices MCP server. Add --browser to open the monitor page.
# Usage: scripts/claude.sh [--browser] [claude arguments...]
exec "$(dirname "${BASH_SOURCE[0]}")/agent.sh" claude "$@"
