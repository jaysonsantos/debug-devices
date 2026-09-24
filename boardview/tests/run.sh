#!/usr/bin/env bash
# Runs the obv-dump tests. Use it in the dev shell: `nix develop --command boardview/tests/run.sh`.
# Set BOARDVIEW_TARGET to a local board file to also run the local-only test.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
exec uv run --group dev pytest boardview/tests "$@"
