# Report: dd-ui

## What I did

- Added the monitor window. It is a local web page that the MCP server serves on `127.0.0.1` (Starlette and uvicorn, no new dependencies, no build step).
  - `mcp/debug_devices_mcp/ui/events.py`: the event bus. `EventBus.record()` is an async context manager for one tool call. `current_call()` gives the running call, so tool code can add images and details. Subscriber queues feed Server-Sent Events.
  - `mcp/debug_devices_mcp/ui/settings.py`: `UiSettings` (pydantic) in `$XDG_STATE_HOME/debug-devices/ui-settings.json`. The file write is atomic. A broken file gives empty settings.
  - `mcp/debug_devices_mcp/ui/monitor.py`: `Monitor`. `instrument(server)` wraps `MCPServer.call_tool`, so each tool call goes into the log with no change to the tool code. `RecordingFrameSource` adds the frame that a tool takes to the call. For `multimeter_read`, this is the exact image that goes to the model. After a successful `phone_connect`, the monitor starts scrcpy.
  - `mcp/debug_devices_mcp/ui/app.py` and `ui/routes/`: the web API, one resource per module (state and SSE, settings, webcam, phone, call images). The `LocalOnly` middleware refuses other host names (DNS rebinding) and writes from other origins (CSRF).
  - `mcp/debug_devices_mcp/ui/static/`: `index.html`, `app.js`, `style.css`. The page has a live webcam view with a crop box that you can draw, move, and resize. It also has a crop preview, the phone panel, the settings, and the activity log. The page supports light and dark mode.
  - `mcp/debug_devices_mcp/ui/desktop.py`: `child_env()` (Wayland fallback) and `BrowserOpener` (`firefox --new-window`, then `xdg-open`). The output of a child process never goes to stdout.
  - `mcp/debug_devices_mcp/ui/setup.py`: `build_monitor(settings, services)`.
- `mcp/debug_devices_mcp/webcam_stream.py`: `FrameSource` protocol, `WebcamStream` (one long-running `ffmpeg ... -f mpjpeg pipe:1`, parsed by `Content-length`), crop math (`clamp_crop`, `crop_jpeg`). ffmpeg restarts after a failure. The error message goes to the tool error and to the page.
- `mcp/debug_devices_mcp/scrcpy.py`: `ScrcpyLauncher`. It keeps one process per serial. It starts the process again after the window closes, and it stops the process at exit.
- Integration, after dd-mcp was done:
  - `config.py`: `--ui/--no-ui`, `--ui-port` (default `18766`), `--ui-open-browser/--no-ui-open-browser`, `--scrcpy/--no-scrcpy`, `--scrcpy-path`.
  - `server.py`: `Services.webcam` is now a `FrameSource`. `build_server(services, monitor=None)` instruments the server, and the lifespan starts and stops the monitor.
  - `multimeter.py`: a `VisionClient.model` setter, so the page can change the model at run time.
  - `__main__.py`: builds the monitor when `--ui` is on.
- Tests: `test_ui_events.py`, `test_ui_settings.py`, `test_scrcpy.py`, `test_webcam_stream.py`, `test_ui_app.py` (Starlette `TestClient` with the real tools, the fake phone, and the fake webcam).
- `mcp/README.md`: new section "Monitor window", new rows in the configuration table.

## What works

- `uv run pytest`: 84 passed. `uv run ruff check mcp/ scripts/` and `uv run ruff format --check mcp/ scripts/`: pass.
- `uv build --wheel`: the wheel contains `ui/static/*`.
- Real stdio run with `mcp.ClientSession` and `stdio_client`: `uv run --directory <repo> debug-devices-mcp --adb-serial 7fad170e`. `WAYLAND_DISPLAY` and `DISPLAY` were removed from the environment.
  - The server wrote `monitor window: http://127.0.0.1:18766/`. Firefox opened the page in a new window.
  - `phone_connect`: OK (101 ms). scrcpy started with the title `debug-devices: phone 7fad170e`.
  - `phone_status`: OK (19 ms). `webcam_snapshot`: OK (2955 ms for the first frame after the stream start, 1920x1080).
  - `curl -sN http://127.0.0.1:18766/api/events` showed the `phone` and `call` events. `/api/state` listed all calls with source, status, duration, and the webcam frame image. The state did not contain the key.
  - I took a screenshot with `spectacle -b -n -o <file>` (it needs `WAYLAND_DISPLAY=wayland-0`) and looked at it. It showed the live webcam, the phone panel (serial `7fad170e`, scrcpy "window open", zoom 1.00x (1–10)), and the settings. The KDE task bar showed the scrcpy window.
  - After the client closed, no `scrcpy` or `ffmpeg` process was left.
- Crop check (separate state dir, `--no-ui-open-browser --no-scrcpy --ui-port 18799`): `PUT /api/settings` with crop `600,300,400,200`. Then `webcam_snapshot` returned 400x200, and the log image was the same 10,912 bytes. The settings file contained the crop.

## Bug found and fixed

- The "Waiting for the first frame" text stayed on the webcam view, because `display: grid` overrides the `hidden` attribute. Fix: a global `[hidden] { display: none !important; }` rule. I did not take a new screenshot after the fix.

## Decisions

- A saved page value replaces the CLI and environment value. "Clear crop" and "Reset to start values" go back to the CLI and environment values.
- With `--no-ui`, the server does not read the page settings file. The old one-shot ffmpeg path, `--webcam-crop`, and `--webcam-warmup-frames` apply.
- The stream uses the default V4L2 format of the device, the same as the one-shot path. Thus a pixel crop is the same in both modes. It re-encodes to MJPEG at 10 fps with `-q:v 2`, about 360 KB per 1080p frame.
- The warm-up setting skips frames at each stream start. A new value starts the stream again.
- The page buttons run the MCP tools through the instrumented `call_tool`. Thus the log shows them with source `ui`, and the phone logic stays in one place.
- The recent 200 calls stay in memory. Only the recent 30 calls keep their images.

## Open items

- **dd-qa**: `scripts/qa_mcp_stdio.py` starts the server with the default settings. Now each run opens a Firefox window, reads `/dev/video0` through the stream, and can start scrcpy. Add `--no-ui` to its server arguments (or `--no-ui-open-browser --no-scrcpy` to test the page too). I did not change that file, because it is not mine.
- A new MCP server start opens a new Firefox window each time. Use `--no-ui-open-browser` (or `DEBUG_DEVICES_UI_OPEN_BROWSER=false`) if this is too many windows.
- The server does not close the Firefox window at exit. It cannot know which window belongs to it.
- If the MCP process gets SIGKILL, scrcpy and ffmpeg stay alive. A normal exit (stdin closed or SIGTERM) stops them.
- `pyproject.toml` does not list `starlette` and `uvicorn`, because `mcp` brings them. The orchestrator can add them as direct dependencies.
- `.env.example` (not mine): add `DEBUG_DEVICES_UI`, `DEBUG_DEVICES_UI_PORT`, `DEBUG_DEVICES_UI_OPEN_BROWSER`, `DEBUG_DEVICES_SCRCPY`, and `DEBUG_DEVICES_SCRCPY_PATH` with comments.
- No change to `docs/phone-api.md` is necessary.

## Round 2: webcam sharing and the "Read multimeter" button

### What I did

- `mcp/debug_devices_mcp/remote_webcam.py` (new):
  - `RemoteMonitor` is a client for the monitor of another process.
  - `identify()` accepts only a monitor with `app == "debug-devices-monitor"`, another process id, the same webcam device, and a running stream.
  - `SharedWebcam` is a `FrameSource`. It uses the local webcam first. On "Device or resource busy", it uses the frames of the other monitor. If that monitor stops answering, it goes back to the local webcam.
- New route `/api/whoami`: app name, process id, URL, webcam device, stream state, crop.
- `/api/webcam/frame.jpg?cropped=true` returns the next frame with the crop of this monitor.
- If a monitor already owns the webcam at start, the new monitor does not start its stream and does not open a browser. `/api/webcam/info` names the owner.
- `WebcamStream.next_frame()` now fails at once when ffmpeg stops. Before, it waited for the full timeout (20 s). A busy webcam now gives the error fast.
- New route `POST /api/multimeter/read` and a "Read multimeter" button under the crop preview. The route runs `multimeter_read` through the instrumented `call_tool` with source `ui`. The log shows the sent image and the reading.
- `Monitor.call_from_ui()` now returns the recorded call and the result. The recording code is in one place (`_record_call`).
- `server.py` (small change): `Services.from_settings` wraps the one-shot `Webcam` in `SharedWebcam`. This covers `--no-ui`.
- `ui/setup.py`: the monitor uses `SharedWebcam` around its stream.
- Tests: `test_remote_webcam.py` (13 tests). New tests in `test_ui_app.py`: whoami, cropped frame, owner, the button, the button error, and monitor start as owner and as second process.

### What works

- `uv run pytest`: 113 passed. `uv run ruff check mcp/ scripts/` and `uv run ruff format --check mcp/`: pass.
- Real test with three MCP processes over stdio (port 18810, `XDG_STATE_HOME` in a temporary directory). A fake ffmpeg sent a test image, so the dd-monitor tab kept `/dev/video0`.
  - Owner: `--ffmpeg-path fake_stream.sh`. `/api/whoami` returned `webcam_running: true`. I set the crop `100,50,320,200` on its page API.
  - Second process: `--no-ui` with a fake ffmpeg that fails with "Device or resource busy". `webcam_snapshot` returned 320x200: the owner's crop. Log: `webcam /dev/video0: using the frames of the monitor at http://127.0.0.1:18810/`.
  - Third process: the monitor with the same busy ffmpeg. It found the owner at start, took a free port (40113), and opened no browser window. `webcam_snapshot` returned 320x200.
- I did not press "Read multimeter" live, because that sends a request to OpenRouter. The tests cover the route with a mock.

### The user must restart dd-monitor

The process in tab dd-monitor runs the old code. It has no `/api/whoami`, so the new code cannot share its webcam. Restart it to get webcam sharing and the new button.

### Open items

- The tool calls of a second process do not show in the log of the owner. A later change can forward them to the owner.
- The owner must run on the port that the other processes know (`--ui-port`, default 18766). If the owner gets a free port because 18766 is busy, the other processes cannot find it.

## Round 3: the phone screen in the page

### What I did

- `mcp/debug_devices_mcp/h264.py` (new):
  - `AnnexBParser` splits a byte stream into NAL units. It handles start codes that cross two chunks.
  - `AccessUnitAssembler` groups the units into pictures. A new picture starts at a slice with `first_mb_in_slice == 0` or at an SPS, PPS, SEI, or AUD after a picture. It puts the last SPS and PPS in front of each key frame, so each key frame decodes alone.
  - `codec_string()` gives `avc1.PPCCLL` from the SPS.
- `mcp/debug_devices_mcp/phone_screen.py` (new): `PhoneScreen`.
  1. It pushes `/usr/share/scrcpy/scrcpy-server` to `/data/local/tmp/debug-devices-scrcpy-server.jar`. It does not use the file of the scrcpy window.
  2. It runs `adb forward tcp:0 localabstract:scrcpy_<scid>`, with a random 31-bit scid.
  3. It starts `adb shell CLASSPATH=... app_process / com.genymobile.scrcpy.Server <version> scid=<scid> tunnel_forward=true audio=false control=false raw_stream=true video_codec=h264 max_size=1280 video_codec_options=i-frame-interval=2 cleanup=true`.
  4. It connects to the forward. ADB accepts and closes the connection until the server listens, so the code tries again until the first bytes come.
  - The version comes from `scrcpy --version` (4.1), or from `--scrcpy-server-version`.
  - I checked the option names in the 4.1 server: `raw_stream` sets no device meta, no frame meta, and no dummy byte.
  - The frames since the last key frame stay in memory (`GopCache`). A page that opens or reloads gets them first, so it shows the picture at once. The stream is not started again.
  - A page that is too slow gets the cache again, from a key frame.
  - If the stream stops, it starts again after 3 seconds.
  - The state (`off`, `starting`, `streaming`, `error`) goes to the page.
- `GET /api/phone/screen` (`ui/routes/screen.py`): one long HTTP response with framed messages. A message has a 4-byte big-endian length, 1 kind byte (0 config JSON with the codec, 1 key frame, 2 delta frame), and the payload.
- Page: a canvas in the phone panel.
  - `fetch` reads the stream. The WebCodecs `VideoDecoder` (`optimizeForLatency`, Annex B, no `description`) decodes it.
  - The decoder drops delta frames until the first key frame.
  - After a disconnect, the page connects again after 2 seconds.
- `phone_connect` starts the screen stream (call detail `screen: started`).
- Settings (`config.py`, only my own fields):
  - New: `--phone-screen/--no-phone-screen` (on), `--phone-screen-max-size` (1280), `--scrcpy-server-path`, `--scrcpy-server-version`.
  - Changed: `--scrcpy` became `--scrcpy-window/--no-scrcpy-window`, now off by default.
- Tests:
  - `test_h264.py` (11 tests): parser, chunk borders, both start code lengths, access units, key frames, and codec string.
  - `test_phone_screen.py` (7 tests): version, server arguments, cache, and a full session. The session test uses a fake adb, a TCP server that closes the first connection, and a subscriber. It also checks that a failed start removes the forward.

### Deviation from the design: HTTP stream, not WebSocket

uvicorn needs the `websockets` or the `wsproto` package for WebSockets, and neither is installed. That requires a change to `pyproject.toml` and `uv.lock`, and dd-mcp was active on shared files. The page reads the same binary messages from a streamed HTTP response with `fetch`. The data flows in one direction only, so a WebSocket gives no extra function here. If you want a WebSocket, add `websockets` to the dependencies, and I will change the route and the reader (a small change).

### What works

- `uv run pytest`: 135 passed. `uv run ruff check mcp/ scripts/` and `uv run ruff format --check mcp/`: pass.
- Manual test with the phone: the server on 7fad170e sent High profile H.264 (590x1280, `avc1.640020`). The parser found 65 access units. It gave the same result for the whole data and for chunks of 1 to 50 bytes.
- Real stdio run: `debug-devices-mcp --adb-serial 7fad170e --ui-port 18820`, with a separate `XDG_STATE_HOME`.
  - `phone_connect` returned OK, and the screen state became `streaming`.
  - `curl /api/phone/screen` returned the config message `{"codec":"avc1.640020"}` and then a key frame.
  - Screenshot of the page (`spectacle -b -n -a`): the phone panel showed the live phone screen (the camera app) on the canvas. The state was "Screen: streaming".
- After a normal exit, no forward and no server process of that run stayed on the phone.

### Bugs found in the real tests, and fixes

- The scrcpy log directory did not exist in a new state directory. The session failed before it streamed. Fix: create the directory.
- A failure after `adb forward` left the forward on the phone, one per retry. Fix: every step after the forward is in the `try` block that removes it. A new test covers this.
- At a normal MCP exit, `adb forward --remove` did not run. The MCP shutdown cancels the lifespan, so the awaits in the cleanup stopped. Fix: `Monitor.stop()` runs the cleanup in `anyio.move_on_after(15, shield=True)`.

### Note: one dd-monitor forward removed by mistake

To remove the leaked forwards, I removed all `localabstract:scrcpy_*` forwards on 7fad170e two times. By then, the user had restarted dd-monitor (18:58:32) with the new code. My second cleanup also removed the forward of the dd-monitor stream (`scrcpy_67ba812c`). The open connection stays, and the stream still sent data afterwards (about 290 KB in 3 seconds). If that connection breaks, the stream starts again after 3 seconds with a new forward.

### The user must restart dd-monitor

The dd-monitor process started at 18:58:32. It does not have the exit cleanup fix (shield) and maybe not the forward fix. Restart it again to get all round 3 changes. After the restart, the page shows the phone screen after `phone_connect` or the "Connect" button. The separate scrcpy window stays off, unless you give `--scrcpy-window`.

### Open items

- The screen has no control: you cannot click or type on the phone from the page. The scrcpy control socket can do this later.
- If the phone rotates, the scrcpy server sends a new SPS and a new key frame. The decoder takes them in the stream. I did not test rotation.
- **dd-qa**: `scripts/qa_mcp_stdio.py` still needs `--no-ui`. Without it, `phone_connect` also starts the screen stream with the fake adb. The stream only goes to the error state, but the flag keeps the QA run clean.

## Round 4: Ctrl-C in a terminal

- Problem: the user started the server by hand in a terminal and pressed Ctrl-C. The cleanup ran (no ffmpeg, no port, no forward), but the process stayed alive. The MCP SDK reads stdin in a worker thread, and in a terminal that read waits for more input. I reproduced this in a pseudo-terminal, also with `--no-ui`: the process was alive 25 s after Ctrl-C. Ctrl-D ended it.
- Fix: `mcp/debug_devices_mcp/shutdown.py` (new). `arm_exit_watchdog()` starts a daemon timer at the end of the lifespan cleanup (one line in `server.py`). If the process has not exited 2 seconds later, the timer flushes stdout and stderr and calls `os._exit(0)`. A normal exit does not wait for the daemon timer.
- Tests: `test_shutdown.py` (2 tests). `uv run pytest`: 137 passed. `uv run ruff check mcp/ scripts/`: pass.
- Real check in a pseudo-terminal, one Ctrl-C: `--no-ui` exited after 2.0 s. With the monitor, it exited after 2.9 s. A normal stdio run with `mcp.ClientSession` exited before the watchdog fired: there was no watchdog line in its log, and no process stayed.

## Round 5: README demo

### What I did

- `scripts/record_demo.py` (new). It runs with `uv run --with playwright python scripts/record_demo.py`, so the project gets no new dependency. It uses Playwright Firefox 155 (build 1543, installed in `~/.cache/ms-playwright`) at a 1440x900 viewport with `record_video_dir`. It drives the running monitor on port 18766 and does not start a server. The steps:
  1. Connect, then wait for the phone screen canvas.
  2. Zoom in 3 steps and out 3 steps.
  3. Torch on and off.
  4. Scroll to the activity log and back.
  5. Read multimeter, hold on the reading, scroll to the log entry, and hold.
  6. Convert the video with ffmpeg: 10 fps, 1280 px wide, `libwebp_anim` quality 60, loop forever. Also write an MP4 (x264, CRF 30). The script keeps the MP4 only if it is at most 4 MB.
- Privacy mask. It is in the recorder only, not in the product page. An init script adds CSS before the first paint:
  - `#crop` gets an opaque box shadow, so everything outside the crop box is black.
  - The webcam image is hidden while no crop box shows (`#stage:has(#crop[hidden])`).
  - A MutationObserver marks the log rows that come after the start of the recording. All other rows are hidden, because their images can be uncropped.
- `README.md` (new, repository root): summary, parts, quick start, the demo image, "Live reload" for `scripts/dev-monitor.sh`, and "Record the demo".
- Output: `docs/images/monitor-demo.webp` (2,651,470 bytes, 1280x800, 366 frames, loop 0) and `docs/images/monitor-demo.mp4` (758,485 bytes).

### What works

- WebCodecs: `VideoDecoder.isConfigSupported({codec: "avc1.640020"})` returned `supported=true` in Playwright Firefox. The recording shows the live phone screen.
- The recording ran end to end in 1 min 49 s. The video is 49.6 s long.
- I extracted 9 frames with ffmpeg (at 1, 6, 12, 20, 26, 32, 38, 44, and 48 s) and looked at them. I also looked at one frame of the final WebP.
  - Only the crop area of the webcam shows. The area outside the crop box is black. No person and no photo is visible.
  - The log shows only the rows of the recording: phone_connect, phone_zoom, phone_torch, and multimeter_read.
  - The multimeter log entry shows the cropped "image sent to the model" and the reading JSON.
- Encoder choice: plain `libwebp` gave 11.8 MB. `libwebp_anim` stores only the changed parts between frames: 2.07 MB at quality 50, 2.65 MB at quality 60 (the text stays readable).

### Open items

- **The reading is "not readable" in the demo.** The model answered with confidence 0.1: "The LCD is cut off at the right edge, and no digits or unit are visible." The multimeter is off (blank LCD), and the saved crop (`728,0,355,842`) cuts the right part of the display. To get a demo with values:
  1. Switch on the meter and measure something.
  2. Draw the crop around the full display on the page.
  3. Run `uv run --with playwright python scripts/record_demo.py` again. It overwrites both files. Check the frames again.
- The Activity heading shows `(10)`: the count includes the hidden old rows. This is only in the recording.
- `.env.example` (not mine): `DEBUG_DEVICES_SCRCPY` is now `DEBUG_DEVICES_SCRCPY_WINDOW` (default false). Add `DEBUG_DEVICES_PHONE_SCREEN`, `DEBUG_DEVICES_SCRCPY_SERVER_PATH`, and `DEBUG_DEVICES_SCRCPY_SERVER_VERSION`. The old variable is ignored, and it does no damage.
- The recording clicks the real phone controls and sends one OpenRouter request per run.

## Round 6: turn the phone screen on the page

### What I did

- Settings: `ScreenRotation` (`auto`, `0`, `90`, `180`, `270`). It is `UiSettings.screen_rotation` (None means the start value) and `EffectiveSettings.screen_rotation` (default `auto`). The page saves it with `PUT /api/settings`.
- Page, phone panel:
  - "Screen view" with ⟲, ⟳, and Auto. The canvas draws each frame turned clockwise by the view angle. For 90° and 270°, the canvas swaps its width and height, so the whole screen stays visible.
  - Auto uses `(360 - rotation_degrees) % 360`. The app gives `rotation_degrees` as the Android surface rotation: 90 means that the phone turned 90° counterclockwise, and the app overlay turns clockwise by the same angle. The screenshot confirms the direction: with `rotation_degrees` 90, Auto shows the overlay text upright.
  - A new "Rotation" fact shows `rotation_degrees` and if it is locked.
  - "Snapshot rotation" (Auto, 0°, 90°, 180°, 270°) calls the new route `POST /api/phone/rotation`. The route runs the `phone_rotation` tool (source `ui`) with `{"degrees": N}` or `{"auto": true}`.
- Monitor: after `phone_connect`, a background task reads `/v1/status` once per second through the phone client. It does not use a tool call, so it adds no log row. It sends a phone update to the page only when the status changes. The page needs this for Auto.
- `phone_rotation` counts as a status tool, so its result also updates the phone panel.
- **Bug fixed in my round 4 change:** the exit watchdog started in every server lifespan, also in tests. Two seconds later it called `os._exit(0)` inside pytest, and pytest stopped with no summary. The earlier "137 passed" and "142 passed" runs only finished before the timer fired. Now `build_server()` takes `after_stop`, and only `__main__.py` gives `arm_exit_watchdog`. A new test checks that `build_server()` without `after_stop` does not call it.
- Tests: `screen_rotation` in the settings file, the default, the merge, and a refused value. Also the settings API, the rotation route (lock, auto, refused 45), the status poll (new `rotation_degrees`, no log row), and `after_stop`.

### What works

- `uv run pytest`: 148 passed, and the run ends with its summary. `uv run ruff check mcp/ scripts/` and `uv run ruff format --check mcp/`: pass.
- Check in Playwright Firefox against the watchexec monitor on port 18766 (it restarted by itself), with the recorder privacy mask:
  - Auto: the view was "auto, now 270°", the canvas 1280x590, and the overlay text was upright.
  - ⟳: 0°, canvas 590x1280. ⟲ two times: 180°.
  - Snapshot rotation 90°: "90° (locked)". Auto: "follows the phone".
  - I set the view back to Auto and the snapshot rotation back to Auto. The saved settings contain `"screen_rotation": "auto"`.

### Open items

- The dev monitor restarts on its own. Open pages connect again after the restart.
- Next: the README demo recording (round 5) can run again after the meter is switched on and the crop covers the display.

## Round 7: demo with a fake phone (stopped before the recording)

The orchestrator stopped this task before the recording, because the room light is off. I did not run the recorder. I did not make a new WebP or MP4. No demo server, fake phone, or check script runs now (checked with `ps`).

### What I did

- `scripts/make_demo_meter.py` (new): it takes a full frame from `http://127.0.0.1:18766/api/webcam/frame.jpg`, keeps only the meter, and draws a 7-segment reading on the LCD. The digits are classic hexagonal segments with a slant, a decimal point, the `kΩ` unit (DejaVu Sans Bold), and an `AUTO` flag, in dark LCD ink with a light blur.
  - The meter was not at the given place (x 640–1050): it moved to the right. Defaults now: meter box `870,60,1340,860`, LCD box `975,150,1245,290`. Both are flags.
  - Output: `docs/images/demo-meter.jpg` (470x800, 79,852 bytes, "4.70 kΩ"). I looked at the whole image: it shows only the meter, the wall, and cables. There are no people.
- `scripts/record_demo.py` (not run in this round):
  - Privacy: outside the crop box, the view is 82% black (crop box shadow `rgba(0,0,0,0.82)`), over a layer with `backdrop-filter: blur(18px)`. The layer has an `evenodd` `clip-path` hole at the crop box, and the hole follows the box on each animation frame. The webcam is still hidden when there is no crop box, and the old log rows are still hidden.
  - New `--fake-phone <jpg>`: it starts `scripts/fake_phone.py --port 18865 --snapshot <jpg>`, and a demo MCP server with its own temporary `XDG_STATE_HOME`. That folder has a crop at the meter (`--demo-crop`, default `870,60,470,800`). The server gets `DEBUG_DEVICES_ADB_PATH=scripts/fake_adb.py`, `--adb-serial fake-phone-0001`, `--local-forward-port 18865`, `--no-phone-screen`, and `--no-ui-open-browser`. The script reads the page URL from the stderr line `monitor window: ...`. At the end, it closes stdin of the server and stops the fake phone.
  - Why `--ui-port 18766` and not another port: the demo server must find the webcam owner (the watchexec monitor) through `/api/whoami` on that port. The port is busy, so the demo server binds a free port for its own page. With another port, it finds no owner, and the webcam stays busy.
  - Scenario with `--fake-phone`: Connect (it waits for the fake serial), zoom, torch, "Take snapshot" (the phone panel shows the composed photo), scroll, then "Read multimeter" with the source "from a phone snapshot". If the read fails, it tries one more time.
- Product changes for the demo:
  - Page: a source menu next to "Read multimeter" ("from the webcam", "from a phone snapshot"). `POST /api/multimeter/read` takes an optional `{"source": ...}` (400 for other values). It calls `multimeter_read` with `include_image: true`. The monitor labels the returned image of `multimeter_read` "image sent to the model", so the log shows the image for the phone source too.
  - A second monitor (webcam owner elsewhere) now shows the owner's webcam: `/api/webcam/stream.mjpg` and `/api/webcam/info` forward the owner's stream and info (`RemoteMonitor.stream()`, `RemoteMonitor.info()`).
- Tests: the phone source (arguments, image label, refused source) and the forwarded webcam info and stream. `uv run pytest`: 150 passed. `uv run ruff check mcp/ scripts/` and `uv run ruff format --check mcp/ scripts/`: pass.
- `README.md`: "Record the demo" now shows `make_demo_meter.py` and `--fake-phone`, and says to turn on the room light.

### What I checked before the stop

- Demo server with the fake phone and the blur mask (no multimeter read): the demo page started on a free port (41435). It showed the owner's webcam at 1920×1080 through the forwarded stream.
- The computed style in Firefox: `backdrop-filter: blur(18px)`, the `evenodd` hole at the crop box, and the box shadow `rgba(0, 0, 0, 0.82)`.
- A test screenshot with the webcam 4 times brighter (only in that check): outside the crop box, the room is dark and blurred. No face is recognizable. The whole frame is dark, because the room light is off.

### Open items

- Record when the room light is on:
  1. If the meter moved, run `uv run python scripts/make_demo_meter.py` again, and check `docs/images/demo-meter.jpg` (no people).
  2. Run `uv run --with playwright python scripts/record_demo.py --fake-phone docs/images/demo-meter.jpg`.
  3. Extract frames and check the blur on the left side before you publish.
- After the new recording, add one line under the demo image in `README.md`: "The recording uses a fake phone with a composed photo of the meter." The current `monitor-demo.webp` is still the old recording (real phone, black mask, "not readable"), so I did not add the line yet.

## Round 8: lazy MCP server, monitor_open, bench_start, and bench_stop

### What I did

- New setting `--ui-start lazy|eager` (`DEBUG_DEVICES_UI_START`, default `lazy`). `scripts/dev-monitor.sh` passes `--ui-start eager`.
- Lazy mode (`ui/monitor.py`):
  - `Monitor.start()` (the lifespan hook) does nothing. At process start there is no port, no web server, no ffmpeg, no adb, no scrcpy, and no browser. The event bus exists from the start, so the page shows the early calls.
  - The first MCP tool call starts the page (`ensure_page`). A page failure is logged and does not fail the tool. With `--ui-open-browser`, Firefox opens one time, at the first page start, but not when another debug-devices monitor answers on the port (new `RemoteMonitor.find_monitor()`).
  - The webcam starts on the first use (`ensure_webcam`): `OnDemandFrameSource` for `webcam_snapshot` and `multimeter_read`, a page that reads the MJPEG stream (`webcam_viewer`), or another process that asks for a cropped frame (`webcam_user`). If another monitor owns the webcam, the frames come from it, as before.
  - Idle release: `--webcam-idle-timeout` (`DEBUG_DEVICES_WEBCAM_IDLE_TIMEOUT`, default 300 seconds, 0 = never). A watch task stops the stream when it has no frame users, no page viewers, and no use for that time. The next use starts it again. The monitor takes a clock, so the tests use a fake clock.
  - I checked: adb runs only in the phone tools (`phone_connect` and after it). `obv-dump` runs only in `board_open`. `Services.from_settings` makes only httpx clients, and they open no socket.
- New tools (`ui/tools.py`, registered in `build_server` when the monitor is on):
  - `monitor_open(open_browser=true)` returns the real `url`, `opened_browser`, and `browser`.
  - `bench_start(open_browser=true, phone=true, webcam=true, board_path=None)` has the steps page, webcam (waits for the first frame), phone (`phone_connect` as a nested recorded call, so the phone screen starts as usual), and board (`board_open`). Each step runs even when another one fails. The result has the URL and `ok`/`error`/`skipped` with a detail for each step.
  - `bench_stop()` stops the webcam stream, stops the phone screen, scrcpy, and the status poll, removes the adb forward of the camera API (new `Adb.remove_forward`), and stops the page 0.5 s after it answers. The event log stays.
  - The tool descriptions and the server instructions say "start the bench" and "stop the bench", and tell the agent to give the user the URL.
- Page: "Start all" (`POST /api/bench/start`, `bench_start` without a new browser window) and "Stop all" (`POST /api/bench/stop`) in the header. Both run through the instrumented `call_tool` with source `ui`.
- `MonitorOptions.open_browser` now defaults to False (the settings still default to True). Then no test can open a browser window.
- Docs: `mcp/README.md` ("Lazy start", the tools table, the flags), `README.md` (lazy start and ChatGPT desktop / Codex note, live reload), `AGENTS.md` (one line), `.env.example` (the two new variables, as AGENTS.md asks), and the `--browser` comment in `scripts/agent.sh`.

### Tests

`mcp/tests/test_lazy.py` (6 tests, fake ffmpeg spawner, fake adb runner, fake opener, fake clock):

- In lazy mode, `list_tools` runs no subprocess (no ffmpeg start, no runner call), serves no page, and opens no browser. The first tool call serves the page (HTTP 200) and opens the browser one time.
- The webcam starts at the first `webcam_snapshot`. It does not stop before the timeout, and it does not stop while a page watches. It stops after the timeout, and the next use starts it again.
- `monitor_open` returns the real URL and opens the browser only when asked.
- `bench_start` runs every step. A bad `board_path` is an error step, and the other steps are ok. The log has `bench_start`, `phone_connect`, and `board_open`. `bench_stop` stops the stream, removes the forward for the serial, and stops the page. The next tool starts the page again, and the log stays.
- `bench_start` with `phone` and `webcam` off skips those steps.
- Eager mode starts the page and ffmpeg at the lifespan start, and it does no idle stop.

`uv run pytest`: 185 passed, 1 skipped. `uv run ruff check`, `uv run ruff format --check`, and `prek run --all-files` (in `nix develop`): pass.

### Real checks with `codex exec` (from the repository root, `.codex/config.toml` not changed)

- `codex exec --skip-git-repo-check "say hi"`: Codex answered before the MCP server finished its start (only the `nix develop` wrapper showed). This run proves little.
- `codex exec ... "List the tool names of the debug_devices MCP server. Do not call any tool."`: the new server process (pid 1965072) ran on the host. During the run, no new listening port, no ffmpeg, no Firefox, no adb, and no scrcpy appeared (polled `ps` and `ss` every 0.3 s against a baseline).
- `codex exec ... monitor_open with open_browser false`: the call completed and returned `http://127.0.0.1:18766/`. The only new thing was the listening port 18766; no ffmpeg, no Firefox.
- `codex exec ... bench_start (open_browser false), then bench_stop`, two runs:
  - Run 1: Codex ran the MCP server in its sandbox (`sandbox: workspace-write`). The server PID was not visible on the host, the page bind failed with `[Errno 1] Operation not permitted`, `/dev/video0` did not exist, and adb could not reach its server ("ADB server didn't ACK", server pid 23). Each step still reported its error, and `bench_stop` answered ok. The before and after state of the host processes and the phone forwards was the same.
  - Run 2 (the prompt said "do not run shell commands"): the server ran on the host. `bench_start`: page ok (`http://127.0.0.1:18766/`), phone ok ("Phone connected; app health ok"), webcam error "Device or resource busy". The webcam was not free: an older MCP process of another session (pid 1946087, old code, no page) held it with its ffmpeg (pid 1963099). `bench_stop`: webcam, phone, and page ok. After it, my phone screen server (`adb shell ... app_process`, pid 1987011) was gone, no ffmpeg of mine stayed, and the forward `tcp:18765` was removed. The ffmpeg, the scrcpy server, and the three older `scrcpy_*` forwards that stayed all belong to pid 1946087.
- No test and no check opened a Firefox window. The Firefox processes in the lists were an older window (pid 1851592).

### Open items

- The watchexec dev monitor did not run during my checks. Start `scripts/dev-monitor.sh` again: it now passes `--ui-start eager`. Running MCP processes of other sessions (for example pid 1946087) still run the old code until they restart.
- `bench_stop` removes the shared camera forward `tcp:18765`. Another session that uses the phone at the same time must call `phone_connect` again.
- Codex sometimes starts the MCP server in its sandbox (run 1 above). Then no hardware tool works. I did not find what decides this; the project config sets no sandbox for the server. The orchestrator can check the Codex sandbox options for MCP servers. I did not edit `.codex/config.toml`.
- I did not commit.

## Round 9: full screen for the camera views

### What I did

- Page (`ui/static/`):
  - Three views are in `.view` boxes (`tabindex="0"`): the webcam (`#webcam-view` around `#stage`), the phone screen (`#phone-view` around the canvas), and the last snapshot (`#snapshot-view`).
  - Each box has a full screen button: top right, SVG icon, `aria-label` "Full screen: …", reachable with Tab.
  - A double-click on a view, or the key `f` on the focused view, turns full screen on or off (Fullscreen API on the box). `f` does nothing in form fields. `Esc` leaves full screen (browser default).
  - In full screen: black background, aspect ratio kept (`object-fit: contain` for the canvas and the snapshot). The webcam frame keeps its ratio (`--frame-ratio` from the frame size, and `aspect-ratio`), so the crop box stays on the same part of the image.
  - The phone screen keeps its rotation (the canvas already has the turned picture). A small bar with zoom out, zoom in, and torch shows only in full screen.
  - The webcam view shows the crop box in full screen, but a drag there does nothing. A pointer down in the webcam view now focuses the view, so `f` works after a click there too.
  - The snapshot image is no longer inside a link, because a single click on the link opened a new tab before a double-click could come. A separate link "Open the full-size snapshot" replaces it.
- Full-resolution snapshot: the tool result has only the scaled image (long edge 1568), so the monitor keeps the full JPEG of the same call. `Monitor.phone_snapshot_recorder()` wraps `services.phone.snapshot` in `ui/setup.py`. The full image of each call is kept only while that call runs, and the `finally` in `_record_call` removes it. `GET /api/phone/snapshot.jpg?full=true` returns the full image, or the scaled one when there is no full one. Without `full`, the route returns the scaled image for the panel, as before. A bad value returns 400.
- `scripts/record_demo.py`: the snapshot selector is now `#snapshot-view`.
- `mcp/README.md`: "Full screen" in the monitor page section.

### Tests

- Route tests in `test_ui_app.py`:
  - With a 3000x2000 fake snapshot, the panel image has a long edge of 1568, and `?full=true` returns the original bytes. Before a snapshot: 404. `full=maybe`: 400. No full image stays behind.
  - The fallback to the scaled image without the recorder.
- Playwright check (headless Firefox) against a separate eager server with the fake phone and fake adb. The flags were `--ui-start eager --ui-port 18890 --no-ui-open-browser --no-phone-screen --webcam /dev/video99`, so the check never used the real camera. It checked `document.fullscreenElement`:
  - Webcam: a double-click enters, `f` leaves, the button enters, and a double-click leaves.
  - A crop drag still sets a crop (`575,317,575,432`). A drag in full screen does not change it, and a double-click does not change it. The saved crop is the same.
  - Snapshot: a double-click enters, and the image then loads `?full=true`. `f` leaves, and the image loads the panel URL.
  - Phone screen (a test picture on the canvas, because the fake phone has no stream): the button enters, and the bar shows (`display: flex`). The bar's zoom-in set the zoom to 1.50. `f` leaves, and the bar hides.
- The screenshots stay in my scratch folder. They show only the fake phone picture, my test canvas, and the webcam error text, no camera frame.
- `uv run pytest`: 209 passed, 1 skipped. `uv run ruff check`, `uv run ruff format --check`, and `prek run --all-files`: pass.

### Bug found in the check, and fix

- In full screen, the webcam `#stage` had no height when there was no frame (`min-height: 0` and an image with no size). Then the error text was not visible, and Playwright could not click the view. Fix: `aspect-ratio: var(--frame-ratio)` on the stage in full screen.

### Open items

- In headless Firefox, the full screen size is 1366x768, so the screenshots show the page around it. A real screen fills completely.
- The `f` key needs the focus on a view. A click on the phone canvas or the snapshot focuses the view (`tabindex`). The webcam view gets the focus in its pointer handler.

## Round 10: clean page stop with open streams

### Problem

At each reload, the dev proxy (`devreload.py`) closes stdin of the server. The lifespan cleanup then calls `Monitor.stop_page()`, which sets `monitor.closing` and asks uvicorn to stop. An open Firefox page holds the SSE stream `/api/events`, and often the MJPEG stream `/api/webcam/stream.mjpg`:
- The SSE loop checked `closing` only after `queue.get()` returned, which can take up to 15 s (the keepalive time).
- The MJPEG loop did not check it at all.

uvicorn waited its graceful time (1 s), then cancelled the tasks. It logged "ERROR: Cancel N running task(s), timeout graceful shutdown exceeded" and an ASGI traceback.

### Fix

- `until_closing(source, closing)` in `ui/routes/__init__.py` passes on the items of a stream. It races each next item against the closing event. When the page stops, it cancels the waiting read, closes the source generator (its `finally` and context managers run: the bus subscription, the webcam viewer count), and ends the response.
- It wraps the three long streams: SSE (`routes/state.py`), MJPEG and the owner proxy stream (`routes/webcam.py`), and the phone screen (`routes/screen.py`). The phone screen loop no longer polls `closing`. Its 1 s wait is now only for the resync check.

### Tests

- `mcp/tests/test_ui_shutdown.py`:
  - `until_closing` ends a stream that waits forever, and it closes the source. A finite stream passes through unchanged.
  - A real in-process uvicorn page, with one open SSE client and one open MJPEG client (a fake webcam that never sends a frame). `stop_page()` must take less than half the graceful time, with no ERROR record and no "graceful shutdown" message in the logs.
- Without the fix (`until_closing` patched out in a temporary test, deleted after), the same test fails: "stop took 1.13 s", and the log has "ERROR: Cancel 2 running task(s), timeout graceful shutdown exceeded". This is the reported error.
- Real reload check: `debug-devices-mcp-dev --ui-start eager --ui-port 18891 --no-ui-open-browser --no-phone-screen --webcam /dev/video99` (the real camera is not used), with two `curl -N` clients on `/api/events` and `/api/webcam/stream.mjpg`. Then a new mtime on `ui/static/style.css`. The proxy reloaded the server in 2.2 s. The server ended both curl streams, and stderr had no "Traceback", "ERROR", "graceful shutdown", or "CancelledError". No process stayed after the proxy stdin closed.
- `uv run pytest`: 220 passed, 1 skipped. `uv run ruff check` and `uv run ruff format --check`: pass.

### Open items

- A page that reads the stream reconnects after the reload by itself (EventSource, and the MJPEG image `error` handler), as before.

## Round 11: mouse-wheel zoom on the full-screen phone snapshot

### What I did

- Only in the full-screen snapshot (`document.fullscreenElement === #snapshot-view`), the mouse wheel zooms the photo. It is a digital zoom of the full-resolution still (the phone camera zoom does not change a still image). Region `snapshot zoom` in `app.js`.
  - Constants: `SNAPSHOT_ZOOM_STEP = 1.25`, `SNAPSHOT_ZOOM_MIN = 1` (fit), `SNAPSHOT_ZOOM_MAX = 8`, reset key `0`.
  - The zoom keeps the image point under the mouse pointer in place: `transform: translate(x, y) scale(s)` with `transform-origin: 0 0`. The movement is limited, so no empty band shows at an edge.
  - The wheel listener is on `#snapshot-view` with `{ passive: false }`. It calls `preventDefault()` only in that full-screen view. Everywhere else the wheel scrolls as before. The live phone view, the webcam view, and the panel snapshot do not change.
  - When zoomed in, a drag with the left mouse button moves the photo (pointer capture, grab cursor). A double-click still leaves full screen.
  - Reset: the key `0`, leaving or entering full screen, and a new snapshot.
  - A small label at the bottom left shows the factor ("1.25x", "8x").
- New `PhoneState.snapshot_seq`: the monitor counts the snapshots. Before, the page reloaded the snapshot image at every phone update, and the status poll sends updates. Now it reloads (and resets the zoom) only when a new snapshot comes.
- **Bug found and fixed:** the double-click that enters full screen also selected the image. Firefox then painted the blue selection color over the whole view (also before this change). Fix: `user-select: none` on `.view`.
- `mcp/README.md`: the full screen section describes the wheel zoom.

### Tests

- `test_ui_app.py`: `snapshot_seq` goes up with each snapshot and stays the same after a status call.
- Playwright check (headless Firefox, 1440x600 viewport) against a separate eager server with the fake phone (snapshot `docs/images/demo-meter.jpg`) and fake adb (`--ui-port 18890 --webcam /dev/video99 --no-phone-screen`). The real camera and the real phone were not used.
  - Outside full screen, the wheel over the panel snapshot scrolled the page, and the transform stayed empty.
  - In full screen: the label shows "1x". A wheel up at (400, 300) gave `translate(-100px, -75px) scale(1.25)`, "1.25x" (the point under the mouse stays in place). A wheel down went back to fit ("1x", no transform). 15 wheel ups stopped at "8x".
  - A drag moved the photo (`translate(-1648px, -1236px)` to `translate(-1748px, -1286px)` at 5.12x), and the view stayed in full screen.
  - The key `0` reset to "1x". Leaving full screen with a double-click reset to "1x".
  - A screenshot after the fix shows the photo in true colors on black. Before the fix, the whole view was blue.
- `uv run pytest`: 221 passed, 1 skipped. `uv run ruff check`: pass. `uv run ruff format --check` passes for `ui/`, `mcp/tests/`, and `scripts/`. It fails only in `mcp/debug_devices_mcp/instructions.py` (dd-mcp edits it now, not my file): one string wants single quotes.

## Round 12: horizontal and vertical flip of the phone snapshot (server-side)

The earlier display-only mirror plan was replaced before I built it. The one setting field that I had added for it is removed.

### What I did

- **One helper** (`images.py`):
  - `SnapshotOrientation` holds `flip_horizontal` and `flip_vertical`. It is frozen, so it can be a cache key, and it has `describe()`.
  - `orient_jpeg()` applies the EXIF rotation first, then `ImageOps.mirror` and `ImageOps.flip`. It re-encodes with the quality steps of `downscale_jpeg`, so the result is never larger than the source. With no flip, it returns the same bytes object.
- **One state** (`orientation.py`): `OrientationState` reads `snapshot_orientation` from the UI settings file at start. `update()` changes the flips (None keeps a flip), saves them into the same file, and calls the listeners. The other settings stay. It works with `--no-ui` and in lazy mode: `Services.from_settings` creates it, and it only reads a small JSON file.
- **One place for the transform** (`server.py`): `Services.phone_snapshot()` takes the phone still and applies the orientation. `phone_snapshot` (result and `save_path` file) and `multimeter_read(source=phone)` use it. `bench_start` takes no snapshot. The downscale comes after the flip. `SnapshotInfo.orientation` states the orientation (for example "flipped horizontally"), and the `phone_snapshot` description says that pixel positions refer to the oriented photo.
- **New tool** `phone_snapshot_orientation(flip_horizontal=None, flip_vertical=None)`: no argument only reads. It uses the same state, the same file, and the same page.
- **Monitor**:
  - The monitor keeps the raw still of the last snapshot (true orientation, full size). `render_snapshot()` makes the page images from it in the current orientation: full size for `?full=true`, scaled for the panel. It caches them per snapshot number, orientation, and size. A new flip therefore shows on the existing snapshot at once. Without the raw still, the page gets the scaled tool result as taken.
  - `PhoneState.orientation` carries the flips to the page (SSE). An agent change shows at once.
  - `update_settings()` keeps the flips of `OrientationState`, so a page save of other settings cannot overwrite them.
  - The tool log keeps each image as it was taken.
- **Page**:
  - "Flip H" and "Flip V" toggles (`aria-pressed`) in the phone panel, next to the snapshot, and in a bar in the full-screen snapshot. The keys `h` and `v` work when the snapshot view has the focus or is full screen.
  - A toggle calls `POST /api/phone/orientation`, which runs the tool with source `ui`, so the log shows it. The page then reloads the snapshot from the server; there is no CSS flip on the snapshot.
  - The live phone view gets the same flips as a CSS `scale()` on the canvas, after the rotation that the canvas draws (the same order as on the server).
  - The wheel zoom works on the flipped image.
- `docs/phone-api.md` is not changed: the phone app is not involved.
- Docs: `mcp/README.md` (tools table and "Snapshot flips"), `README.md` (tool list).

### Tests

- `mcp/tests/test_orientation.py` (9 tests):
  - A pixel test on a four-color image for H, V, and both, with the result not larger than the source.
  - With no flip, the same bytes.
  - The EXIF rotation (orientation 6) comes before the flip, and the result has no EXIF orientation.
  - The descriptions.
  - Persistence: the other settings stay, and a new state reads the saved flips.
  - The tool result states the orientation, and its image and `save_path` file are flipped.
  - The page and the tool show the same orientation: the panel, full size, and tool log images. A new flip re-renders the existing snapshot. A stale page save keeps the flips.
- Playwright check (headless Firefox) against a separate eager server with the fake phone (a four-color photo), port 18890, `--webcam /dev/video99`, and its own state folder:
  - As taken, the top left is red. The panel "Flip H" gives green, `aria-pressed` true, and the live view CSS `scale(-1, 1)`.
  - The key `v` gives white, and the live view CSS `scale(-1)` (that is, -1, -1).
  - In full screen, the bar shows. Its "Flip H" gives blue, the image stays full size, and the wheel zoom still works (`translate(-75px, -75px) scale(1.25)`).
  - The server orientation matches the page. The log has three `phone_snapshot_orientation:ui` calls.
- `uv run pytest`: 230 passed, 1 skipped. `uv run ruff check`: pass. `ruff format --check` on my files: pass. `prek run --files` on my files: pass.

### Open items

- The live view flip is CSS only (the phone screen stream is video). The snapshot is the evidence image: the server makes it, and the agent gets the same image.

## Round 13: snapshot flips on the phone preview, no CSS flip of the live view

### Why

The page flipped the whole live phone screen with CSS, so the app's status text (zoom, torch) was mirrored and unreadable. The app now mirrors only its camera preview (`POST /v1/preview`, contract in `docs/phone-api.md`, changed before this task).

### What I did

- `phone_api.py` (small edit):
  - `CameraStatus.preview_flip_horizontal` and `preview_flip_vertical` default to false, so the status of an app from before the endpoint still parses.
  - New `PreviewFlipRequest` and `PhoneClient.preview()`. A 404 becomes `PreviewNotSupportedError` ("update the app").
  - `constants.phone.PATH_PREVIEW`.
- `orientation.py`: `PreviewSync`, the one sync object.
  - `push()` sends the current snapshot flips.
  - `ensure(status)` sends them only when the status shows other preview flips (an app restart).
  - After a 404, it warns one time and stops trying until the next `phone_connect` (`reset()`).
  - Other phone errors (not connected, camera not ready) only log at info level: `phone_connect` and the status reads send the flips again later.
- `server.py` (small edits):
  - `Services.preview_sync` is created in `__post_init__`.
  - `connect_phone` resets the 404 flag and calls `ensure()`. `bench_start` runs `phone_connect`, so it is covered too.
  - `phone_status` calls `ensure()` (app restart).
  - `phone_snapshot_orientation` calls `push()` after the change. The page toggles run this tool.
  - The tool description says that the phone mirrors its preview the same way.
- `ui/setup.py`: the monitor status poll (1 s after `phone_connect`) reads the status through `ensure()`, so an app restart is fixed within about a second.
- The snapshot is still flipped only by the server (`orient_jpeg`). The app keeps `/v1/snapshot` unflipped, so nothing is flipped twice.
- Page: I removed the CSS flip of the live phone view; the live view shows the phone screen as it is. The snapshot flip stays on the server, as before. The phone panel has a new line, "Preview flip", from the camera status ("not flipped", "flipped horizontally", "flipped horizontally and vertically").
- `scripts/fake_phone.py`:
  - `POST /v1/preview` with the 400 rules: exactly `flip_horizontal` and `flip_vertical`, both booleans. A missing field, another type, or an unknown field gives 400 `bad_request`.
  - The new status fields.
  - `--no-preview` is an old app: the path gives 404 (not 405), and the status has no preview fields.
- `scripts/qa_contract.py`:
  - The status requires the two fields.
  - The start state requires no preview flips.
  - New `check_preview`: all four flip combinations, in the POST answer and in `GET /v1/status`, then 7 bad bodies with 400 and no change.
  - `GET /v1/preview` is in the wrong-method list (405).
- `mcp/README.md`: "Snapshot flips" describes the phone preview.

### Tests

- `mcp/tests/test_preview_sync.py` (6 tests):
  - The client sends the flips and reads the status. An old app raises `PreviewNotSupportedError`, and an old status reads as not flipped.
  - `phone_connect` sends the chosen flips. The same flips send nothing. A toggle sends at once. After an app restart (preview flips back to false), the next `phone_status` sends them again.
  - An old app: `phone_connect` and `phone_status` still work, and the snapshot is still flipped by the server. It gets one request and one warning per `phone_connect`, not one per status read.
  - `scripts/qa_contract.py --strict` against `scripts/fake_phone.py` passes, with `PASS preview`.
- `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18896 --strict` against the fake phone: 19/19 checks passed.
- Playwright (headless Firefox, fake phone, eager server on port 18890, `--webcam /dev/video99`):
  - After connect: "not flipped". Panel "Flip H": "flipped horizontally" (the check ran before the wording change, which shows "horizontal").
  - The key `v`: both flips.
  - The live view `style.transform` stayed empty at each step.
  - The fake phone `/v1/status` showed `preview_flip_horizontal: true`, `preview_flip_vertical: true`.
- `uv run pytest`: 236 passed, 1 skipped. `uv run ruff check`: pass. `ruff format --check` and `prek run --files` on my files: pass.

### Open items

- I did not test against the real phone app: dd-android builds `/v1/preview` now. After the new app is on the phone, run `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict`, and check on the phone that only the camera image mirrors.
- The live view shows what the phone screen shows. So the preview flips show on the page only when the app mirrors its preview; with an old app, the live view stays unflipped.

## Round 14: phone camera zoom from the full-screen live view (wheel and arrow keys)

### What I did

- Region `live zoom` in `app.js`. It acts only when `document.fullscreenElement` is `#phone-view` (the live phone screen).
  - A wheel listener on `#phone-view` with `{ passive: false }` and `preventDefault()` only in this full-screen view. Wheel up gives step `in`, wheel down step `out`.
  - A key listener, also only in this full-screen view and not in form fields: `ArrowUp` and `ArrowRight` zoom in, `ArrowDown` and `ArrowLeft` zoom out.
  - Each step is the phone camera zoom: `POST /api/phone/zoom {"step": ...}` runs `phone_zoom` through the instrumented `call_tool` (source `ui`, in the log). It is not a digital zoom of the video.
  - Rate limit: `LIVE_ZOOM_INTERVAL_MS = 150`. There is at most one step per interval, and none while a zoom request runs. Extra events are dropped, not queued.
  - The full-screen bar shows the zoom ratio from the returned `CameraStatus` ("1.5x"). It is highlighted for 1.2 s after a step, and it is set when the view enters full screen.
- The digital wheel zoom of the full-screen snapshot, the webcam view, and the page scrolling do not change.
- `mcp/README.md`: the full screen section.

### Tests

Playwright check (headless Firefox, 1440x600) against a separate eager server with the fake phone and fake adb (`--ui-port 18890 --webcam /dev/video99 --no-phone-screen`; a test picture on the canvas, because the fake phone has no stream):

- In full screen, one wheel up: the fake phone zoom went 1.0 to 1.5, the bar showed "1.5x", and the log had `phone_zoom in:ui`.
- A burst of 10 wheel-up events inside one interval: exactly one more call (fake phone zoom 2.25).
- `ArrowUp`, `ArrowRight`, `ArrowDown`, `ArrowLeft` (0.3 s apart) gave `in, in, out, out` in the log. The zoom went back to 2.25.
- Outside full screen, the wheel over the phone view scrolled the page, and `ArrowDown` made no zoom call (0 new calls).
- `uv run pytest`: 236 passed, 1 skipped (no Python change in this task). `uv run ruff check` and `prek run --files` on the page files: pass.

## Round 15: one monitor page shows the tool calls of every MCP server

### What I did

- **Primary and secondary** (`ui/monitor.py`):
  - The primary is the server with the page on the configured UI port.
  - Before `ensure_page()` serves a page, it asks `/api/whoami` on that port (same app name, another pid, through `RemoteMonitor.find_monitor()`). If a monitor answers, this server is a secondary: it serves no page and opens no browser, `primary_url` is set, and the forwarder starts.
  - `monitor_open` and `bench_start` return the primary URL (`open_browser()` uses it too).
  - A secondary that had a primary waits up to 3 s for it (`PRIMARY_RESTART_GRACE`, polls every 0.25 s), so a dev monitor reload does not give the port to a secondary. When no primary answers, the next tool call starts this server's page, and it becomes the primary.
- **Token** (`ui/forward.py`):
  - At each page start, the primary writes a new `secrets.token_urlsafe` token to `$XDG_RUNTIME_DIR/debug-devices/ingest-<port>.token`, mode 600, created with `O_EXCL`. Without `XDG_RUNTIME_DIR`, the state folder is used.
  - At page stop, the primary removes the file, but only if it still holds its own token.
- **Forwarder** (`CallForwarder`, secondary side):
  - It subscribes to the bus in `start()`, not in its task, so the events of the call that found the primary are not lost.
  - One worker sends the local events in order: each start and end event to `POST /api/ingest/calls` (`IngestCall{origin, event}`), then at the end of a call its images to `POST /api/ingest/calls/{id}/images?origin=&label=&index=`. The header `X-Debug-Devices-Token` carries the token.
  - The timeout is 1 s. A tool call never waits: the bus hands the events over with `put_nowait`.
  - After a failed send, it looks up the primary again at once and reads the new token (a restarted primary), then tries one more time. After a failed lookup, it backs off: 1 s, 2 s, up to 30 s.
  - At stop, it sends what is still queued, for at most 1 s.
- **Origin label**: the MCP client name from `initialize` (`context.session.client_params.client_info.name`, read one time) and the pid, for example "codex 1824788". Without a name, "mcp <pid>".
- **Redaction**: forwarded events of `bench_instructions` (the user's instructions text) carry only the tool name and the status: the summary is "(not forwarded: the user's instructions)", with no details and no images. The OpenRouter key is never in an event: events carry tool arguments and results only.
- **Primary side**:
  - `routes/ingest.py`:
    - 403 without the token or with a wrong one (`secrets.compare_digest`).
    - Size limits: 1 MiB per event (413), 16 MiB per image (413).
    - Only `image/jpeg` and `image/png` (415).
    - An image before its event gives 404.
  - `EventBus.ingest()` adds or updates a call. A late start event never undoes an end.
  - Per sender: the recent 200 calls and images for the recent 30, and at most 20 senders.
  - `calls()` merges all senders by start time. The existing image route finds the calls of other senders too.
- **Page**: the source badge shows the sender ("mcp · codex 1824788").
- **Tests**: `conftest.py` has an autouse fixture that points `XDG_RUNTIME_DIR` at a temporary folder, so tests never write token files into the real one.
- **Docs**: `mcp/README.md` ("More than one MCP server"), `README.md` ("Live reload").

### Tests

- `mcp/tests/test_shared_log.py` (10 tests):
  - Ingest routes: 403 without the token and with a wrong one; 204 and the call with its origin and duration; images, 404 before the event, 415, and 403.
  - A late start event after the end; the limits per sender; the token file mode 600.
  - Forwarding over real HTTP (a primary on port 0, a secondary in the same process with a patched primary pid): the calls and the image arrive with the origin label. The secondary has no page, and `monitor_open` gives the primary URL.
  - The instructions are redacted.
  - A primary restart with a new token: the secondary waits, does not take the port, and sends to the new primary.
  - A primary that hangs for 10 s does not slow three tool calls.
  - With no primary, the server serves its own page and sends nothing.
- Real check (two processes, a shared temporary `XDG_RUNTIME_DIR`):
  - A primary (`debug-devices-mcp --ui-start eager --ui-port 18892 --webcam /dev/video99`) and a second server over stdio (`mcp.ClientSession`, fake phone, fake adb, `--ui-port 18892`).
  - The primary wrote `ingest-18892.token`.
  - The second server: `phone_connect` and `phone_status` ok, and `monitor_open` returned `http://127.0.0.1:18892/`. Only 18892 and the fake phone port listened, so the second server served no page.
  - The primary `/api/state` listed `phone_connect`, `phone_status`, and `monitor_open`, all ok, with `origin=mcp 1445664` (the Python MCP client names itself "mcp").
- `uv run pytest`: 246 passed, 1 skipped, five runs in a row. One run during a parallel prek run failed one test and passed on the next run. I raised the time limit of the slow-primary test from 1 s to 3 s (the fake primary hangs for 10 s). `uv run ruff check`, `ruff format --check`, and `prek run --files` on my files: pass.

### Notes and open items

- **The code is live.** watchexec restarted the running dev monitor (pid 1390385, 19:58:47) and the dev-reload agent servers with the new code while I worked. The dev monitor wrote `ingest-18766.token`. One agent server reloaded while the dev monitor restarted and took a free port (`ingest-41513.token`); it becomes a secondary only after it stops serving that page. I did not stop or change these processes.
- The page of a secondary is not served, so only the primary URL works. Old bookmarks of random secondary ports do not work anymore; that is intended.
- The 3 s restart grace can delay one tool call of a secondary by up to 3 s, but only when its primary is gone.

## Round 16: phone distance and detail (monitor page and phone_status)

### What I did

- `phone_api.py` (small edit):
  - New models `Focus`, `Optics`, `FocusState`, and `FocusCalibration`. `CameraStatus.focus` and `CameraStatus.optics` are optional (default None), so an old app still works.
  - An unknown state becomes `unknown`, and an unknown calibration becomes `uncalibrated` (then no cm show), so a new app value does not break the status.
- `focus.py` (new, pure math, named constants):
  - `distance_cm = 100 / diopters`: None for no value and for 0 (infinity).
  - `detail_px_per_mm = output_width_px * focal_length_mm / (sensor_width_mm * distance_mm)`.
  - The closest focus distance comes from `min_distance_diopters`; 0 is fixed focus, with no "too close".
  - Advice: `too_close` (nearer than the closest focus distance), `good` (`GOOD_DETAIL_PX_PER_MM = 20`), `far` (text "move closer: about N cm gives about M px/mm", with N = `CLOSER_FACTOR` 1.1 × the closest focus distance), and `unknown` (no data, no value yet, or an uncalibrated lens).
  - Rounding: whole cm from 10 cm, one decimal below; detail with one decimal.
  - `PhoneStatusReport` extends `CameraStatus` flat with the derived values.
- `phone_status` returns a `PhoneStatusReport`. The tool description tells the agent to tell the user to move the phone when the advice is `far` or `too_close`. It stays a `CameraStatus`, so the monitor and the page read it as before.
- `bench_start`: `BenchResult.focus` after the phone step.
- **Fixes of round 15**, found here:
  - `bench_start` and `bench_stop` give the page URL of the primary on a secondary (`Monitor.page_url`).
  - `bench_stop` stops only this server's own page.
- **Page**:
  - `EventBus.update_phone()` computes `PhoneState.focus` for each new status, so the math stays on the server. The status poll (1 s, and only on a change) brings the updates, with no extra load.
  - The phone panel has a line "Distance" with an advice line. The full-screen live view bar shows the same text.
  - The format is "≈ 29 cm · ~9 px/mm · focused": "≈" for an approximate calibration, and no cm for an uncalibrated lens.
  - The color follows the advice (good green, far amber, too close red). The advice text shows for `far` and `too_close`.
  - The agents do not read the page (cockpit rule); `phone_status` gives them the same values.
- `scripts/fake_phone.py`:
  - `focus` and `optics` with the contract example values (3.41 dpt, focused, approximate, closest 10 dpt; 6.07 mm, 9.14 mm, 4080 px).
  - `--focus-diopters` for the start distance, and the test-only route `POST /fake/focus {"distance_diopters": x}` (outside `/v1`, not part of the contract) to move the phone.
  - `--no-preview` (an old app) also drops `focus` and `optics`.
- `scripts/qa_contract.py`: the status must have `focus` (null, or the four fields with the allowed enum values and numbers ≥ 0) and `optics` (three positive values; the width is an integer).
- `mcp/README.md`: the tools table and "Phone distance".

### Tests

- `mcp/tests/test_focus.py` (8 tests):
  - The known values: 3.41 dpt, 6.07 mm, 9.14 mm, and 4080 px give about 29.3 cm and 9.3 px/mm (9.24; rounded 9.2), and 10 cm gives about 27 px/mm.
  - The advice text at 29 cm is "move closer: about 11 cm gives about 24.6 px/mm". At 10 cm the advice is good, and at 6.7 cm too close.
  - Uncalibrated gives no cm. Infinity gives far. No value gives unknown. Fixed focus is never too close.
  - An old app without the fields, and unknown enum values.
  - The page state report, and the `phone_status` result (29 cm, 9.2 px/mm, far, with the camera status fields).
- `test_lazy.py`: the `bench_start` result has a focus report.
- `scripts/qa_contract.py --strict` against the fake phone (through `test_preview_sync.py`) passes with the new checks.
- Playwright (headless Firefox, fake phone, eager server on port 18890, `--webcam /dev/video99`, temporary state and runtime folders):
  - After connect: "≈ 29 cm · ~9 px/mm · focused", class `advice-far`, and the advice "move closer: about 11 cm gives about 24.6 px/mm".
  - After `POST /fake/focus 10`: "≈ 10 cm · ~27 px/mm · focused", `advice-good`, no advice line.
  - After 15 dpt: "≈ 6.7 cm · ~41 px/mm · focused", `advice-too_close`, and "too close: the lens focuses from about 10 cm; move back".
  - In full screen, the bar showed the same text and class.
  - The page followed each move within the 1 s status poll.
- `uv run pytest`: 254 passed, 1 skipped. `uv run ruff check`, `ruff format --check`, and `prek run --files` on my files: pass.

### Open items

- I did not test with the real phone app: dd-android adds `focus` and `optics` now. With the new app, run `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict`, and check the distance with a ruler once. The thin-lens estimate and an `approximate` calibration can be off by some cm.
- The detail value is for the snapshot at zoom 1. Zoom crops, so the snapshot gets no more pixels per mm; moving the phone closer does.
