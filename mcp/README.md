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
| `phone_status` | none | `CameraStatus`, and how far the phone is from the board: `distance_cm`, `detail_px_per_mm`, `min_distance_cm`, `focus_state`, `calibration`, `advice` (`too_close`, `good`, `far`, `unknown`), `advice_text` |
| `phone_zoom` | `ratio` or `step` (`in`, `out`) | `CameraStatus` |
| `phone_torch` | `enabled` | `CameraStatus` |
| `phone_rotation` | `degrees` (`0`, `90`, `180`, `270`) or `auto: true` | `CameraStatus`. `degrees` locks the snapshot rotation. `auto` follows the phone orientation again. |
| `phone_snapshot` | `save_path` (optional), `max_side` (default `1568`, `0` = full size) | JPEG image with the long edge at most `max_side`, in the chosen orientation, and a JSON line with the sizes, the saved path, and `orientation` |
| `phone_in_sensor_zoom` | `enabled` | The camera status with the distance values, and `in_sensor_zoom`: `on`, `off`, `unsupported` (no such mode on this phone), or `fallback` (the mode failed; the camera runs normally). Real extra detail at 2x-4x from a sensor crop (not optics); the preview stops about 1 s while the camera rebinds. The choice persists. |
| `phone_focus` | `x`, `y`, `source` (`snapshot` default, or `screen`) | Focus and meter the phone camera on one point. `snapshot`: `x`, `y` are pixels in the last `phone_snapshot` image that the agent got (after the flips and the scaling); the server maps them back to the true-orientation snapshot. `screen`: a point on the phone screen from 0 to 1. The result is the camera status with the distance values (`focus_state` goes `scanning`, then `focused` or `unfocused`). The lock holds 5 s. Take a fresh `phone_snapshot` after about 1 s to see the effect. An app without `POST /v1/focus` gives the error "update the phone app". |
| `phone_highlight` | `boxes` (up to 8 × `{x, y, width, height, label}`), `clear` | Draw green boxes around parts on the phone screen (over the camera preview, so the live view shows them) and on the page snapshot. The pixels refer to the last `phone_snapshot` image that the agent got; the server maps them through the flips and the scaling to the true-orientation snapshot (`POST /v1/overlay`). The result has the count and a copy of the last snapshot with the boxes, so the agent can check them. The boxes are estimates. `clear: true` removes them. |
| `phone_snapshot_orientation` | `flip_horizontal`, `flip_vertical` (optional; a missing value keeps that flip) | The flips of the phone snapshots: `flip_horizontal`, `flip_vertical`. No argument only reads them. |
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
| `board_render` | `side`, `highlight_parts`, `highlight_nets`, `crop_to_part`, `max_side` (default 1568), `green` | PNG of one side and a JSON legend (colors, pixel positions of the highlighted parts, notes). `green` draws every highlight in the green of the phone highlight boxes. |
| `board_register_photo` | `side`, `photo_width_px`, `photo_height_px`, `pairs` (4 or more `{refdes, x_px, y_px}`) | A `registration_id` and the fit errors. The server refuses a fit with a large error. |
| `board_locate_in_photo` | `registration_id`, `refdes`, `net`, `photo_path` (optional), `max_side`, `highlight` | Pixel positions of the parts and net pins in the photo. With `photo_path`, also the photo with circles on them. With `highlight: true`, also green boxes around the located parts (their boardview outline) through `phone_highlight`; the registered photo must be the last `phone_snapshot`. |

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

### Page reload after a server restart

`/api/state` and the first message of the event stream carry a code version: a hash of the page files and a new id for each server start. When an open page reconnects and gets another version (a restart by `scripts/dev-monitor.sh` or `--dev-reload`, or new page files), it reloads itself, so no tab runs old page code.

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

- **Phone** (the main panel: the larger left column on a wide window, the first panel on a narrow one): the live phone screen, as large as its column and the window height allow, with its aspect ratio. Next to it: the serial, the screen state, the zoom, the torch, and the controls. The buttons run the same MCP tools as the agent: connect, refresh status, zoom in and out, zoom slider, torch, and snapshot. The page shows the last snapshot.
- **Webcam** (the right column, above the settings): the live view of the webcam, only for the multimeter reading. Drag on the image to draw the crop box. Drag the box to move it. Drag a corner to change its size. `webcam_snapshot` and `multimeter_read` send only the part in the box. The button "Clear crop" removes the box. Each saved change of the box (at the end of a drag, and Clear crop) adds one `webcam_crop` row (source `ui`) to the activity log, with a small image of the new area.
- **Read multimeter**: the button under the webcam view runs `multimeter_read`. The page shows the reading. The activity log shows the image that went to the model.
- **Settings**: the vision model and the webcam warm-up frames. An empty field uses the value from the CLI flag or the environment.
- **Board**: open a boardview file by path (the panel also offers a board that another MCP server of this page opened), then search a part (for example `J4`) or a net (name or glob) with autocomplete over the part and net names. A part shows its side, position, pin count, and nets, and a board drawing with the part in green (`board_render` with `crop_to_part` and `green`). A net shows its parts, pins, and test points, and a drawing of the side with most of its parts. With a valid photo registration of the visible side, the search also runs `board_locate_in_photo` with `highlight: true`: the green box shows on the phone live view and on the page snapshot. Otherwise the panel says why: "register the photo first: 4 reference parts", "the board moved: register again" (a scene change made the registration stale), or "on the bottom side: not visible now". "Register photo": take a snapshot, type a reference part, press Pick, and click its center on the snapshot (panel or full screen; use the full-screen button or `f`, because a double-click also picks). With 4 or more parts, Register runs `board_register_photo` on the last snapshot and shows the fit error. All steps are tool calls with source `ui` in the log.
- **Highlights**: the page draws the agent's `phone_highlight` boxes (also `board_locate_in_photo` with `highlight: true`) with their labels on the snapshot, in the panel and in full screen. They follow the snapshot flips and the wheel zoom and pan. The button "Clear highlights" (in the panel and in the full-screen snapshot bar) runs `phone_highlight` with `clear: true`. The log row of `phone_highlight` shows the annotated image. The phone draws the same boxes itself, so the live view shows them.
- **Scene change**: while a phone session runs, the server compares about 2 small grayscale frames per second of the phone screen (the camera preview area, blurred) with the frame at the last `phone_snapshot`. A large mean difference or a shift of the whole picture, in 2 frames in a row, means that the board or the phone moved. The server then removes the highlight boxes (phone and page), marks the photo registrations as stale, and `phone_status` returns `scene_changed: true`. `phone_highlight`, `phone_focus` with snapshot pixels, `board_register_photo`, and `board_locate_in_photo` refuse until a fresh `phone_snapshot` (a stale registration needs a new `board_register_photo`). The page shows "Scene changed: highlights cleared" for 4 s. Our own commands (zoom, torch, rotation, flips, sensor zoom, focus, highlight boxes) take a new reference after 1.5 s, so they do not count as a move. The watcher uses the page's MJPEG fallback decoder when it runs, else its own small ffmpeg (2 fps, 160 px wide). Without the page server (`--no-ui`) or the phone screen, there is no detection.
- **Click to focus**: one click on the live phone view (in the panel or in full screen, H.264 or the MJPEG fallback) runs `phone_focus` with `source` `screen` on that point. The page first undoes the letterbox of the full-screen view and the Screen view rotation (⟲, ⟳, Auto), so the point is on the phone screen as the stream shows it. The click waits 250 ms: a double-click (full screen) cancels it. A yellow ring fades at the click point in about 1 s, and a label shows "focusing…", then the focus state from the status ("focused" or "could not focus"), or the error (for example "outside the preview" for a click on the app buttons). A click on the black letterbox does nothing.
- **Sensor zoom**: the toggle "Sensor zoom (2x-4x detail)" in the phone panel and "Sensor zoom" in the full-screen live bar run `phone_in_sensor_zoom`. The panel shows the state of the phone (on, off, not on this phone, failed; normal camera) and, when it is on and the zoom is 2x-4x, the hint "real sensor detail at this zoom (not optics)". The distance line then says "+ sensor zoom" after the px/mm estimate: the real detail is higher, but the server does not make up a number. The choice persists in `ui-settings.json` (`in_sensor_zoom`). The server sends it again after `phone_connect` (also `bench_start`) and after an app restart (the status shows off while the choice is on, or the reverse). It never sends again for `unsupported` or `fallback`, because each request stops the preview for about 1 s. An app without `POST /v1/camera` gives the error "update the phone app".
- **Phone distance**: the phone panel and the full-screen live view bar show how far the phone is from the board and how much detail that gives, for example "≈ 29 cm · ~9 px/mm · focused". The server computes it from the camera status (`focus`, `optics` in `docs/phone-api.md`): distance = 100 / diopters, detail = `output_width_px × focal_length_mm / (sensor_width_mm × distance_mm)` (a thin-lens estimate; zoom does not change it). "≈" means an approximate lens calibration; an uncalibrated lens shows no cm. The color and the advice line follow the advice: at 20 px/mm or more the detail is good; below it, the advice says how close to move ("move closer: about 11 cm gives about 25 px/mm", 1.1 × the closest focus distance); nearer than the closest focus distance, it says to move back. `phone_status` and `bench_start` give the same values to the agent. The values follow the 1 s status poll.
- **Snapshot flips**: the buttons "Flip H" (left-right) and "Flip V" (upside down), in the phone panel and in the full-screen snapshot bar. The keys `h` and `v` work when the snapshot view has the focus. The server applies the flips (`phone_snapshot_orientation`), so the agent sees the same photo as you: the `phone_snapshot` result and its `save_path` file, `multimeter_read` with the phone, the tool log, and the page snapshot. The `phone_snapshot` text says the orientation, and pixel positions (`board_register_photo`, `board_match_marking`) refer to the oriented photo. The phone mirrors its camera preview the same way (`POST /v1/preview`): only the camera image, so the app's status text stays readable, and the live view on the page shows the phone screen as it is (no CSS flip). The server sends the flips when they change, after `phone_connect` (also through `bench_start`), and when a status read shows other preview flips (an app restart). The phone panel shows the preview flips from the camera status. An app from before `/v1/preview` answers 404: the server logs one warning per `phone_connect` and keeps flipping the snapshots. The choice persists in `ui-settings.json` (`snapshot_orientation`), also with `--no-ui`. The phone camera itself does not change.
- **Full screen**: the live webcam view, the phone screen, and the last phone snapshot each have a full screen button (top right). A double-click on the view, or the key `f` on the focused view, also turns full screen on and off. `Esc` leaves full screen. The view fills the screen with its aspect ratio kept, on black. The phone screen keeps its rotation and shows a small bar with zoom out, zoom in, torch, and the zoom ratio. In the full-screen phone screen, the mouse wheel and the arrow keys zoom the phone camera (`phone_zoom`, shown in the log): wheel up, `ArrowUp`, and `ArrowRight` zoom in; wheel down, `ArrowDown`, and `ArrowLeft` zoom out. There is at most one step per 150 ms, and extra events during a step are dropped, so a fast scroll does not queue steps. Outside this view, the wheel and the arrow keys work as usual. The snapshot loads the full-resolution image in full screen (`/api/phone/snapshot.jpg?full=true`); the panel keeps the scaled one. In the full-screen snapshot, the mouse wheel zooms the photo around the mouse pointer (up zooms in, down zooms out, 1x to 8x, in steps of 1.25). This is a digital zoom of the still image, not the phone camera zoom. When zoomed in, drag with the left mouse button to move the photo. The key `0` resets the zoom. The full-screen snapshot bar also has the phone camera zoom `−` / `+` with the zoom ratio (the next snapshot shows it; the same rate limit as the live view), the Sensor zoom toggle (the same choice as the other two toggles), and "New snapshot" (also the key `s`): the new photo loads at full size, and the digital zoom resets. The zoom also resets when you leave full screen or when a new snapshot comes. A small label at the bottom left shows the zoom factor. Everywhere else, the mouse wheel scrolls the page as usual. The webcam view shows the crop box in full screen, but you cannot change it there. A double-click on the webcam view does not change the crop box. Microscope use: a double-click on the live phone screen puts only that view in full screen. The rest of the page stays as it is. This does not need a snapshot or the Take snapshot button.
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
  - Some browsers have WebCodecs without H.264 (for example Camoufox). Before the first frame, the page asks `VideoDecoder.isConfigSupported()` with the codec of the stream. When the browser cannot decode it (or a decoder error comes later), the page uses the MJPEG fallback `/api/phone/screen.mjpg`. The server then runs one ffmpeg that turns the H.264 stream into JPEG frames (10 fps, at most 1280 px wide), only while a fallback page watches. The page draws the frames into the same view, so rotation, full screen, the zoom keys, and the Sensor zoom button work the same way. The view then shows the hint "No H.264 decoder in this browser: MJPEG fallback". H.264 stays the first choice: it is smoother.
  - Camoufox: Camoufox 152 finds no H.264 decoder when the system has only FFmpeg with libavcodec 63. With the FFmpeg 7 libraries (`libavcodec.so.61`) on `LD_LIBRARY_PATH`, it decodes H.264 in WebCodecs (also MSE and MP4), and the page uses the smooth H.264 path. Start Camoufox with that folder first on the path, for example `LD_LIBRARY_PATH=$HOME/.cache/debug-devices/ffmpeg7-lib-lib/lib camoufox`.
  - The app is locked to portrait. The buttons ⟲ and ⟳ turn the view on the page by 90°, and the canvas changes its shape, so nothing is cut off. "Auto" turns the view with the phone: after `phone_connect`, the monitor reads the camera status once per second (no tool call, no log row) and uses `rotation_degrees`. The page saves the choice (`screen_rotation` in `ui-settings.json`).
  - "Snapshot rotation" locks the rotation of the next phone snapshots to 0°, 90°, 180°, or 270°, or sets it back to Auto. It runs the `phone_rotation` tool.
- `--scrcpy-window` also opens a separate scrcpy window: `scrcpy -s <serial> --window-title "debug-devices: phone <serial>" --no-audio --stay-awake`. It is off by default, because the page shows the screen. If the window is already open, the server does not start a second one.
- The scrcpy log goes to `~/.local/state/debug-devices/scrcpy.log`.
- If `WAYLAND_DISPLAY` and `DISPLAY` are both empty and `$XDG_RUNTIME_DIR/wayland-0` exists, Firefox and scrcpy get `WAYLAND_DISPLAY=wayland-0`.
- When the MCP server stops, it stops the phone screen stream, scrcpy, and `ffmpeg`. The Firefox window stays open.

### More than one MCP server

Only one process can read the webcam. The first MCP server with the monitor owns it. Other MCP servers (another Claude Code session, or a `--no-ui` run) take their webcam frames from that monitor:

- When a server needs the webcam, it asks `http://127.0.0.1:<ui port>/api/whoami`. If a debug-devices monitor of another process answers and streams the same webcam, the server does not start its own stream.
- If a webcam tool gets "Device or resource busy", the server asks the same address. If a debug-devices monitor answers, the server uses its frames.
- The frames come from `/api/webcam/frame.jpg?cropped=true`, with the crop of the owner.
- `/api/whoami` returns the app name `debug-devices-monitor` and the process id. The server never reads frames from another service on that port.
- If the owner stops, the next webcam call uses the local webcam again.

One page shows the tool calls of every server:

- The primary is the server with the page on the configured UI port (`--ui-port`, default 18766), often `scripts/dev-monitor.sh`. A server that finds the primary through `/api/whoami` (same app name, another process) is a secondary. It serves no page and opens no browser window. `monitor_open` and `bench_start` give the primary URL.
- A secondary sends each tool call (start and end: tool, arguments, status, duration, short result) and its images (the images that the tool took or returned) to the primary: `POST /api/ingest/calls` and `POST /api/ingest/calls/{id}/images`. It sends them in order, with a timeout of 1 s, from a background worker, so a slow or stopped primary never slows a tool call. It keeps its own log too.
- The primary shows these calls in the same activity log, with the sender as a label, for example "mcp · codex 1824788" (the MCP client name from `initialize` and the process id). It keeps the recent 200 calls and the images of the recent 30 calls for each sender.
- The ingest routes need a secret token. The primary writes a new random token for its port to `$XDG_RUNTIME_DIR/debug-devices/ingest-<port>.token` (mode 600; the state folder when `XDG_RUNTIME_DIR` is not set), and a secondary sends it in the header `X-Debug-Devices-Token`. The page listens on 127.0.0.1 only, and the host and origin checks stay.
- The result of `bench_instructions` (your instructions text) does not go to the primary: its row shows only the tool name and the status. The OpenRouter key is never in an event.
- When the primary restarts (a dev monitor reload), a secondary waits up to 3 s for it, finds it again, and reads its new token. When no primary answers, the next tool call starts the page of this server on the port, and this server becomes the primary.

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

### Reload proxy (`debug-devices-mcp-dev`)

`debug-devices-mcp-dev [server arguments...]` (module `devreload.py`, started by `scripts/mcp-server.sh --dev-reload`) runs `python -m debug_devices_mcp` with the same arguments as a child, and forwards the newline-delimited JSON-RPC of the stdio transport in both directions. Stdout carries only JSON-RPC. The proxy and the child write their logs to stderr.

- Watch: the proxy polls the modification times of `.py`, `.html`, `.js`, and `.css` files under `mcp/debug_devices_mcp/` every second (`poll_interval`). After a change, it waits 300 ms for more changes (`debounce`).
- Initialize: it keeps the client's `initialize` request and `notifications/initialized`. In the `initialize` answer to the client, it sets `capabilities.tools.listChanged` to `true`.
- Reload: it closes the child's stdin, so the server stops the webcam stream, scrcpy, and the adb forward. After 5 s (`stop_timeout`) it sends SIGTERM, then SIGKILL, to the child's process group. The server has no SIGTERM handler, so only the stdin close gives a clean stop. Then it starts a new child, sends it the kept `initialize` (the proxy takes the answer) and `initialized`, and sends `notifications/tools/list_changed` to the client. The log line is `[devreload] reloaded (N files changed)`.
- Messages from the client during a reload wait in a queue and go to the new child. Requests that the old child did not answer get the JSON-RPC error -32001 "debug-devices reloaded its code; call the tool again".
- If the new child exits or does not answer `initialize` (30 s), requests get the error -32002 with the problem. The proxy tries again at the next change. The same happens when the server stops by itself.
- Client EOF: the proxy stops the child and exits.

The tests do not need a phone, a webcam, or an API key. They use `httpx.MockTransport` for the phone and OpenRouter, and a fake command runner for `adb` and `ffmpeg`.
