# debug-devices MCP server

A stdio MCP server for Claude Code. It controls the phone camera app through ADB and reads a multimeter through the PC webcam.

## Requirements

- Python 3.14 and `uv`.
- `adb` for the phone tools.
- `ffmpeg` with V4L2 input for the webcam tools.
- An OpenRouter API key for `multimeter_read`.

## Set up

1. Copy `.env.example` to `.env` at the repo root.
2. Set `OPENROUTER_API_KEY` in `.env`.
3. If more than one ADB device is connected, set `DEBUG_DEVICES_ADB_SERIAL` to the serial of the phone.
4. Run `uv sync`.

## Add the server to Claude Code

Run this command. Replace `<repo>` with the absolute path of this repository.

```sh
claude mcp add debug-devices -- uv run --directory <repo> debug-devices-mcp
```

Alternative: copy `.mcp.json.example` to `.mcp.json` at the repo root. Then start Claude Code in the repo root.

## Tools

| Tool | Arguments | Result |
|---|---|---|
| `phone_connect` | none | Selects the ADB device, runs `adb forward tcp:<local> tcp:8765`, and starts the app if `/v1/health` does not answer. Returns the health and the camera status. |
| `phone_status` | none | `CameraStatus` |
| `phone_zoom` | `ratio` or `step` (`in`, `out`) | `CameraStatus` |
| `phone_torch` | `enabled` | `CameraStatus` |
| `phone_snapshot` | `save_path` (optional) | JPEG image and a JSON line with the size and the saved path |
| `webcam_snapshot` | `save_path` (optional) | JPEG image and a JSON line with the size and the saved path |
| `multimeter_read` | `include_image` (default `false`) | `MultimeterReading`: `readable`, `value`, `unit`, `display_text`, `mode`, `range`, `flags`, `confidence`, `notes` |

Call `phone_connect` before the other `phone_*` tools. The HTTP contract with the app is in `docs/phone-api.md`.

`multimeter_read` sends one webcam frame to the OpenRouter model as a base64 data URL. The request uses a strict `json_schema` response format. If the answer is not a valid reading, the server sends the request one more time.

## Configuration

Each setting has a CLI flag, an environment variable, and a default. The server reads `.env` from the repo root. Durations are in seconds.

| Flag | Environment variable | Default |
|---|---|---|
| `--openrouter-api-key` | `OPENROUTER_API_KEY` | none |
| `--vision-model` | `DEBUG_DEVICES_VISION_MODEL` | `openai/gpt-6-luna` |
| `--webcam` | `DEBUG_DEVICES_WEBCAM` | `/dev/video0` |
| `--webcam-warmup-frames` | `DEBUG_DEVICES_WEBCAM_WARMUP_FRAMES` | `10` |
| `--adb-serial` | `DEBUG_DEVICES_ADB_SERIAL` | empty: the only connected device |
| `--local-forward-port` | `DEBUG_DEVICES_LOCAL_FORWARD_PORT` | `18765` |
| `--adb-path`, `--ffmpeg-path` | `DEBUG_DEVICES_ADB_PATH`, `DEBUG_DEVICES_FFMPEG_PATH` | `adb`, `ffmpeg` |
| `--phone-http-timeout` | `DEBUG_DEVICES_PHONE_HTTP_TIMEOUT` | `10` |
| `--phone-snapshot-timeout` | `DEBUG_DEVICES_PHONE_SNAPSHOT_TIMEOUT` | `30` |
| `--app-start-timeout` | `DEBUG_DEVICES_APP_START_TIMEOUT` | `20` |
| `--adb-timeout` | `DEBUG_DEVICES_ADB_TIMEOUT` | `15` |
| `--webcam-timeout` | `DEBUG_DEVICES_WEBCAM_TIMEOUT` | `20` |
| `--vision-timeout` | `DEBUG_DEVICES_VISION_TIMEOUT` | `90` |

Run `uv run debug-devices-mcp --help` to see all flags.

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

The tests do not need a phone, a webcam, or an API key. They use `httpx.MockTransport` for the phone and OpenRouter, and a fake command runner for `adb` and `ffmpeg`.
