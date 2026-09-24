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
| `monitor_open` | `open_browser` (default `true`) | `url` of the monitor page (the real port), `opened_browser`, `browser` |
| `bench_start` | `open_browser` (default `true`), `phone` (default `true`), `webcam` (default `true`), `board_path` (optional) | `url`, `opened_browser`, and `steps`: `page`, `webcam`, `phone`, `board`, each with `status` (`ok`, `error`, `skipped`) and `detail` |
| `bench_stop` | none | `url` and `steps`: `webcam`, `phone`, `page` |

Call `phone_connect` before the other `phone_*` tools. The HTTP contract with the app is in `docs/phone-api.md`.

`save_path` always gets the full-resolution JPEG. Only the image that goes to the model is scaled down.

`multimeter_read` sends one image to the OpenRouter model as a base64 data URL, with `detail: high`. With `source: webcam`, the image is one webcam frame with the webcam crop. With `source: phone`, the image is one phone snapshot, scaled to a long edge of 1568 px, with no crop. Call `phone_connect` first, and point the phone at the meter. The prompt tells the model to take the mode from the unit symbol and the annunciators on the LCD, not from the dial alone. If the model cannot read the unit symbol, it says so in `notes` and gives a confidence of 0.5 or lower. The server checks `OPENROUTER_API_KEY` before it opens the webcam or asks the phone. The request uses a strict `json_schema` response format and `provider.require_parameters: true`. The schema has no `$ref` or `$defs`, nullable fields use `type: [x, "null"]`, and every property is required. The request has no `temperature`, `top_p`, or `stop`. If the answer is not a valid reading, the server sends the request one more time.

If `--webcam-crop` is set, ffmpeg crops the frame on the PC. Only the cropped part goes to OpenRouter and to the model. The crop also applies to `webcam_snapshot`.

## Bench instructions

- `instructions.md` (git-ignored, template `instructions.example.md` at the repo root) is the user's text for the agent: device, board file, bench set-up, safety limits, workflow, and preferences.
- Server instructions (the `instructions` of the MCP initialize result): a fixed header ("The user started debug-devices: they want to debug hardware now. First call bench_instructions and follow it."), the tool guide, then the file content. The content is cut at 8 KiB (`MAX_SERVER_INSTRUCTIONS_BYTES`), and a note says so. The server reads the file once at start for this text.
- Tool `bench_instructions()`: the full content, the path, the modification time, and the size. It reads the file on each call. Without the file, it returns `exists: false` and `how_to_create`.
- The server treats the file as user instructions. It does not log the file, and it never sends it to OpenRouter.

## Evidence rules

The server instructions carry these rules (`EVIDENCE_RULES` in `instructions.py`), and the tool descriptions repeat them:

1. What is visible on the device or board: a fresh `phone_snapshot`. Not `webcam_snapshot`, `board_render`, or boardview data alone.
2. Meter values: only `multimeter_read`. The agent never reads a meter from an image itself.
3. Board questions (part position, net, test point): the board tools. Boardview data is supporting evidence. For the physical device, confirm with `phone_snapshot`.
4. Markings: quote the marking as seen, then `board_match_marking`. Say if it is an exact match or only candidates.

`multimeter_read` with `source: phone` sends the phone photo to the vision model. Use it only when the phone points at the meter, never at the board.

## Boardview tools

The board tools read boardview files with `obv-dump` (the OpenBoardView parsers as a command line tool, `boardview/`, `nix build .#obv-dump`, on `PATH` in the dev shell). The contract is `docs/boardview-json.md`. The server keeps the last opened board in memory. It caches each board by the SHA-256 of the file.

| Tool | Arguments | Result |
|---|---|---|
| `board_open` | `path` | Format, SHA-256, counts (parts, pins, nets, nails, test points), board size in mm, parts per side, load time |
| `board_find_part` | `query` (refdes, glob such as `C1*`, or mfgcode text), `limit` | Parts with side, center, box, rotation, mfgcode, pin count, and nets. `match`: `name_or_mfgcode`, `prefix` (no match, so the parts whose name starts with the query, with a `note`), or `none`. |
| `board_match_marking` | `marking` (as seen in the photo), `side`, optional `registration_id` + `x_px` + `y_px` | `visible_marking`, `resolution` (`exact`, `prefix_candidates`, `contains_candidates`, `confusion_candidates`, `none`), candidates (refdes, side, center, box, pin count, mfgcode, up to 5 nets, and with a position: photo position and distance), `best_candidate`, and a plain `message` |
| `board_part_pins` | `refdes` | All pins: number, name, net, position, side |
| `board_find_net` | `query` (net name or glob), `limit` | Per net: the parts and pin numbers, the test points (nails and `TP*` parts), and the nearest test point to each part |
| `board_parts_near` | `refdes` or `x_mm` + `y_mm`, `radius_mm` (default 5), `side`, `limit` | Parts within the radius, sorted by distance |
| `board_render` | `side`, `highlight_parts`, `highlight_nets`, `crop_to_part`, `max_side` (default 1568) | PNG of one side and a JSON legend (colors, pixel positions of the highlighted parts, notes) |
| `board_register_photo` | `side`, `photo_width_px`, `photo_height_px`, `pairs` (4 or more `{refdes, x_px, y_px}`) | A `registration_id` and the fit errors. The server refuses a fit with a large error. |
| `board_locate_in_photo` | `registration_id`, `refdes`, `net`, `photo_path` (optional), `max_side` | Pixel positions of the parts and net pins in the photo. With `photo_path`, also the photo with circles on them. |

Rules:

- Positions are in mm, in board coordinates (y up). `obv-dump` gives mil. The server converts them at one place (`board/units.py`).
- A part center is the center of the part box. When the file has no box (or a box with zero size), the box is the box around the pins plus 0.3 mm.
- `board_render` draws the bottom side mirrored in X, as seen from below. With `crop_to_part` and no `side`, it uses the side of that part.
- `board_match_marking` ignores case, spaces, dashes, and underscores. The order is: exact, then names that start with the marking, then names that contain it, then names that match when O/0, I/l/1, S/5, B/8, Z/2, G/6 are read wrong. With a photo position, `best_candidate` is set only when the nearest candidate is at least 2 times nearer than the next one (`BEST_DISTANCE_RATIO`). An exact match is always the best candidate.
- Photo mapping: a homography from 4 or more part centers. With 5 or more pairs, the server checks the error (limit: 2 % of the photo point spread). With 6 or more pairs, it also names the most likely wrong pair. Use large parts far apart, and a phone zoom of 1.5-2x (less lens distortion). Each side of the board needs its own registration.

Settings:

| Flag | Environment variable | Default |
|---|---|---|
| `--obv-dump-path` | `BOARDVIEW_DUMP_BIN` | `obv-dump` |
| `--boardview-dump-timeout` | `DEBUG_DEVICES_BOARDVIEW_DUMP_TIMEOUT` | `60` (seconds) |
| `--boardview-fz-key`, `--boardview-cae-key`, `--boardview-xzz-key` | `BOARDVIEW_FZ_KEY`, `BOARDVIEW_CAE_KEY`, `BOARDVIEW_XZZ_KEY` | none |

Put the keys in `.env`, not on the command line: a flag shows in the process list. The server gives the keys only to `obv-dump`. No error message and no log line contains them.

Tests: `mcp/tests/test_board*.py` use only open data (`mcp/tests/fixtures/boardview/`, see its README) and a fake `obv-dump`. `test_board_target.py` loads a real board with the real `obv-dump`. It runs only when `BOARDVIEW_TARGET` is set:

```sh
BOARDVIEW_TARGET=/path/to/board.cad uv run pytest mcp/tests/test_board_target.py -s
```

## Monitor window

The server has a local web page (the monitor). The page shows what the server does, and you can change the devices and the settings on it. With `--ui-open-browser`, the page opens in a new Firefox window. If Firefox does not start, the server uses `xdg-open`.

### Lazy start

With `--ui-start lazy` (the default), the server costs nothing until a tool needs hardware. MCP clients that start the server in every session (ChatGPT desktop, Codex, Claude Code) can keep it in their configuration.

- At process start: no port, no web server, no webcam, no ffmpeg, no adb, no scrcpy, no browser. The tool log is in memory from the start, so the page shows the early calls too.
- The first tool call (any tool) starts the page. With `--ui-open-browser`, Firefox opens then, one time.
- The first `webcam_snapshot`, the first `multimeter_read` with the webcam, a page that shows the live view, or another MCP process that asks for frames starts the webcam stream. If another monitor owns the webcam, the server uses its frames.
- After `--webcam-idle-timeout` (default 300 seconds) without frame users and page viewers, the stream stops, so other programs can use the camera. The next use starts it again.
- `phone_connect` starts adb, the phone screen, and scrcpy. `board_open` runs `obv-dump`.
- `monitor_open` starts the page and returns its URL. The port can differ from 18766 when that port is busy, so the agent tells you the URL.
- `bench_start` starts the page, the webcam, `phone_connect`, and `board_open` (with `board_path`) in one call. Each step runs even when another one fails. The result has the page URL and the status of each step. `bench_stop` stops the webcam stream, the phone screen and scrcpy, removes the adb forward of the phone camera, and stops the page. The tool log stays. You can say "start the bench" and "stop the bench" to the agent.
- The page has the buttons "Start all" (`bench_start` without a new browser window) and "Stop all" (`bench_stop`; the page then stops).

`--ui-start eager` starts the page and the webcam stream at process start, with no idle stop. `scripts/dev-monitor.sh` uses it.

The page has these parts:

- **Webcam**: the live view of the webcam. Drag on the image to draw the crop box. Drag the box to move it. Drag a corner to change its size. `webcam_snapshot` and `multimeter_read` send only the part in the box. The crop preview shows that part. The button "Clear crop" removes the box.
- **Phone**: the live phone screen, the serial, the screen state, the zoom, and the torch. The buttons run the same MCP tools as the agent: connect, refresh status, zoom in and out, zoom slider, torch, and snapshot. The page shows the last snapshot.
- **Read multimeter**: the button under the crop preview runs `multimeter_read`. The page shows the reading. The activity log shows the image that went to the model.
- **Settings**: the vision model and the webcam warm-up frames. An empty field uses the value from the CLI flag or the environment.
- **Full screen**: the live webcam view, the phone screen, and the last phone snapshot each have a full screen button (top right). A double-click on the view, or the key `f` on the focused view, also turns full screen on and off. `Esc` leaves full screen. The view fills the screen with its aspect ratio kept, on black. The phone screen keeps its rotation and shows a small bar with zoom out, zoom in, and torch. The snapshot loads the full-resolution image in full screen (`/api/phone/snapshot.jpg?full=true`); the panel keeps the scaled one. The webcam view shows the crop box in full screen, but you cannot change it there. A double-click on the webcam view does not change the crop box.
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
| `--instructions-file` | `DEBUG_DEVICES_INSTRUCTIONS` | `instructions.md` at the repo root. A missing file is not an error. |
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
| `--ui-start` | `DEBUG_DEVICES_UI_START` | `lazy`. `eager` starts the page and the webcam at process start. |
| `--webcam-idle-timeout` | `DEBUG_DEVICES_WEBCAM_IDLE_TIMEOUT` | `300`. Lazy mode: seconds without users before the webcam stream stops. `0` keeps it on. |
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
