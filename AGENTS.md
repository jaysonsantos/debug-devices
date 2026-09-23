# debug-devices

Tools that let a coding agent (Claude Code) see and measure real hardware while it debugs.

## Parts

- `android/`: Kotlin Android app. Remote-controlled back camera: zoom in, zoom out, torch (flash), snapshot. It exposes the HTTP API in `docs/phone-api.md`.
- `mcp/`: Python MCP server (stdio). Tools for the phone camera (through ADB port forward) and for the PC webcam that points at a multimeter. The multimeter tool sends a webcam frame to an OpenRouter vision model (default `openai/gpt-6-luna`) and returns the reading and the meter mode.
- `docs/phone-api.md`: the contract between the app and the MCP server. Change it first.

## Rules

- Follow the `jayson-code-conventions` and `jayson-python-conventions` skills.
- Kotlin: CameraX, coroutines, kotlinx.serialization, no magic values (constants in a companion or an object).
- Python: Python 3.14, uv, ruff (line length 120), pydantic v2 models, pytest.
- Config through CLI flags and environment variables with defaults. Secrets (`OPENROUTER_API_KEY`) in `.env`, never committed.
- Commits: Conventional Commits.
- The dev shell comes from `flake.nix` (`nix develop` or direnv).
