# debug-devices

Tools that let a coding agent (Claude Code) see and measure real hardware while it debugs.

## Parts

- `android/`: Kotlin Android app. Remote-controlled back camera: zoom, torch (flash), rotation, snapshot. It exposes the HTTP API in `docs/phone-api.md` on `127.0.0.1:8765` of the phone.
- `mcp/`: Python MCP server (stdio), entry point `debug-devices-mcp`.
  - Phone tools (`phone_*`): through `adb forward` to the app.
  - Webcam and multimeter (`webcam_snapshot`, `multimeter_read`): one shared ffmpeg stream of the PC webcam. The multimeter tool sends the cropped frame to an OpenRouter vision model (default `openai/gpt-6-luna`).
  - Board tools (`board_*`): run `obv-dump` and answer part, net, and position questions.
  - Monitor page (`mcp/debug_devices_mcp/ui/`): local web page on `127.0.0.1:18766` with the webcam, the phone screen (scrcpy-server H.264), the controls, and a live tool-call log.
  - Lazy start (`--ui-start lazy`, default): nothing runs at process start. The first tool call starts the page, the first webcam use starts ffmpeg, and `phone_connect` starts adb. `bench_start` and `bench_stop` start and stop everything.
- `boardview/`: C++ CLI `obv-dump` on the OpenBoardView 10.0.0 parsers (MIT). The flake builds it from a pinned tag plus `boardview/patches/`.
- `scripts/`: fake phone, fake adb, contract tests, `dev-monitor.sh` (watchexec live reload), demo recording.
- `docs/`: contracts, research, and agent reports (`docs/reports/`).

## Contracts

Change the contract first, then both sides.

- `docs/phone-api.md`: app and MCP server.
- `docs/boardview-json.md`: `obv-dump` and MCP server.

## Commands

Run them in the dev shell (`nix develop` or direnv with `use flake`).

```sh
uv run pytest                                          # Python tests
uv run ruff check && uv run ruff format --check        # Python lint
(cd android && ./gradlew assembleDebug testDebugUnitTest)
boardview/tests/run.sh                                 # obv-dump tests
prek run --all-files                                   # all hooks
scripts/dev-monitor.sh                                 # MCP + monitor page with live reload
scripts/mcp-server.sh                                  # MCP server with the cached dev-shell env (for MCP clients)
scripts/mcp-server.sh --dev-reload                     # the same, and restart the server when its code changes
python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict   # phone contract
```

## Rules

- Follow the `jayson-code-conventions` skill and the language skill (`jayson-python-conventions` for Python).
- Kotlin: CameraX, coroutines, kotlinx.serialization, ktlint. Constants in one object, no magic values.
- Python: Python 3.14, uv, ruff (line length 120), pydantic v2 models, pytest.
- C++: only in `boardview/`. Do not edit the OpenBoardView sources. Put changes in `boardview/patches/`.
- Config through CLI flags and environment variables with defaults. `.env.example` lists every variable.
- Commits: Conventional Commits.
- When the debug-devices MCP server is connected, call `bench_instructions` first and follow it. It is the user's `instructions.md` (git-ignored; template `instructions.example.md`).
- Evidence: answer what is visible on the board from a fresh `phone_snapshot`, meter values only from `multimeter_read`, and label boardview data as supporting evidence. Quote a visible marking as seen, then use `board_match_marking` (see `EVIDENCE_RULES` in `mcp/debug_devices_mcp/instructions.py`).

## Safety

- Secrets live in `.env` only (`OPENROUTER_API_KEY`, `BOARDVIEW_*_KEY`). Never print or commit them.
- adb: use only the serial in `DEBUG_DEVICES_ADB_SERIAL`. Other Android devices (for example Fire TV devices) can be on the network. Never send them a command.
- Board files from repair sites are proprietary. Never copy them, or parts of them, into the repository, test fixtures, commit messages, or a web service or model. Local tests read the board in `BOARDVIEW_TARGET` and skip without it.
- Test fixtures come only from open sources, with a license note next to them. Hooks must not change them.
- Webcam frames can show people. Only the crop box goes to the vision model. Mask the area outside the crop in recordings.
- The monitor and the phone app listen on `127.0.0.1` only.
- The monitor page is the user's cockpit. Agents get device data only from the MCP tools. They never open, fetch, or drive the page or its HTTP API.
