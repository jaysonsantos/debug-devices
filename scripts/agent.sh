#!/usr/bin/env bash
# Start Claude Code or Codex with the debug-devices MCP server, inside the dev shell.
#
# Usage: scripts/agent.sh <claude|codex> [--browser] [agent arguments...]
#
#   --browser   Open the monitor page in a new Firefox window at the first tool call (the server starts lazily).
#               Without it, the page does not open. Use it when no monitor runs yet.
#
# Run it from the project that you debug: the agent starts in the current directory.
# The MCP server reads .env from this repository.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_NAME="debug-devices"
CODEX_SERVER_KEY="mcp_servers.debug_devices"
BROWSER_FLAG="--browser"
OPEN_BROWSER_ARG="--ui-open-browser"
NO_OPEN_BROWSER_ARG="--no-ui-open-browser"

usage() {
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

[ $# -ge 1 ] || usage
agent="$1"
shift

open_browser_arg="$NO_OPEN_BROWSER_ARG"
if [ "${1:-}" = "$BROWSER_FLAG" ]; then
    open_browser_arg="$OPEN_BROWSER_ARG"
    shift
fi

server_args=(run --directory "$ROOT" debug-devices-mcp "$open_browser_arg")

# jq comes from the dev shell. It quotes the path correctly, also with spaces.
run_in_shell() {
    exec nix develop "$ROOT" --command bash -c "$1" bash "${@:2}"
}

case "$agent" in
    claude)
        # shellcheck disable=SC2016 # The inner bash expands these variables.
        run_in_shell '
            name="$1"; shift
            n_args="$1"; shift
            server_args=("${@:1:$n_args}")
            shift "$n_args"
            config="$(jq -cn --arg name "$name" '"'"'$ARGS.positional as $a | {mcpServers: {($name): {command: "uv", args: $a}}}'"'"' --args -- "${server_args[@]}")"
            exec claude --mcp-config "$config" "$@"
        ' "$SERVER_NAME" "${#server_args[@]}" "${server_args[@]}" "$@"
        ;;
    codex)
        # shellcheck disable=SC2016 # The inner bash expands these variables.
        run_in_shell '
            key="$1"; shift
            n_args="$1"; shift
            server_args=("${@:1:$n_args}")
            shift "$n_args"
            args_toml="$(jq -cn '"'"'$ARGS.positional'"'"' --args -- "${server_args[@]}")"
            exec codex -c "$key.command=\"uv\"" -c "$key.args=$args_toml" "$@"
        ' "$CODEX_SERVER_KEY" "${#server_args[@]}" "${server_args[@]}" "$@"
        ;;
    *)
        usage
        ;;
esac
