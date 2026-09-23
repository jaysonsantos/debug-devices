# Report: dd-mcp

## What I did

- Added the Python project at the repo root: `pyproject.toml` and `uv.lock`. The build backend is `uv_build` with the module root `mcp/`. The entry point is `debug-devices-mcp`.
- Wrote the MCP server in `mcp/debug_devices_mcp/`:
  - `constants.py`: all names, defaults, and limits.
  - `config.py`: `Settings` with pydantic-settings. Each setting is a `--kebab-case` flag, a `DEBUG_DEVICES_*` variable, and a default. The server reads `.env` from the repo root. Durations are `timedelta`, given in seconds.
  - `phone_api.py`: a typed httpx client for `docs/phone-api.md`, with the models `Health`, `CameraStatus`, `ApiError`, and the request bodies.
  - `adb.py`: device selection, `adb forward`, and `am start`.
  - `webcam.py`: one JPEG frame through ffmpeg. The first 10 frames are skipped (warm-up).
  - `multimeter.py`: the OpenRouter request (image as a base64 data URL, strict `json_schema` response format), the `MultimeterReading` model, and one retry on an invalid answer.
  - `server.py`: the tools `phone_connect`, `phone_status`, `phone_zoom`, `phone_torch`, `phone_snapshot`, `webcam_snapshot`, and `multimeter_read`.
- Wrote 36 unit tests in `mcp/tests/`. They use `httpx.MockTransport` for the phone and OpenRouter, and a fake command runner for `adb` and `ffmpeg`. The server tests use the in-process `mcp.Client`.
- Wrote `.mcp.json.example` and `mcp/README.md`.

## What works

- `uv run pytest`: 36 passed.
- `uv run ruff check` and `uv run ruff format --check`: pass (this also covers `scripts/`).
- Stdio smoke test with `mcp.Client` and `StdioServerParameters` (`uv run --directory <repo> debug-devices-mcp`):
  - `tools/list` returns the 7 tools.
  - `webcam_snapshot` returns a real 1920x1080 JPEG from `/dev/video0` (about 70 KB).
  - `phone_connect` returns a tool error. The error lists the two Fire TV devices and asks for `--adb-serial`. The server ran only `adb devices -l`.
  - `multimeter_read` returns a tool error that names `OPENROUTER_API_KEY`. No network call.
- Real phone test before the app install, with `--adb-serial 7fad170e`: `phone_connect` returned a clear error: "the camera app dev.jayson.debugdevices.camera is not installed on 7fad170e. Build and install android/ first."
- Real phone test after the app install. Driver: `mcp.Client` over stdio, `debug-devices-mcp --adb-serial 7fad170e`. Only `7fad170e` got commands. dd-qa used the phone at the same time, so each result shows the state at that moment only.

  | Call | Result | Time |
  |---|---|---|
  | `phone_connect` | OK. `started_app: false`, app `0.1.0`, zoom 1.0, range 1.0 to 10.0, flash unit present | 194 ms |
  | `phone_status` | OK. zoom 1.0, torch off | 28 ms |
  | `phone_zoom step=in` | OK. zoom 1.5 | 320 ms |
  | `phone_zoom step=out` | OK. zoom 1.0 | 283 ms |
  | `phone_zoom ratio=2.0` | OK. zoom 2.0 | 264 ms |
  | `phone_zoom ratio=1000` | OK. clamped to 10.0 | 270 ms |
  | `phone_zoom ratio=0.01` | OK. clamped to 1.0 | 286 ms |
  | `phone_zoom` with no argument | Tool error: "give exactly one of `ratio` or `step`" | 4 ms |
  | `phone_torch enabled=true` | OK. `torch_enabled: true` | 360 ms |
  | `phone_torch enabled=false` | OK. `torch_enabled: false` | 293 ms |
  | `phone_snapshot save_path=...` | OK. Image content `image/jpeg`, 2,009,862 bytes, 3060x4080, EXIF model `2510ERA8BG`. The saved file is a correct back camera photo. | 1127 ms |

- Live multimeter test (one call, as the orchestrator allowed). Settings from `.env`: model `openai/gpt-6-luna`. The key did not go into any output.
  - `webcam_snapshot`: OK, 59,360 bytes. The webcam points at the ceiling. There is no multimeter in the frame.
  - `multimeter_read include_image=true`: tool error after 5.5 s. OpenRouter returned 400 from the provider (`Azure`): "Invalid schema for response_format 'multimeter_reading': context=('properties', 'mode'), $ref cannot have keywords {'description'}." The server did not crash.

## Decisions

- The installed SDK is `mcp` 2.2.0. In 2.x, `FastMCP` has the name `MCPServer` (`mcp.server.mcpserver`). The server uses `MCPServer`.
- Expected failures become `ToolError`. The model reads the message, and the server does not crash.
- `phone_connect` polls `/v1/health` and `/v1/status` until the camera is ready, or until `--app-start-timeout` (20 s) ends. It retries on `camera_not_ready`.
- The other phone tools do not forward the port. If the phone does not answer, the error tells the caller to run `phone_connect`.
- `multimeter_read` returns structured content (the `MultimeterReading` output schema) and a JSON text block. With `include_image`, it also returns the frame.
- The OpenRouter request sends the `X-Title` header. It does not send `HTTP-Referer`, because the repository URL is not known.

## Changes after the orchestrator update

- The default vision model is now `openai/gpt-6-luna`. I changed `constants.py`, `mcp/README.md`, and the tests.
- `adb.start_app` now raises `AppNotInstalledError` when the activity does not exist. This occurs with exit code 0 or 1.

## Bugs found in the real tests, and fixes

- **Strict schema refused (fixed).** Cause: pydantic writes the `mode` field as `{"$ref": "#/$defs/MeterMode", "description": ...}`. OpenAI strict mode refuses keywords next to `$ref`. Fix: `reading_json_schema()` in `multimeter.py` now puts each `$defs` entry in place of its `$ref` and removes `$defs`. The new test `test_schema_has_no_refs_for_strict_mode` checks this. The fix has no live test yet, because I used the one allowed call.
- **Noisy log (fixed).** The server wrote one `HTTP Request: ...` INFO line to stderr for each phone request. `__main__.py` now sets the `httpx` and `httpcore` loggers to WARNING. Stdout was not affected.

## Open items

- Run one more live `multimeter_read` to check the schema fix. Point the webcam at a multimeter first. The orchestrator must allow this call.
- The phone snapshot is about 2 MB (2.7 MB as base64 in the MCP image content). This is below the 5 MB image limit of the Claude API. A phone with a larger sensor can go above the limit. If that occurs, add a `max_size` argument that scales the JPEG down before the server returns it.
- `docs/research.md` has no MCP SDK or OpenRouter section yet. I used the installed package source for the SDK facts.
- Proposal for `flake.nix` (owner: dd-research or the orchestrator): no change is necessary. The shell has `uv`, `ruff`, `ffmpeg`, and `adb`.
- Proposal for `.env.example` (not my path): put `7fad170e` in the comment of `DEBUG_DEVICES_ADB_SERIAL` as an example. Also add the optional variables `DEBUG_DEVICES_LOCAL_FORWARD_PORT`, `DEBUG_DEVICES_ADB_PATH`, and `DEBUG_DEVICES_FFMPEG_PATH`, each with a comment.
- No change to `docs/phone-api.md` is necessary.
