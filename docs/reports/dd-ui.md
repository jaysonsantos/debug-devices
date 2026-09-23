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
