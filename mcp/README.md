# debug-devices MCP server

A stdio MCP server for Claude Code. It controls the phone camera app through ADB and reads a multimeter through the PC webcam.

## Requirements

- Python 3.14 and `uv`.
- `adb` for the phone tools.
- `ffmpeg` with V4L2 input for the webcam tools.
- An OpenRouter API key for `multimeter_read`.
- For the monitor window: Firefox (or `xdg-open`) and `scrcpy`. Both are optional.

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
| `phone_rotation` | `degrees` (`0`, `90`, `180`, `270`) or `auto: true` | `CameraStatus`. `degrees` locks the snapshot rotation. `auto` follows the phone orientation again. |
| `phone_snapshot` | `save_path` (optional), `max_side` (default `1568`, `0` = full size) | JPEG image with the long edge at most `max_side`, and a JSON line with the sizes and the saved path |
| `webcam_snapshot` | `save_path` (optional), `max_side` (default `1568`, `0` = full size) | JPEG image with the long edge at most `max_side`, and a JSON line with the sizes and the saved path |
| `multimeter_read` | `include_image` (default `false`), `source` (`webcam` default, or `phone`) | `MultimeterReading`: `readable`, `value`, `unit`, `display_text`, `mode`, `range`, `flags`, `confidence`, `notes` |

Call `phone_connect` before the other `phone_*` tools. The HTTP contract with the app is in `docs/phone-api.md`.

`save_path` always gets the full-resolution JPEG. Only the image that goes to the model is scaled down.

`multimeter_read` sends one image to the OpenRouter model as a base64 data URL, with `detail: high`. With `source: webcam`, the image is one webcam frame with the webcam crop. With `source: phone`, the image is one phone snapshot, scaled to a long edge of 1568 px, with no crop. Call `phone_connect` first, and point the phone at the meter. The prompt tells the model to take the mode from the unit symbol and the annunciators on the LCD, not from the dial alone. If the model cannot read the unit symbol, it says so in `notes` and gives a confidence of 0.5 or lower. The server checks `OPENROUTER_API_KEY` before it opens the webcam or asks the phone. The request uses a strict `json_schema` response format and `provider.require_parameters: true`. The schema has no `$ref` or `$defs`, nullable fields use `type: [x, "null"]`, and every property is required. The request has no `temperature`, `top_p`, or `stop`. If the answer is not a valid reading, the server sends the request one more time.

If `--webcam-crop` is set, ffmpeg crops the frame on the PC. Only the cropped part goes to OpenRouter and to the model. The crop also applies to `webcam_snapshot`.

## Monitor window

When the server starts, it opens a local web page in a new Firefox window. If Firefox does not start, the server uses `xdg-open`. The page shows what the server does. You can also change the devices and the settings on the page.

The page has these parts:

- **Webcam**: the live view of the webcam. Drag on the image to draw the crop box. Drag the box to move it. Drag a corner to change its size. `webcam_snapshot` and `multimeter_read` send only the part in the box. The crop preview shows that part. The button "Clear crop" removes the box.
- **Phone**: the live phone screen, the serial, the screen state, the zoom, and the torch. The buttons run the same MCP tools as the agent: connect, refresh status, zoom in and out, zoom slider, torch, and snapshot. The page shows the last snapshot.
- **Read multimeter**: the button under the crop preview runs `multimeter_read`. The page shows the reading. The activity log shows the image that went to the model.
- **Settings**: the vision model and the webcam warm-up frames. An empty field uses the value from the CLI flag or the environment.
- **Activity**: each tool call, live. A row shows the time, the source (`mcp` for the agent, `ui` for the page), the tool, the arguments, the duration, the result, and a short summary. Open a row to see the images. For `multimeter_read`, the row shows the exact image that went to the model and the reading.

The server stores the page settings (crop, vision model, warm-up) in `$XDG_STATE_HOME/debug-devices/ui-settings.json`. The default path is `~/.local/state/debug-devices/ui-settings.json`. The saved values replace the CLI and environment values. "Clear crop" and "Reset to start values" go back to the CLI and environment values.

How it works:

- The page listens on `127.0.0.1` only. The server refuses requests for another host name and writes from another site. The page and the log never show the OpenRouter key.
- Only one process can read `/dev/video0`. While the page is on, the server keeps one `ffmpeg` process that reads the webcam at 10 frames per second. The webcam tools take the next frame from this stream, so they do not wait for a warm-up. A new warm-up value starts the stream again.
- After a successful `phone_connect`, the page shows the phone screen in real time:
  - The server pushes the scrcpy server of the installed scrcpy (`/usr/share/scrcpy/scrcpy-server`) to `/data/local/tmp/debug-devices-scrcpy-server.jar`. It starts the server with `adb shell app_process`. The server version comes from `scrcpy --version`, because the server refuses another version.
  - The server sends raw H.264 only (`raw_stream=true`, no audio, no control, `max_size=1280`, a key frame every 2 seconds) through `adb forward tcp:0 localabstract:scrcpy_<scid>`.
  - The page reads the frames from `/api/phone/screen` with `fetch`, decodes them with the WebCodecs `VideoDecoder`, and draws them on a canvas.
  - A page that opens or reloads gets the frames since the last key frame, so it shows the picture at once.
  - If the stream stops, the server starts it again after 3 seconds.
  - The app is locked to portrait. The buttons ⟲ and ⟳ turn the view on the page by 90°, and the canvas changes its shape, so nothing is cut off. "Auto" turns the view with the phone: after `phone_connect`, the monitor reads the camera status once per second (no tool call, no log row) and uses `rotation_degrees`. The page saves the choice (`screen_rotation` in `ui-settings.json`).
  - "Snapshot rotation" locks the rotation of the next phone snapshots to 0°, 90°, 180°, or 270°, or sets it back to Auto. It runs the `phone_rotation` tool.
- `--scrcpy-window` also opens a separate scrcpy window: `scrcpy -s <serial> --window-title "debug-devices: phone <serial>" --no-audio --stay-awake`. It is off by default, because the page shows the screen. If the window is already open, the server does not start a second one.
- The scrcpy log goes to `~/.local/state/debug-devices/scrcpy.log`.
- If `WAYLAND_DISPLAY` and `DISPLAY` are both empty and `$XDG_RUNTIME_DIR/wayland-0` exists, Firefox and scrcpy get `WAYLAND_DISPLAY=wayland-0`.
- When the MCP server stops, it stops the phone screen stream, scrcpy, and `ffmpeg`. The Firefox window stays open.

### More than one MCP server

Only one process can read the webcam. The first MCP server with the monitor owns it. Other MCP servers (another Claude Code session, or a `--no-ui` run) take their webcam frames from that monitor:

- At start, a server with the monitor asks `http://127.0.0.1:<ui port>/api/whoami`. If a debug-devices monitor of another process answers and streams the same webcam, the server does not start its own stream. It also does not open a browser window. Its own page uses a free port.
- If a webcam tool gets "Device or resource busy", the server asks the same address. If a debug-devices monitor answers, the server uses its frames.
- The frames come from `/api/webcam/frame.jpg?cropped=true`, with the crop of the owner.
- `/api/whoami` returns the app name `debug-devices-monitor` and the process id. The server never reads frames from another service on that port.
- If the owner stops, the next webcam call uses the local webcam again.

The tool calls of the other servers go into their own logs, not into the log of the owner.

Use `--no-ui` to turn off the page, the webcam stream, the phone screen, and scrcpy. Then each webcam tool call runs `ffmpeg` one time, with the warm-up frames and `--webcam-crop`. Use `--no-phone-screen` to keep the page without the phone screen. Use `--no-ui-open-browser` to keep the page without a new Firefox window. The server writes the page URL to stderr.

## Configuration

Each setting has a CLI flag, an environment variable, and a default. The server reads `.env` from the repo root. Durations are in seconds.

| Flag | Environment variable | Default |
|---|---|---|
| `--openrouter-api-key` | `OPENROUTER_API_KEY` | none |
| `--vision-model` | `DEBUG_DEVICES_VISION_MODEL` | `openai/gpt-6-luna` |
| `--meter-model` | `DEBUG_DEVICES_METER_MODEL` | empty. Make and model of the meter, for example `PROSTER T21D`. The prompt names it. |
| `--webcam` | `DEBUG_DEVICES_WEBCAM` | `/dev/video0` |
| `--webcam-crop` | `DEBUG_DEVICES_WEBCAM_CROP` | none. Format `x,y,w,h` in pixels, for example `640,0,640,540` |
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
| `--ui`, `--no-ui` | `DEBUG_DEVICES_UI` | on |
| `--ui-port` | `DEBUG_DEVICES_UI_PORT` | `18766`. If the port is busy, the server takes a free port. |
| `--ui-open-browser`, `--no-ui-open-browser` | `DEBUG_DEVICES_UI_OPEN_BROWSER` | on |
| `--phone-screen`, `--no-phone-screen` | `DEBUG_DEVICES_PHONE_SCREEN` | on |
| `--phone-screen-max-size` | `DEBUG_DEVICES_PHONE_SCREEN_MAX_SIZE` | `1280` |
| `--scrcpy-window`, `--no-scrcpy-window` | `DEBUG_DEVICES_SCRCPY_WINDOW` | off |
| `--scrcpy-path` | `DEBUG_DEVICES_SCRCPY_PATH` | `scrcpy` |
| `--scrcpy-server-path` | `DEBUG_DEVICES_SCRCPY_SERVER_PATH` | `/usr/share/scrcpy/scrcpy-server` |
| `--scrcpy-server-version` | `DEBUG_DEVICES_SCRCPY_SERVER_VERSION` | empty: from `scrcpy --version` |

Run `uv run debug-devices-mcp --help` to see all flags.

If you start the server by hand in a terminal, one Ctrl-C stops it. The MCP SDK reads stdin in a thread, and in a terminal that read does not end at Ctrl-C. Thus the server exits by force 2 seconds after its cleanup.

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

The tests do not need a phone, a webcam, or an API key. They use `httpx.MockTransport` for the phone and OpenRouter, and a fake command runner for `adb` and `ffmpeg`.
