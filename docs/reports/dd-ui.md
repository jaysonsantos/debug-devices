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
- Real stdio run with `mcp.ClientSession` and `stdio_client`: `uv run --directory <repo> debug-devices-mcp --adb-serial 0a1b2c3d`. `WAYLAND_DISPLAY` and `DISPLAY` were removed from the environment.
  - The server wrote `monitor window: http://127.0.0.1:18766/`. Firefox opened the page in a new window.
  - `phone_connect`: OK (101 ms). scrcpy started with the title `debug-devices: phone 0a1b2c3d`.
  - `phone_status`: OK (19 ms). `webcam_snapshot`: OK (2955 ms for the first frame after the stream start, 1920x1080).
  - `curl -sN http://127.0.0.1:18766/api/events` showed the `phone` and `call` events. `/api/state` listed all calls with source, status, duration, and the webcam frame image. The state did not contain the key.
  - I took a screenshot with `spectacle -b -n -o <file>` (it needs `WAYLAND_DISPLAY=wayland-0`) and looked at it. It showed the live webcam, the phone panel (serial `0a1b2c3d`, scrcpy "window open", zoom 1.00x (1–10)), and the settings. The KDE task bar showed the scrcpy window.
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
- Manual test with the phone: the server on 0a1b2c3d sent High profile H.264 (590x1280, `avc1.640020`). The parser found 65 access units. It gave the same result for the whole data and for chunks of 1 to 50 bytes.
- Real stdio run: `debug-devices-mcp --adb-serial 0a1b2c3d --ui-port 18820`, with a separate `XDG_STATE_HOME`.
  - `phone_connect` returned OK, and the screen state became `streaming`.
  - `curl /api/phone/screen` returned the config message `{"codec":"avc1.640020"}` and then a key frame.
  - Screenshot of the page (`spectacle -b -n -a`): the phone panel showed the live phone screen (the camera app) on the canvas. The state was "Screen: streaming".
- After a normal exit, no forward and no server process of that run stayed on the phone.

### Bugs found in the real tests, and fixes

- The scrcpy log directory did not exist in a new state directory. The session failed before it streamed. Fix: create the directory.
- A failure after `adb forward` left the forward on the phone, one per retry. Fix: every step after the forward is in the `try` block that removes it. A new test covers this.
- At a normal MCP exit, `adb forward --remove` did not run. The MCP shutdown cancels the lifespan, so the awaits in the cleanup stopped. Fix: `Monitor.stop()` runs the cleanup in `anyio.move_on_after(15, shield=True)`.

### Note: one dd-monitor forward removed by mistake

To remove the leaked forwards, I removed all `localabstract:scrcpy_*` forwards on 0a1b2c3d two times. By then, the user had restarted dd-monitor (18:58:32) with the new code. My second cleanup also removed the forward of the dd-monitor stream (`scrcpy_67ba812c`). The open connection stays, and the stream still sent data afterwards (about 290 KB in 3 seconds). If that connection breaks, the stream starts again after 3 seconds with a new forward.

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

## Round 17: in-sensor zoom switch (MCP server and monitor page)

### What I did

- `phone_api.py`:
  - `InSensorZoom` (`off`, `on`, `unsupported`, `fallback`; an unknown value reads as `off`). `CameraStatus.in_sensor_zoom` is optional; None means an app from before the endpoint.
  - New `CameraSettingsRequest` and `PhoneClient.camera()` for `POST /v1/camera`. A 404 becomes `CameraSettingsNotSupportedError`: "the phone app has no /v1/camera; update the phone app to use the in-sensor zoom".
- `camera_choice.py` (new):
  - `InSensorZoomChoice` persists the user's choice in `ui-settings.json` (`in_sensor_zoom`, default off), like the flips. It also works with `--no-ui`.
  - `InSensorZoomSync` sends the choice when a status clearly differs: choice on and status `off` (an app restart), or choice off and status `on`. It never sends for `unsupported` or `fallback`, because each request rebinds the camera and stops the preview for about 1 s. After a 404, it warns one time and stops until the next `phone_connect`.
- `server.py`:
  - `Services.sync_phone(status)` runs the preview flip sync and this sync. `connect_phone` (so also `bench_start`), `phone_status`, and the monitor status poll use it. `reset_phone_syncs()` runs at each `phone_connect`.
  - New tool `phone_in_sensor_zoom(enabled)`: it saves the choice, sends it, and returns the status report (a `CameraStatus` with the distance values). The description is the text of the brief. An old app gives a `ToolError` with "update the phone app".
- `instructions.py`, evidence rule 6: "When the user wants more detail without moving the phone, you may turn on phone_in_sensor_zoom and use a zoom of 2x-4x: on phones that support it, this gives real extra detail from a sensor crop. It is not optical zoom. Still take a fresh phone_snapshot after the change." `test_board_marking.py` checks the new phrases.
- `focus.py`: `FocusReport.sensor_zoom_boost` is true when the in-sensor zoom is `on` and the zoom is 2x or more (`SENSOR_ZOOM_MIN_RATIO`). The detail number stays the thin-lens estimate; no number is made up.
- Monitor and page:
  - `PhoneState.in_sensor_zoom_choice`. `phone_in_sensor_zoom` counts as a status tool, so the panel updates. `update_settings()` keeps the choice, as it does the flips.
  - `POST /api/phone/in-sensor-zoom {"enabled": true}` runs the tool (source `ui`). The body is a `StrictBool`: the test showed that pydantic's lax mode took `"yes"` as true.
  - The toggle "Sensor zoom (2x-4x detail)" is in the phone panel, and "Sensor zoom" in the full-screen live bar (`aria-pressed` shows the choice).
  - The panel line "Sensor zoom" shows on, off, "not on this phone", "failed; normal camera", or "not in this app". When the mode is on and the zoom is 2x-4x, it adds the hint "real sensor detail at this zoom (not optics)".
  - The distance line adds "+ sensor zoom" after the px/mm estimate.
- `scripts/fake_phone.py`:
  - `POST /v1/camera`: exactly `in_sensor_zoom`, a boolean; otherwise 400.
  - The status field, `off` at start. Zoom and torch stay.
  - `--in-sensor-zoom-unsupported` answers 200 with `unsupported`. The old-app mode (`--no-preview`) gives 404 and no field.
- `scripts/qa_contract.py`:
  - The status field must have an allowed value, and it is `off` after an app start.
  - New `check_camera`: true gives `on`, `unsupported`, or `fallback`, and `GET /v1/status` shows the same. Zoom and torch stay. False gives `off` or `unsupported`. Five bad bodies give 400.
  - `GET /v1/camera` gives 405.
- Docs: `mcp/README.md` (the tools table and "Sensor zoom"), `README.md` (the tool list).

### Tests

- `mcp/tests/test_in_sensor_zoom.py` (8 tests):
  - The client call, the old-app 404 error, and the old status without the field.
  - The tool sets the choice, saves it, and a new start reads it. An old app gives a tool error with "update the phone app".
  - `phone_connect` sends the choice. The same state sends nothing. After an app restart (status `off`), `phone_status` sends it again.
  - With `unsupported` and with `fallback`, the server sends one time only (at connect), never again.
  - Choice off and a phone that is `on`: the server sends false.
  - The "+ sensor zoom" flag only with `on` and a zoom of 2x or more.
  - The page route runs the tool, a non-boolean gives 400, and a page save of other settings keeps the choice.
- `scripts/qa_contract.py --strict` against the fake phone: 20/20 in the normal mode (`in_sensor_zoom true gives 'on'`) and in `--in-sensor-zoom-unsupported` (`'unsupported'`).
- Playwright (headless Firefox, fake phone, eager server on port 18890, `--webcam /dev/video99`, temporary folders):
  - Supported: the panel toggle gives "on" (`aria-pressed` true). At 2.25x, the hint shows, and the line reads "≈ 29 cm · ~9 px/mm + sensor zoom · focused". The full-screen bar toggle sets it back to "off", and the "+ sensor zoom" goes away.
  - Unsupported: "not on this phone", with no hint and no "+ sensor zoom".
- `uv run pytest`: 262 passed, 1 skipped. `uv run ruff check`, `ruff format --check`, and `prek run --files` on my files: pass.

### Open items

- I did not test with the real phone app (dd-android builds `/v1/camera` now). With the new app, run `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict` (the camera check turns the mode on and off one time, so the preview stops twice for about 1 s), then compare a snapshot at 2x with the mode on and off.
- In the unsupported mode, the button stays pressed (it shows the choice), and the line says "not on this phone".

## Round 18: phone screen MJPEG fallback, and page self-reload

### Part 1: MJPEG fallback for browsers without H.264 in WebCodecs

Problem: in Camoufox (Firefox 152 based), the live phone view showed "Decoder error: Operation is not supported". WebCodecs is there, but it cannot decode H.264.

- Page (`app.js`, region `phone screen`):
  - Before the first frame, `checkCodec()` asks `VideoDecoder.isConfigSupported({codec, optimizeForLatency: true})` with the codec string of the stream.
  - The page switches to the fallback when the answer is not supported, when the browser has no `VideoDecoder`, or on a later decoder error (`error` callback). It then stays on the fallback until a reload.
  - `startFallback()` stops the H.264 fetch (`AbortController`), shows the label "MJPEG fallback" (the title says why), and loads `/api/phone/screen.mjpg` into a hidden `<img>`. The image is 1 px and transparent, not `display: none`, so the browser keeps decoding the frames.
  - Every 100 ms, the page draws the current image into the same canvas as the H.264 path (`drawScreenSource()`). So rotation, full screen, the layout, the wheel and arrow camera zoom, and the Sensor zoom button work the same way.
  - The H.264 path stays for browsers that can decode it.
- Server:
  - `screen_mjpeg.py` (new): `ScreenTranscoder`. One ffmpeg (`-f h264 -i pipe:0`, `fps=10`, width at most 1280, `-q:v 5`, `mpjpeg` on stdout) gets the key and delta frames of a phone screen subscription on stdin. The subscription starts at a key frame, and a resync gives a key frame again.
  - It starts with the first fallback viewer and stops after the last one leaves (`viewer()` counts them).
  - `GET /api/phone/screen.mjpg` (multipart, like the webcam stream, in `routes/screen.py`) has the same LocalOnly checks and the same clean end at a page stop (`until_closing`). It returns 404 without the phone screen.
- **Bug found and fixed in `until_closing()` (round 10):** when a client left, uvicorn cancelled the response task. The helper then called `aclose()` on the source generator while a read still ran inside it. That fails, so the generator's cleanup never ran. Effects:
  - The fallback ffmpeg did not stop after the last viewer.
  - The webcam viewer count never went down after a page left the live view, so the webcam idle release could not stop the camera.
  - Fix: cancel and await the pending read first, then close the source. A new test covers a cancelled reader.

### Part 2: the page reloads itself after a server restart

- `ui/version.py`: `code_version()` is a hash of the page files (`ui/static/`) and a new UUID v7 per server start (16 hex chars). `Monitor.code_version` is computed at start.
- `/api/state` has `version`. The event stream sends `event: version` as its first message.
- The page keeps the version from `/api/state`. When a reconnect brings another version (a `scripts/dev-monitor.sh` or `--dev-reload` restart, or new page files), it calls `location.reload()`. It does not keep the full screen state.

### Tests

- `mcp/tests/test_screen_mjpeg.py` (real ffmpeg; skipped without ffmpeg):
  - ffmpeg makes a 2 s H.264 test clip (`testsrc`, libx264). It is split into access units like the phone screen, and the test publishes them in a loop.
  - `/api/phone/screen.mjpg` returns `multipart/x-mixed-replace` with JPEG frames. ffmpeg runs only while the client reads, and it stops after the client leaves (viewers 0).
  - Also: the transcode arguments (10 fps, width 1280), and 404 without the phone screen.
- `test_ui_shutdown.py`: a cancelled reader of `until_closing` closes its source. `test_ui_app.py`: the first SSE message is the version.
- Browser checks (script in my scratch folder; an in-process monitor with a looping H.264 test clip):
  - Camoufox (`uv run --with camoufox`, Firefox/152.0): the page chose the fallback with the reason "avc1.64000c is not supported by this browser". The label "MJPEG fallback" showed. The canvas had pixels at 320x240. Full screen on the fallback view worked (`fullscreenElement` = `phone-view`).
  - Playwright Firefox: the H.264 path ran (no fallback label), and the canvas had pixels at 320x240.
  - Reload: with the page open in Playwright Firefox, the script stopped the page, changed `code_version`, and started the page on the same port. The page reloaded itself (a window marker was gone), and `/api/state` showed the new version.
- `uv run pytest`: 266 passed, 1 skipped. `uv run ruff check`, `ruff format --check`, and `prek run --files` on my files: pass.

### Notes

- `camoufox` pulls another Playwright version than the one with the installed Firefox build (1543), so the two checks run in two separate `uv run --with ...` calls.
- The fallback costs one ffmpeg that decodes and encodes, but only while a fallback page is open. H.264 browsers do not start it.
- The fallback ffmpeg starts when the fallback page opens, also before `phone_connect`. It then waits for the first key frame and uses almost no CPU.

## Round 19: sensor zoom, camera zoom, and a new snapshot in the full-screen snapshot bar

### What I did

The full-screen snapshot bar (`#snapshot-view .fs-controls`) had only Flip H and Flip V. It now has:

- **Camera zoom `−` / `+`** with the zoom ratio text. They send `phone_zoom` steps through `liveZoomStep()`, so the same rate limit applies as in the live view (one step per 150 ms, none while a request runs). `showLiveZoom()` now updates every `[data-zoom-ratio]` label: the live bar and this bar show the same value. Each phone update also refreshes the labels.
- **Sensor zoom**: the same `data-isz` toggle, so the one choice drives all three toggles (panel, live bar, snapshot bar), with the same `aria-pressed`. During the request (the camera rebinds), all three toggles are disabled, not only the clicked one.
- **New snapshot**, and the key `s` in the full-screen snapshot view (not in form fields): `phone_snapshot` through the instrumented `call_tool`. The panel button uses the same function (`takeSnapshot()`, with no second request while one runs). The new snapshot number reloads the image at full size in full screen and resets the digital zoom, as before.
- Flip H and Flip V stay. The digital wheel zoom and pan stay.
- Each button has a tooltip that says what it does. They are normal buttons, so the keyboard can focus them.

### Tests

Playwright (headless Firefox, fake phone, eager server on port 18890, `--webcam /dev/video99`), in the full-screen snapshot view:

- The bar had Flip H, Flip V, `−`, `+`, Sensor zoom, and New snapshot, each with its tooltip. The ratio showed "1.0x".
- The bar's Sensor zoom: all three toggles went to `aria-pressed="true"` and were enabled again afterwards. The state showed "on".
- `+`, `+`, `−`: the ratio went 1.5x, 2.3x, 1.5x. The fake phone showed zoom 1.5, and the log had three `phone_zoom` calls.
- With a digital zoom active, "New snapshot" loaded a new image with `full=true` and reset the digital zoom (no transform). The key `s` loaded another new image. Flip H still worked, and the view stayed in full screen.
- The log: `phone_connect`, `phone_snapshot`, `phone_in_sensor_zoom`, `phone_zoom` ×3, `phone_snapshot` ×2, `phone_snapshot_orientation`.
- `uv run pytest`: 266 passed, 1 skipped (no Python change). `uv run ruff check` and `prek run --files` on the page files: pass.

### Note

In full screen, an error of a bar button shows in the phone panel (under the full-screen view), not in the bar.

## Round 18, addendum: H.264 first, the fallback hint, and the Camoufox library fix

The orchestrator's note: the user prefers H.264 (smoother). Camoufox 152 has no H.264 only because this system has libavcodec 63. With FFmpeg 7 (`libavcodec.so.61`) on `LD_LIBRARY_PATH`, it decodes H.264 in WebCodecs.

- The page already uses WebCodecs H.264 first. It falls back only when `VideoDecoder` is missing, when `isConfigSupported()` says no, or after a decoder error. No change to that logic.
- The label in the view now says "No H.264 decoder in this browser: MJPEG fallback". The title still gives the exact reason.
- `mcp/README.md`: a note on the Camoufox fix (start it with `LD_LIBRARY_PATH=$HOME/.cache/debug-devices/ffmpeg7-lib-lib/lib`).
- Camoufox checks, both with an in-process monitor and a looping H.264 test clip. The library path goes only to the browser, through Camoufox's `env`.
  - Plain Camoufox (Firefox/152.0): fallback, reason "avc1.64000c is not supported by this browser", the new label text, canvas pixels at 320x240, full screen works.
  - Camoufox with `LD_LIBRARY_PATH=$HOME/.cache/debug-devices/ffmpeg7-lib-lib/lib`: the H.264 path (no fallback label), canvas pixels at 320x240, full screen works.
- The check script also pressed ArrowUp in full screen. The in-process test monitor has no MCP server, so that press logged "the monitor is not attached to an MCP server". This comes from the test harness, not the product. The zoom keys were checked in round 14 with a real server.

## Round 20: the phone panel is the main panel; the crop preview goes to the log

The user's request: "the phone view is more important than the multimeter ... swap them but keep the features". Addition: "crop preview is not needed, it can go to the log".

### What I did

- **Panel order.** The Phone panel is now the first section in the page and has the large left column (`2fr`). The Webcam panel is in the right column, and the Settings panel is below it. The activity log stays at the bottom, full width. Below a window width of 1000 px, the panels are in one column: Phone, Webcam, Settings, Activity. The grid rows are `auto 1fr auto`, so the Settings panel stays right below the Webcam panel, also when the phone panel is tall.
- **The phone panel inside.** When the panel is 720 px wide or more (a container query), it has two columns: the live view on the left (`3fr`), and the facts, the controls, and the snapshot on the right (`2fr`). A narrower panel puts them in one column, live view first.
- **The live view size.** The canvas is as large as its column and the window height allow: `width: min(100cqw, (100vh − 96px) × ratio)`, `height: auto`. `drawScreenSource()` sets `--screen-ratio` (the width / height of the drawn frame, after the view rotation), so the canvas keeps the aspect ratio and shows the full frame. The `#phone-view` box shrinks to the canvas (`fit-content`), so the full-screen button stays on the picture corner. Full screen uses the old rules (`100vw` × `100vh`, `object-fit: contain`).
- **Crop preview removed.** The canvas `#crop-preview`, its label, `drawPreview()`, and its 500 ms timer are gone. The crop hint says: "The Activity log shows each new area."
- **Crop changes in the log.** `PUT /api/settings` and `DELETE /api/settings/crop` call `Monitor.log_crop_change(before)`. When the effective crop changed, the monitor adds one log row: tool `webcam_crop`, source `ui`, argument and summary `x,y,w,h` (or "none (the whole frame)" after Clear crop), and the image "new crop area": the crop of the latest webcam frame, scaled to at most 320 px (`defaults.CROP_PREVIEW_MAX_SIDE`). The page saves the crop once at the end of a drag, so a drag gives one row, not one per mouse move. A save with the same crop (for example Save of the other settings) adds no row. With no webcam frame, the row says "(no webcam frame for a preview)" and has no image. `multimeter_read` rows keep the exact image that went to the model.
- Every id and data attribute stays, so the tests and `scripts/record_demo.py` need no change. `mcp/README.md` describes the new layout and the `webcam_crop` rows.

### Tests

- New in `mcp/tests/test_ui_app.py`: a crop change, the same crop again, and Clear crop give exactly two `webcam_crop` rows (source `ui`), each with one image; the first image is 20x10 px, the crop size. A crop change without a webcam frame gives a row with the "no webcam frame" note and no image.
- Playwright (headless Firefox; an in-process monitor with a looping 360x800 H.264 test clip as the phone screen and plain red frames as the webcam; no real devices, no people in frames). Screenshots are in my scratch folder only.
  - 1920x1080: Phone panel 1251 px wide at the left, Webcam 625 px at the right, Settings right below it. The canvas is 443x984, ratio 0.45, the same as the frame.
  - 1366x768: Phone 881 px, Webcam 441 px. The canvas is 302x672 and fits the window height.
  - 900x900: one column: Phone, Webcam, Settings, Activity. The canvas is 362x804.
  - At all three sizes: no horizontal page scroll, `#crop-preview` is gone, the full-screen button is on the picture, and a double-click on the live view opens full screen with the canvas at the full screen size.
  - A crop drag on the webcam gave one log row `webcam_crop 61,61,309,185` with one image. Clear crop gave the second row.
- `uv run pytest`: 268 passed, 1 skipped. `uv run ruff check`, `ruff format --check`, and `prek run --files` on the changed files: pass.

## Round 21: click to focus on the live phone view, and the `phone_focus` tool

The user's request: "if I click on the image of the phone, can you try to focus on that area?" The contract was already changed (`POST /v1/focus` with `screen_x`/`screen_y` or `snapshot_x`/`snapshot_y`). I did the panel swap (round 20) first, as the orchestrator asked, and then finished this task.

### What I did

- **Client** (`phone_api.py`): `ScreenFocusRequest`, `SnapshotFocusRequest` (each value in [0, 1]), and `PhoneClient.focus()`. A 404 raises `FocusNotSupportedError`: "the phone app has no /v1/focus; update the phone app to focus on a point". `constants.phone.PATH_FOCUS`.
- **MCP tool** `phone_focus(x, y, source="snapshot" | "screen")` (`server.py`, in the new `register_camera_tools()` with `phone_in_sensor_zoom`; the phone tool function had too many statements).
  - `phone_snapshot` now keeps the geometry of the image that the agent got: its width and height after the scaling, and the flips (`Services.last_snapshot`).
  - `source` "snapshot": `snapshot_focus_request()` (`focus.py`) divides by that width and height, then undoes the flips (`1 − x` for Flip H, `1 − y` for Flip V). The result is the point on the true-orientation snapshot of the phone.
  - Errors (tool errors): no `phone_snapshot` yet ("take a phone_snapshot first"), a point outside the last snapshot, a `screen` value outside [0, 1], an old app ("update the phone app"), and the 400 message of the app (for example "outside the preview").
  - The result is `PhoneStatusReport` (the status with the distance values and `focus_state`). The description says: take a fresh `phone_snapshot` after the focus settles (about 1 s).
  - `phone_focus` is in `PHONE_STATUS_TOOLS`, so the page gets the new status.
- **Evidence rule 6** has a new sentence: "When the relevant area is blurry, focus on it with phone_focus (pixels in your last phone_snapshot), then take a fresh phone_snapshot." The phrase test has "focus on it with phone_focus".
- **Page route** `POST /api/phone/focus {screen_x, screen_y}` (strict floats in [0, 1], otherwise 400). It runs `phone_focus` with `source` "screen" through the instrumented tool call, so the log shows a `ui` row.
- **Page click** (`app.js`, region "click to focus"):
  - One click on `#phone-screen` (the canvas that shows the H.264 frames and also the MJPEG fallback, in the panel and in full screen) starts a timer of `FOCUS_CLICK_DELAY_MS` = 250 ms. The second click of a double-click (`event.detail > 1`) and the `dblclick` event cancel it, so a double-click only toggles full screen.
  - `screenPoint()` undoes the CSS scaling and the letterbox (`object-fit: contain`: the scale is the smaller of the two axis scales, and the content is centered), then turns the point back by the view rotation around the box center (the inverse of `drawScreenSource()`). It then divides by the frame size, which `drawScreenSource()` now stores. A click on the letterbox gives no point and sends nothing.
  - A yellow ring fades at the click point in 1 s (`FOCUS_RING_MS`, CSS animation). The label `#focus-tap` shows "focusing…", then the focus state from the status poll ("focused", "could not focus"), or the error without the SDK prefix (for example "phone API error 400 bad_request: outside the preview"). It hides 3 s after the last change.
  - The full-screen button tooltip says: "A click on the picture focuses there."
- **Fake phone**: `POST /v1/focus` with the 400 rules of the contract (exactly one pair, numbers in [0, 1]; not a string, null, or boolean). A screen point outside the fake preview (above 0.1 or below 0.8 of the screen height) gives 400 "outside the preview". The state is `scanning` for 0.3 s, then `focused`. An old app (`--no-preview`) answers 404. `FakeCamera.last_focus` keeps the last point for tests.
- **`qa_contract.py`**: `check_focus` sends a snapshot center and corner, and a screen center; each must give a status with a known `focus.state`. It also checks ten bad bodies (400) and adds `GET /v1/focus` to the 405 checks. `--strict` also checks the screen edges: 200, or 400 with "outside the preview" (`docs/qa.md`).
- `mcp/README.md`: the `phone_focus` tool row and the "Click to focus" page part.

### Not done: a click on the full-screen snapshot

The brief made it optional ("only if cheap"). I did not do it. The snapshot view already uses click-drag for the pan and the wheel for the digital zoom, and a double-click for full screen. A focus click needs the same click-versus-drag and click-versus-double-click logic again, plus the inverse of the pan and zoom transform and of the flips. Also, the focus changes the live camera, not the photo on the screen: the user must take a new snapshot to see it. Click to focus on the live view gives the same result with direct feedback.

### Tests

- New `mcp/tests/test_focus_tap.py` (10 tests):
  - The mapping with the four flip combinations, the edges, and points outside the image.
  - The tool: no snapshot yet gives an error. With Flip H and a 4000x3000 photo (scaled for the agent), a point at 1/4 of the image width gives `snapshot_x` 0.75 and `snapshot_y` 0.5. A point outside the image, and a screen value of 30, send nothing. The screen source sends the point as it is. The old app gives "update the phone app", and a 400 from the app gives "outside the preview".
  - The page route: 200 with the status, one `phone_focus` row with source `ui`, and 400 for a string, 1.2, or a missing field. A 400 of the app gives 502 with the message.
- Playwright (headless Firefox, an in-process monitor with the real tools, the httpx fake phone, and a looping 360x800 H.264 test clip; no devices):
  - Panel click at (0.25, 0.75) of the picture: nothing after 100 ms, then `screen_x` 0.249, `screen_y` 0.75. The ring and "focusing…" showed. The ring was gone after 1 s, and the label after 3 s.
  - A double-click opened full screen and sent no focus. A full-screen click at (0.9, 0.4) sent 0.899, 0.4. A click on the black letterbox sent nothing. A second double-click left full screen and sent nothing.
  - View rotation 90°: a click at (0.2, 0.3) of the canvas sent 0.298, 0.8 (expected 0.3, 0.8). At 180°: 0.801, 0.701 (expected 0.8, 0.7).
  - A 400 "outside the preview" showed as "phone API error 400 bad_request: outside the preview" (checked before I removed the SDK prefix from the label).
  - The log had five `phone_focus` rows with source `ui`: four ok and one error.
- `qa_contract.py --strict` against `scripts/fake_phone.py`: 21/21 checks passed, with `PASS focus`. `fake_phone.py --self-check`: passed. `--no-preview`: `POST /v1/focus` gives 404.
- `uv run pytest`: 278 passed, 1 skipped. `uv run ruff check` and `ruff format --check`: pass. `prek run --files` on all changed files: pass.

### Notes

- Headless Firefox in full screen: a mouse click near the top edge opens the browser toolbar. The click then goes to the browser, not to the page, and full screen ends. The check clicks lower. A control check with the full-screen snapshot view showed that a normal click there keeps full screen. This behavior is from the browser, not from the page code.
- The real app part is dd-android's task. I did not test against the phone.

## Round 22: `phone_highlight`, green boxes on the page, and scene-change detection

The brief: the agent draws green boxes around the parts that it found, on the phone and on the page. The addition from the user: "somehow the model has to know about the rotation of the board if I changed something". A box or a photo registration is only valid for the scene that the agent saw.

### What I did: highlight boxes

- **Client** (`phone_api.py`): `OverlayBox` (a rectangle on the true-orientation snapshot from 0 to 1, inside the image, label at most 32 characters), `OverlayRequest` (at most 8 boxes), and `PhoneClient.overlay()`. A 404 raises `OverlayNotSupportedError`: "update the phone app to show highlight boxes". `CameraStatus.overlay_boxes` (None for an older app).
- **Mapping** (`highlight.py`): `PixelBox` is a box in pixels of the last `phone_snapshot` image that the agent got. `overlay_box()` cuts the box at the image edge, divides by the image size, and mirrors it for the flips: `x = 1 − x − width` for Flip H, and the same for y with Flip V. A box fully outside the image is refused. `scale_boxes()` converts boxes of the same photo at another size. It refuses another aspect ratio (more than 2 % difference). `draw_boxes()` draws the green boxes and the labels on a copy.
- **Tool** `phone_highlight(boxes, clear)` has the description from the brief, and it also says how the pixels are defined. `phone_snapshot` now also keeps the scaled image that the agent got. The tool sends the boxes, keeps them in `Services.highlights`, and returns `HighlightResult`: the count, the true-orientation boxes, `overlay_boxes` from the phone, and a note that the boxes are estimates. The result also has the annotated copy of the last snapshot as an image. `clear: true` sends an empty list. Both `boxes` and `clear`, neither of them, 9 boxes, or no snapshot yet give a tool error.
- **`board_locate_in_photo(highlight=true)`**: it maps the boardview box of each located part (in the photo, on the registered side) through the registration homography, takes the bounding rectangle (at least 8 px), and sends up to 8 boxes with the part name as label through the same code. The registered photo can be the last snapshot at another size, but it must have the same shape. The result has `highlight` (the `phone_highlight` result) and the annotated snapshot.
- **Evidence rule 4** has a new sentence: "After you identify a part in a photo, you may call phone_highlight so the user sees it: say that the box is your estimate, and clear it when done."
- **Page**:
  - `#snapshot-boxes` draws the boxes and labels over the snapshot. `drawHighlights()` takes the image rectangle after the transform, removes the panel border, and places each box in view pixels. It includes the `object-fit: contain` letterbox and the snapshot flips (the stored boxes are in the true orientation). The labels keep their size under the wheel zoom.
  - The boxes are drawn again after an image load, a zoom or pan, a size change (`ResizeObserver`), full screen, and each phone update.
  - "Clear highlights" is in the panel flip row and in the full-screen snapshot bar. It is disabled with no boxes. It calls `POST /api/phone/highlight/clear`, which runs `phone_highlight` with `clear: true`.
  - The log row of `phone_highlight` shows the annotated image.
  - The monitor takes the boxes from the result of `phone_highlight` and of `board_locate_in_photo` (`PhoneState.highlights`).
- **Fake phone**: `POST /v1/overlay` with the 400 rules (exactly `boxes`; at most 8; each box with exactly the five fields, numbers, width and height > 0, inside the image, label a string of at most 32 characters). `overlay_boxes` is in the status. An old app (`--no-preview`) answers 404 and sends no `overlay_boxes`.
- **`qa_contract.py`**: `overlay_boxes` is a required status field (int). `check_overlay` sends 3 boxes (one at the image edge, one with a 32-character label), then 8, then nine bad bodies. It checks that a refused body keeps the boxes, and that an empty list gives 0. `GET /v1/overlay` must give 405. `--after-start` also checks `overlay_boxes == 0`.

### What I did: scene-change detection

- **`scene.py`**:
  - `SceneState` is shared by the tools and the watcher. It has `changed_at`, `snapshot_taken()` (a new reference), `own_command()` (a new reference after `settle` = 1.5 s), `guard()` (a tool error with the message from the brief), and `mark_changed()` (tells the listeners).
  - `prepare()` makes the compared frame: a JPEG draft decode in grayscale, the crop to the preview area (x 5–95 %, y 20–95 %: the app status text at the top left stays out), 64 px wide, a Gaussian blur, and the mean brightness removed (so auto exposure does not count).
  - `compare()` gives the mean absolute difference and the best global shift up to ±4 px. `is_changed()` is true when the difference is above 12, or when a shift of 2 px or more explains the difference (the shifted difference is below 0.6 × the plain one).
  - A change must last 2 compared frames in a row (a hand that passes does not count). All thresholds are named fields of `SceneOptions`.
- **Frames**: `SceneWatcher` compares one frame each 0.5 s. `screen_feed()` uses the page's MJPEG fallback decoder while it runs. Otherwise it runs its own small `ScreenTranscoder` (2 fps, 160 px wide; `TranscodeOptions` is new in `screen_mjpeg.py`). It switches when the fallback starts or stops (`frames_while()`). The watcher starts at `phone_connect` when the phone screen runs, and it stops with `stop_phone` (also `bench_stop`) and at the monitor stop.
- **Our own commands** (`Services.camera_command()`, before and after the request): `phone_zoom`, `phone_torch`, `phone_rotation`, the preview flips of `phone_snapshot_orientation`, `phone_in_sensor_zoom`, `phone_focus`, and `phone_highlight` (the boxes are in the stream). After them, the watcher takes a new reference after 1.5 s.
- **On a change**: the server removes the phone boxes, marks every photo registration as stale, and sets `scene_changed`. The monitor removes the page boxes and sets `scene_changed_at`. The page shows "Scene changed: highlights cleared" for 4 s, over the live view and the snapshot.
- **Refusals** until a fresh `phone_snapshot`: `phone_highlight`, `phone_focus` with `source` "snapshot", `board_locate_in_photo`, and also `board_register_photo` (its pixel pairs come from the old photo). A stale registration stays refused after the new snapshot: "call board_register_photo again". `phone_focus` with `source` "screen" still works.
- `phone_status` returns `scene_changed` and `scene_changed_at`.
- **Evidence rule 6** has a new sentence: "After the user moves or turns the board, take a fresh phone_snapshot before you point at anything; never reuse boxes or positions from an older photo."

### Tests

- `mcp/tests/test_highlight.py` (13 tests):
  - The mapping with the four flip combinations, cutting at the edge, a box outside the image, the scaling and the shape check, and the green pixels of the annotated image.
  - The tool: a 4000x3000 photo with Flip H (the agent sees 1568x1176). The top-left quarter goes to the phone as (0.75, 0, 0.25, 0.25), and the annotated image is 1568x1176 with green at the box edge. It also checks 9 boxes, both and neither argument, clear, and the old app.
  - The scene change: after a change, the phone boxes are cleared, `phone_status` shows `scene_changed`, and `phone_highlight` and snapshot-pixel `phone_focus` are refused. A new snapshot allows them again. A zoom asks for a new reference.
  - The board: the registration of `markings.json`, then `board_locate_in_photo` with `highlight` sends a U7301 box centered on its located center (within 2 px), with the width of its boardview box. After a change it is refused, and after a new snapshot the stale registration is still refused.
  - The page route: the boxes go to the page state, Clear highlights empties them on the phone and the page, and a scene change clears them and sets `scene_changed_at`.
- `mcp/tests/test_scene.py` (12 tests) with a synthetic board texture as a 360x800 phone screen:
  - Not changed: still, sensor noise (σ 6), and 25 levels brighter.
  - Changed: moved 12 px (3 %) sideways, moved 40 px down, turned 10°, and turned 180°. The shift check finds a small shift.
  - The watcher: no reference before the first snapshot. One changed frame is ignored, and two in a row mark the change once. The state stays changed until a new snapshot. Frames during the settle time of our own command are ignored, and then a new reference is taken. `frames_while()` stops when asked.
- End to end (scratch script; real ffmpeg with the watcher's own decoder, and still H.264 clips of the synthetic board): no change in 5 s of a still scene. A move of 4 % was found after 2.1 s, and a turn of 12° after 2.0 s. After our own command, no false change came. The decoder stopped with the watcher.
- Playwright (headless Firefox, an in-process monitor with the real tools and the httpx fake phone):
  - The agent's box on the second quarter showed at (0.25, 0.248, 0.25, 0.251) of the panel picture. After Flip H it moved to x 0.501, the mirror position.
  - In full screen at zoom 1.56x, the box stayed at (0.25, 0.25, 0.25, 0.25) of the zoomed picture.
  - Clear highlights sent an empty list to the phone.
  - A scene change removed the boxes, showed the note, and hid it after 4 s. `phone_highlight` was then refused.
  - The log rows of `phone_highlight` had the annotated image.
- `qa_contract.py --strict` against `scripts/fake_phone.py`: 22/22 passed, with `PASS overlay`. `fake_phone.py --self-check`: passed.
- `uv run pytest`: 303 passed, 1 skipped. ruff check, ruff format, and `prek run --files` on the changed files: pass.

### Notes and limits

- Detection needs the page server and the phone screen stream. With `--no-ui` or `--no-phone-screen`, `scene_changed` stays false.
- The preview area crop is a fixed part of the screen. The app's preview fills the screen, and the status text is at the top left. If the app layout changes, `SceneOptions` must change.
- A move within 1.5 s after our own command becomes part of the new reference and is not found. A hand that stays over the board for 1 s or more counts as a change.
- The page snapshot tool (a user click) also resets the reference, because it is the same `phone_snapshot` tool.
- The real app part is dd-android's task. I did not test against the phone.

## Round 23: Board panel with part search and camera highlight

The user's request: "on the web UI there should be a search for a part, and then it tries to highlight on the board where that is".

### What I did

- **Board panel** (right column, between Webcam and Settings; in one column after Webcam). It has:
  - A path field with Open. It runs `board_open` through the instrumented tool call (source `ui`).
  - The open board: the file name, parts, and nets.
  - A note when another MCP server of this page opened a board (from the ingested `board_open` calls). Open then loads it here.
  - A search box with a `<datalist>` over all part names and net names (`GET /api/board/names`).
- **Search** (`POST /api/board/search`, `ui/board.py` `BoardPanel`). A part name or part glob searches a part; a net name or glob without such parts searches a net.
  - Part: `board_find_part` (limit 5). The panel shows the side, the position in mm, the pin count, the mfgcode, up to 6 nets, the other matches, and the prefix note. It also runs `board_render` with `crop_to_part`, the part highlighted, and the new `green` option (every highlight in the green of the phone boxes). The drawing is the image of that logged call.
  - Net: `board_find_net`. The panel shows the part count, the pin count, up to 12 parts, and the test points. It draws the side with most of the net's parts, with the net in green.
- **Camera highlight**: the panel uses the newest photo registration (`BoardSession.last_registration_id`). If it is valid for the board and the side, it runs `board_locate_in_photo` with `highlight: true`, so the green box shows on the phone live view (the phone overlay) and on the page snapshot. For a net, the boxes are up to 8 of its parts on the registered side. Otherwise the panel keeps the drawing and says why:
  - "register the photo first: 4 reference parts";
  - "the board moved: register again" (a stale registration, or the scene-change refusal of the tool);
  - "on the bottom side: not visible now";
  - "the registration is for another board: register again".
- **Register photo** (a `<details>` in the panel):
  1. Take a snapshot.
  2. Type a reference part and press Pick.
  3. Click its center on the snapshot, in the panel or in full screen. A click that moves more than 5 px is a pan, not a pick.
  4. Yellow marks with the part names show the picked points (they follow the zoom). Undo removes the last one.
  5. With 4 or more points, Register sends `POST /api/board/register`. The server converts the points (0 to 1 of the shown picture) to pixels of the last snapshot and runs `board_register_photo` with that size. It refuses when the flips changed after the snapshot. The panel shows the rms and max error in px, and it says when only 4 parts give an exact fit that is not checked.
- The monitor keeps the last `board_open` result (the page's or the agent's), and it reloads the panel after each finished `board_*` call.
- `board_render` has the new optional `green` argument (`RenderOptions.green`, `colors.GREEN`). The palette stays the default.
- The cockpit rule is unchanged: these are page routes for the user. Agents still use only the MCP tools. `mcp/README.md` has a "Board" part and the `green` argument.

### Tests

- New `mcp/tests/test_board_panel.py` (7 tests; the synthetic `markings.json` board with invented names, and the fake obv-dump runner):
  - Before a board: no summary, names give 409, and search gives 502. After Open: the summary and the names, and one `board_open` row with source `ui`.
  - Part search without a registration: the facts, the "register the photo first" message, and the drawing image of the `board_render` call with `green`.
  - A registration from 5 clicked points (converted from the known photo mapping): checked, max error below 1 px. Then U7301 is highlighted (the phone got one box "U7301"), and U7303 gives "on the bottom side: not visible now".
  - Net search: the parts and test points, and boxes only for parts on the registered side.
  - After a scene change: the registration is stale, the search says "the board moved: register again" and keeps the drawing. A new registration works again.
  - Register needs 4 points and a snapshot. A board of another server is offered.
- Playwright (headless Firefox; an in-process monitor with the real tools, the httpx fake phone, and the synthetic board):
  - Before Open, the search box is disabled. After Open, the panel showed "markings.json · 8 parts · 9 nets" and 17 autocomplete entries.
  - U7301: the facts, the green drawing, and "Camera: register the photo first: 4 reference parts".
  - Five parts picked by clicks on the snapshot (the last one in full screen): 5 marks. Register gave "error 0.75 px (max 1.14 px), checked".
  - U7301 again: "highlighted on the camera (top side)". The page box center was at (352.9, 644.5) px of the photo, and the boardview center maps to (354.0, 646.0).
  - U7303: "on the bottom side: not visible now". After a scene change: "the board moved: register again", and the registration shows "stale".
  - Net PP_SYN_1V0: 4 parts (6 pins) and test point TP9.
  - Every call in the log has source `ui`.
- `uv run pytest`: 310 passed, 1 skipped. `uv run ruff check`, `ruff format --check`, and `prek run --files` on the changed files: pass.

### Notes

- A double-click on the snapshot during Pick also places a point, because the first click picks. The hint says to use the full-screen button or `f`.
- The panel uses the board session of the MCP server of this page. A board that only another server opened needs Open here. The panel offers its path.
- The registration uses the size of the last snapshot. When the agent registers another photo size, the highlight scales the boxes if the shape is the same.

## Round 24: live tracking and the direction arrow (`phone_point_to`)

The user's request: "if I am looking for a component, it could show an arrow to which direction I should move if it is not in the view of the device". User decision D4: live tracking with image features. The phone is drained, so I built and tested everything with the fake phone and synthetic frames only.

### What I did

- **Dependency**: `opencv-python-headless>=4.12,<5` (4.14.0 is installed; `uv.lock` is updated). No nix change: uv manages the Python packages.
- **Live tracking** (`tracking.py`, `LiveTracker`):
  - At registration, it matches the snapshot with the current screen frame: ORB (3000 features), a ratio test (0.75), and a MAGSAC++ homography (3 px). Only the preview area of the frame counts (the app status text stays out). This gives `snapshot_to_frame`.
  - Each new frame is matched with the reference frame, or, when the view moved far, with the last good frame (chained). A match is trusted with at least 25 inliers, at least 30 % of the matches, a scale of 0.25–4, no mirror, and small perspective terms. All thresholds are named constants.
  - The map is `inv(snapshot_to_frame) @ motion @ snapshot_to_frame @ board_to_snapshot`. The snapshot and the preview crop around the same center, so this also follows a zoom.
  - After 3 failed frames in a row, the tracking is lost.
  - I compared settings on the synthetic tests. RANSAC at 480 px had errors up to 31 px. MAGSAC with 3000 features at 720 px had at most 2.8 px. One tracked frame takes about 34 ms on this PC.
- **Frames**: `SceneState` now has `latest_frame` and frame listeners. The watcher gives about 4 frames per second to the listeners and still compares 2 per second for the scene check. Its own decoder is now 4 fps and 720 px wide (it was 2 fps and 160 px), because the features need the detail. It uses the page fallback decoder when that decoder runs.
- **Registration** (`board_register_photo`): the result has `tracking`, with "live tracking on: …" or why not (no phone screen stream, or the snapshot does not match the live screen).
- **Pointing** (`pointer.py`, `pointing.py`):
  - `plan()` maps each part through the current map. A part with its center in the view gets a green box: its boardview outline, at least 8 px. A part outside the view gets an arrow from the image center toward the part. The angle is converted to the true-orientation snapshot for the flips, and the label is "U7301 ~4 cm": the board distance from the view edge to the part (one decimal below 1 cm). A part on the other side gets no box and no arrow, only the message "on the other side: isolate the power before you turn the board".
  - `Pointing.point_to()` sets the target. On each tracked frame it computes the boxes and arrows again. It sends them only when a box edge moves by 1 % of the image, an arrow turns by 8°, or the set changes, and at most every 0.3 s.
- **New tool** `phone_point_to(refdes, registration_id=None)`: one part or up to 8. Its description says: give the arrow and the distance, never left or right words (they depend on how the user holds the phone). It also says to isolate the power for the other side, and that the positions are estimates.
- `board_locate_in_photo(highlight=true)` now uses the same pointing, so a located part outside the photo also gets an arrow.
- **Scene change with tracking**: when the watcher sees a move and the tracker follows it, the tracked registration stays valid, and the pointed boxes and arrows stay and move. The other registrations become stale, and the page shows no "Scene changed" note. `phone_point_to` and `board_locate_in_photo` work for the tracked registration after a move. The agent's pixel boxes (`phone_highlight`) and snapshot-pixel `phone_focus` still need a fresh snapshot. When the tracking is lost, the registration becomes stale and the overlay is cleared.
- **Overlay** (contract `arrows`, `overlay_arrows`):
  - `OverlayArrow` (angle, label of at most 32 characters) and `OverlayRequest.arrows` (at most 4) are in the client. `Services.send_overlay()` is the one sender. It tells the page through overlay listeners, so the page shows the boxes and arrows that the phone got.
  - `ui/setup.py` has `connect_services()` for this wiring. The tests use it too.
- **Page**:
  - The snapshot view draws each arrow at the picture edge (34 px inside), in the shown orientation (the flip is its own inverse), with its label. The arrows follow the zoom and pan like the boxes.
  - A Board panel search now runs `phone_point_to`. The panel line shows one message per part, and the Register photo line shows the tracking state.
- **Pick fix**: in Pick mode, a click waits 250 ms. The second click of a double-click, and the `dblclick` event, cancel it. A double-click only toggles full screen.
- **Fake phone and `qa_contract.py`**:
  - The fake phone takes `arrows`: at most 4, each exactly `angle_deg` (a number) and `label` (at most 32 characters). The status has `overlay_arrows`.
  - `check_overlay_arrows` checks 3 arrows (one at 450° and one at −90°), five bad bodies, that a body without `arrows` removes them, and that an empty body clears everything. `overlay_arrows` is a required status field, and `--after-start` checks that it is 0.
- `mcp/README.md`: the `phone_point_to` row, the parts "Live tracking" and "Arrows", and the Board panel text.

### Tests

- `mcp/tests/test_tracking.py` (8 tests):
  - A synthetic board (invented parts, 2000x1500 px) is the snapshot. The frames are warps of it into a 480x1040 portrait screen, turned by 90° (the preview on a portrait display), with a status text that does not move.
  - The start map matches the true map within 6 px. The tracking follows a slide, a move closer, a turn of 25°, and a move closer with a turn of −15°, within 6 snapshot px. Six steps far from the start stay within 12 px through the chaining.
  - A covered camera loses the tracking after 3 frames. Another scene does not start a tracker.
- `mcp/tests/test_pointing.py` (8 tests):
  - `plan()`: a box in view; arrows at 0° "J4 ~5 cm" and 270° "U2 ~3 cm"; the other side message; no left or right words in the messages; the four flip conversions; "~0.4 cm".
  - The tool without tracking: it needs a registration. With the board 700 px further right in the photo, U7301 gets an arrow toward the image left, and U7303 gets the other-side message. An unknown part gives an error.
  - Live tracking through the MCP server (the synthetic texture as the snapshot, frames through `SceneState.frame`):
    - `tracking` is on after the registration, and the U7301 box is within 8 px of the truth.
    - After a slide and a turn of 12°, the box sent to the phone is within 8 px of the new true position.
    - A scene change keeps the tracked registration valid, and `phone_point_to` still works.
    - After a far move, the arrow angle is within 3° of the true direction, and the phone got the arrow.
    - A covered camera marks the registration stale, clears the overlay, and `phone_point_to` is refused.
- The updated `test_board_panel.py` and `test_highlight.py` pass with the pointing messages.
- Playwright (headless Firefox; an in-process monitor with the real tools, the httpx fake phone, and the synthetic board):
  - In Pick mode, a double-click opened full screen and placed no point. One click then placed one point.
  - Without a screen stream, `tracking` said "no live tracking: the phone screen stream is not running".
  - Search U7301 (outside the photo): the panel said "U7301: outside the view: follow the arrow, …". The page showed an arrow at 171.2° with the label "U7301 ~4 cm", in the panel and in full screen. The phone got the same arrow. After Flip H, the page arrow turned to 8.8° (the mirror direction).
  - TP9: a box and no arrow. U7303: "on the other side: isolate the power before you turn the board".
- `qa_contract.py --strict` against `scripts/fake_phone.py`: 23/23 passed, with `PASS overlay_arrows`. `fake_phone.py --self-check`: passed.
- `uv run pytest`: 326 passed, 1 skipped. ruff check, ruff format, and `prek run --files` on the changed files: pass.

### Limits and the real test

- No test on the real phone: it is drained. On the real phone, check these things:
  - that the snapshot matches the live screen (a 4:3 still against the preview crop);
  - that autofocus hunting and motion blur do not lose the tracking too often;
  - that the arrow on the phone points the same way as the page arrow.
- The tracker assumes a flat board and the same camera for the snapshot and the preview. A strong zoom change (more than about 4x) or a hand that covers most of the view loses the tracking. Then the user must take a snapshot and register again.
- Without the page server or the phone screen stream, there is no tracking. Then `phone_point_to` uses the registration as it is (static), and the scene-change rules apply as before.
- The phone overlay also shows in the frames. The RANSAC step ignores these few lines. I did not test this with real frames.

## Round 25: macro autofocus mode (`phone_af_mode`)

The contract: `POST /v1/camera {"af_mode": "continuous" | "macro"}` (`in_sensor_zoom` is now optional), and `CameraStatus.af_mode`.

### What I did

- **Client** (`phone_api.py`):
  - `AfMode` (continuous, macro, and unknown for any other value) and `CameraStatus.af_mode` (None for an older app).
  - `CameraSettingsRequest` now has two optional fields, and at least one is required. The client sends a request body without None fields (`exclude_none`), so an in-sensor zoom request stays `{"in_sensor_zoom": true}`.
  - The 404 message of `/v1/camera` now says "to change the camera settings".
- **Choice and sync** (`camera_choice.py`), like the in-sensor zoom:
  - `AfModeChoice` (default continuous) is saved in `ui-settings.json` as `af_mode`.
  - `AfModeSync` sends the choice after `phone_connect` (also `bench_start`) and after an app restart (the status shows the other mode). It sends nothing to an app without `af_mode`.
  - A phone without the macro mode answers 200 and stays continuous. Then the sync stops until the next `phone_connect`, so the 1 s status poll does not send it again and again.
  - An app that answers 400 for the unknown field gives "update the phone app for the macro focus".
  - `Services.sync_phone` runs the flips, the in-sensor zoom, and then the autofocus mode.
- **Tool** `phone_af_mode(mode)`, with the description from the brief. The result is the phone status report. On a phone without macro, `advice_text` starts with "this phone has no macro autofocus mode: the camera stays in continuous autofocus". The tool counts as our own camera command for the scene watcher.
- **Evidence rule 6** has a new clause: "At close range (about 10-12 cm), you may set phone_af_mode macro." The phrase test checks it.
- **Page**:
  - "Macro focus" is in the panel next to Sensor zoom, and "Macro" is in the full-screen live bar and the snapshot bar. The three toggles share one choice (`PhoneState.af_mode_choice`) and show it with `aria-pressed`.
  - The panel line "Focus mode" shows the phone state: continuous autofocus, macro (close range), or "not in this app".
  - When the phone is closer than 15 cm and the mode is continuous, the panel suggests "close range: try Macro focus".
  - `POST /api/phone/af-mode {"mode"}` runs the tool. A save of the other settings keeps the choice.
- **Fake phone**: `POST /v1/camera` takes `in_sensor_zoom` and/or `af_mode`. Anything else, an empty body, or a wrong value gives 400. The status has `af_mode` (continuous after start). `--no-macro` is a phone without the macro mode. An old app (`--no-preview`) sends no `af_mode`.
- **`qa_contract.py`**: `af_mode` is a required status field (continuous or macro). The new `check_af_mode` checks these things:
  - macro, then continuous again;
  - zoom, torch, and in-sensor zoom stay;
  - GET status shows the same mode;
  - five bad bodies give 400.

  `--after-start` also checks that `af_mode` is continuous.
- `mcp/README.md`: the `phone_af_mode` row and the "Macro focus" page part.

### Tests

- New `mcp/tests/test_af_mode.py` (7 tests):
  - The request bodies have only the given fields, and an empty request is refused.
  - The tool sets macro, and the choice is saved and read again at a new start. A wrong mode is refused.
  - A phone without macro: the result says so, and 3 status polls send nothing more.
  - An app without `af_mode`: "update the phone app for the macro focus".
  - The sync after `phone_connect` and after an app restart. A continuous choice sends nothing to a normal phone.
  - The page route: 200 with the choice and the status, 400 for "MACRO", and a settings save keeps the choice.
- Playwright (headless Firefox, an in-process monitor, the httpx fake phone at 12.5 cm):
  - The three toggles are "Macro", "Macro focus", and "Macro". The state showed "continuous autofocus", and the hint "close range: try Macro focus" was visible.
  - After the panel toggle, all three toggles were pressed, the state showed "macro (close range)", and the hint was hidden. The second toggle set continuous again. The phone got `{"af_mode": "macro"}` and then `{"af_mode": "continuous"}`.
- `qa_contract.py --strict` against `scripts/fake_phone.py`: 24/24 passed, with `PASS af_mode (af_mode macro gives 'macro')`. With `--no-macro`: 24/24, and macro gives 'continuous'. `fake_phone.py --self-check`: passed.
- `uv run pytest`: 333 passed, 1 skipped. ruff check, ruff format, and `prek run --files` on the changed files: pass.

### Notes

- The real app part is dd-android's task, and the phone is drained. I did not test against the phone.
- The hint uses the lens focus distance. With an uncalibrated lens, the page has no distance, and there is no hint.

## Round 26: no settings fight between MCP servers (`app_start_id`)

The bug (found on the phone on 2026-09-27): the camera rebound about every 7 s. One MCP server sent `in_sensor_zoom=true`, another sent `false` about 1 s later, and this did not stop. Each server kept its own copy of the settings in memory, and it sent its value again when "the status differs from my setting". Two servers with different copies never stop.

### What I did

- **The rule** (`app_start.py`, `AppStartWatch`; used by `PreviewSync`, `InSensorZoomSync`, and `AfModeSync`). A server sends a stored setting only in these cases:
  - The user or an agent changes it through this server (the tools and the page send it themselves).
  - After `phone_connect` (one time).
  - After a real app restart: `CameraStatus.app_start_id` is new since this server last saw it.

  A status that differs from the stored value in the same app run is never a reason to send again. The server logs "changed by another client" one time per app run and value, and it does not fight. An app without `app_start_id` gets the settings only at `phone_connect`, with no periodic re-send.
- **One source of truth**:
  - `SettingsStore.current()` reads `ui-settings.json` again when the file changed (mtime in ns, size, inode). Otherwise it uses the cached copy.
  - `OrientationState.current`, `InSensorZoomChoice.enabled`, and `AfModeChoice.mode` are now properties that read the file through the store. They are not copies loaded at start. Without a store (tests), they stay in memory.
  - A change through any server writes the file, so all servers see the same value.
  - `refresh()` on each choice tells the listeners (the page) when another server changed the file. `Services.sync_phone` calls it on each status read, which includes the monitor's 1 s poll.
  - The temporary file of a save now has the process id in its name, so two servers that save at the same time do not share one temporary file.
- **Client**: `CameraStatus.app_start_id` (None for an older app).
- **Fake phone**: `app_start_id` (UUID v7 at each fake start); an old app (`--no-preview`) does not send it. **`qa_contract.py`**: `app_start_id` is a required status field. It must be a UUID v7, and it must be the same in two status reads while the app runs.
- **Test fakes**: the httpx fake phone now has an `app_start_id`, and its `restart_app()` makes a new one, like the real app.
- `mcp/README.md`: the new part "Settings of several servers". The sensor zoom and flip parts point to it.

### Tests

- New `mcp/tests/test_settings_sync.py` (6 tests; two `Services` on one fake phone, and the status polls of both servers in turns):
  - Two servers with different stored values (separate files: the worst case). Each server can send one time after its start. Then 20 more rounds of polls from both servers send nothing. The old rule sent in every round.
  - Two servers that share the file: a change through the first server is seen at once by the second (by the file time). The second server's page listener gets it one time, and the phone gets exactly one request.
  - An app restart (new `app_start_id`, the app back at its defaults): exactly one re-send of the flips and one of the in-sensor zoom. The second server sees the phone already right.
  - Another client turns the zoom on in the same app run: no re-send, the phone keeps "on", and one log line.
  - An old app without `app_start_id`: one send at the first status (as after `phone_connect`), none after a restart that this server cannot see, and one again after `phone_connect`.
  - The store reads a file that another store wrote, and it does not read the file again when nothing changed.
- The earlier sync tests (flips, in-sensor zoom, af_mode after an app restart) pass with the new rule, because the fake restart now makes a new `app_start_id`.
- `qa_contract.py --strict` against `scripts/fake_phone.py`: 24/24 passed. `fake_phone.py --self-check`: passed.
- `uv run pytest`: 339 passed, 1 skipped. ruff check, ruff format, and `prek run --files` on the changed files: pass.

### Notes

- Servers that run the old code keep the old rule until they restart. After this change, restart all MCP servers once (`scripts/dev-monitor.sh` and `scripts/mcp-server.sh --dev-reload` restart on a code change).
- Until dd-android's app sends `app_start_id`, every server uses the old-app path: it sends only at `phone_connect`. So an app restart without a new `phone_connect` does not get the settings back. The new app fixes that.

## Round 27 (P0): phone_snapshot shows the same picture as the monitor preview

The problem (docs/reports/bench-workflow-improvements.md, P0): the user saw four coils in one direction on the monitor preview, and the agent saw another direction in `phone_snapshot`. The page turned the phone screen with the Screen view choice, and the app turned its still with the phone rotation. The server applied only the flips. The paths had no shared final transform.

The orchestrator made this task a high priority. I stopped the adb-wifi task for it; that task is not finished and comes next.

### The geometry (checked in the app code; only read, not changed)

- The activity is locked to portrait (`AndroidManifest.xml`). So the phone screen and the screen stream show the camera image N in the natural portrait orientation.
- The page shows the screen turned clockwise by V: the Screen view choice, or in Auto V = (360 − `rotation_degrees`) % 360.
- The still of `/v1/snapshot` is N turned clockwise by (360 − `rotation_degrees`). CameraX sets the still rotation to (sensor orientation − target rotation), and the preview uses target rotation 0. This agrees with `FocusTap.snapshotToSurface` in the app.
- So the still must turn clockwise by **T = (V + `rotation_degrees`) % 360** to show what the page shows. T is 0 in Auto, so the bug appeared only with a manual Screen view choice (or a locked still rotation that is not the physical one).
- The flips are what the user sees, so they are in the page frame. The preview flips of the phone are in its natural frame. With V = 90° or 270°, a horizontal flip on the page is a vertical flip on the phone. The earlier code sent them unchanged.

### What I did

- **`orientation.py`** (pure functions):
  - `ImageTransform`: a clockwise quarter turn, then the flips. It extends `SnapshotOrientation`, so code that reads the flips still works.
  - It maps between the phone still ("true") and the shown image: points, boxes, and angles (`*_to_true`, `*_from_true`), and the size.
  - `view_rotation()` (the same rule as `viewRotation()` in app.js), `remaining_turn()`, `still_transform()`, and `phone_preview_flips()`.
  - `OrientationState.screen_rotation` reads the Screen view choice from the settings file (shared by all servers), and `OrientationState.transform(rotation_degrees)` gives the transform of a still.
- **`images.transform_jpeg()`**: the EXIF rotation, then the turn, then the flips. It now always applies an EXIF rotation. Before, a still without flips came back with its EXIF tag, and the width and height in the result were the sizes before that rotation. That was a second, older error in the same path.
- **`Services.phone_snapshot()`** reads the status first (the rotation of the still that the app takes now), computes the transform, turns and flips the still, and returns the image with its transform. The phone still rotation is used once, inside T.
- **`SnapshotInfo`** has `turn_degrees`, `flip_horizontal`, and `flip_vertical`, and the `orientation` text says, for example, "turned 90° clockwise to match the monitor view, flipped horizontally". The `phone_snapshot` description says that the photo is the same as the monitor preview.
- **The same transform everywhere**:
  - The saved file (`save_path`) and `multimeter_read(source="phone")` get the transformed still.
  - `phone_focus` (snapshot pixels), `phone_highlight` boxes, and `phone_point_to` / `board_locate_in_photo` boxes and arrows map back through the transform (`SnapshotGeometry.orientation` is now an `ImageTransform`).
  - Photo registration and live tracking work on the agent's image. The tracker matches that image with the screen frames by features, so the turn is part of its match.
- **The page**:
  - The monitor reads `turn_degrees` from the `phone_snapshot` result (`PhoneState.snapshot_turn`), and it draws the panel and full-screen snapshot with that turn and the current flips.
  - The boxes and arrows use the same math in app.js (`snapshotTransform`, `boxFromTrue`, `angleFromTrue`).
  - The Board panel compares only the flips of the last snapshot with the current flips (the turn is not a user choice).
- **Preview sync**: `PreviewSync` sends `phone_preview_flips(flips, V)`. It sends again when this wanted value changes: the user changed the flips or the Screen view, or in Auto the phone turned between upright and sideways. All servers compute the same value from the shared file and the status, so this does not start a fight (round 26 rule).
- **Contract** (`docs/phone-api.md`): a new rule text says how the still relates to the screen image. It describes the current app; there is no behavior change, and dd-android has nothing to change.
- **Fake phone**: the still now turns like CameraX with a 90° sensor (EXIF 6 at rotation 0: a portrait still), like a real phone. The contract check `snapshot_rotation` still passes.
- `scripts/make_orientation_target.py`: a printable test target (an arrow with a notch, and a red circle, green square, blue triangle, and yellow diamond). It is the same drawing as the test image.
- `mcp/README.md`: the part "Same picture for the user and the agent".

### Tests

- New `mcp/tests/test_image_transform.py` (142 tests), with a synthetic target (made for the tests, same license as this repository) as the sensor image:
  - The table of V and T for Auto and 0/90/180/270 × all 4 phone rotations.
  - For all 5 choices × 4 rotations × 4 flips: the still, turned by T and then flipped, is pixel-equal to the page view (the screen with the app's preview flips, turned by V). With T ≠ 0, the old way (flips only) differs.
  - For all 4 turns × 4 flips: the red circle center, a box around it, and the angle from the center map to the still and back.
  - The preview flips swap axes for a sideways view.
  - Through the MCP server with a scene fake phone (20 cases: 5 choices × rotations 0 and 90 × no flip or Flip H):
    - the `phone_snapshot` image is the page view (a mean difference below 6 of 255, after JPEG), with the right size, `turn_degrees`, and flips;
    - `phone_focus` and `phone_highlight` on the red circle of the agent's image reach the red circle of the phone's still.
  - After phone rotations (0, 270, 180) with Screen view 90°, and after an app restart, the snapshot still matches.
  - The preview flips change from H to V when the view turns to 90°. They are sent once, and not again on 3 more status reads.
- Playwright (headless Firefox; an in-process monitor with the real tools; the fake phone screen stream is an H.264 clip of the portrait target with the preview flips that the server sent):
  - For Auto, 0, 90, 180, and 270 × phone rotation 0 and 90 × no flip or Flip H (20 cases), the page's live canvas and a fresh page snapshot had the same size, and a mean difference of at most 1.5 of 255.
  - Control: the same comparison with the snapshot mirrored gave at least 10.8, so the check sees a mirror mistake.
  - A screenshot (rotation 90, Screen view 90, Flip H): the arrow direction and the shape order are the same in the live view and the snapshot. The screenshots are in my scratch folder only.
- `qa_contract.py --strict` against the new fake: 24/24. `fake_phone.py --self-check`: passed.
- `uv run pytest`: 520 passed, 1 skipped. The 8 tests of the unfinished adb-wifi task were not run. ruff and `prek run --files` on my files: pass.

### The real-phone test (for the user)

1. Make the target: `uv run python scripts/make_orientation_target.py target.png`. Print it, or show it on a second screen. Point the phone at it (in the camera app of this project), about 20 cm above it.
2. Start the bench (`bench_start`, or the page "Start all"). Open the monitor page.
3. For each Screen view choice (⟲/⟳ to 0°, 90°, 180°, 270°, then Auto):
   1. Take a snapshot on the page ("Take snapshot"), or ask the agent for `phone_snapshot`.
   2. Check that the arrow points the same way in the live view and in the snapshot, and that the shapes are in the same order (red, green, blue, yellow along the arrow).
   3. Check that the agent's `phone_snapshot` text has `turn_degrees` (0 in Auto).
4. Repeat step 3 with Flip H, then with Flip V (the buttons under the snapshot). The live view and the snapshot must be mirrored the same way.
5. Turn the phone by 90° (landscape) and repeat steps 3–4 for Auto and one manual choice.
6. Ask the agent to focus on the red circle (`phone_focus` with its pixels) and to draw a box around it (`phone_highlight`). The focus ring and the green box on the phone must be on the red circle.
7. Close the camera app on the phone (an app restart), run `phone_connect`, and repeat one case of step 3.

### Notes

- The page turns the live view on each frame with the status that it has. The server reads the status just before the snapshot. If the phone turns in the ~1 s between them, one snapshot can have the old turn. The text of the snapshot shows the turn that was used.
- Another agent (dd-mcp) worked in the same tree at the same time (`evidence.py`, `multimeter.py`, `instructions.py`). I changed only my files. One of my early `ruff format mcp` runs reformatted one file; I cannot see which. Since then, I format only my files.

## Round 28: choose the phone in the page, and adb over USB or Wi-Fi

The user's request: "allow the app to use adb over USB or over IP; a selector on the web app to list the connected devices, in case I do not want to keep the phone on USB". The phone app does not change. I started this task after the sync fix, stopped it for P0 (round 27), and then finished it.

### What I did

- **Device list** (`devices.py`, `list_devices`):
  - `adb devices -l` gives the serial, the link (`usb`, or `wifi` for an `ip:port` or `…._adb-tls-connect._tcp` serial), the state, the model, and the product.
  - The camera app check (`pm path dev.jayson.debugdevices.camera`, 3 s timeout) runs only for devices in state `device`.
  - The list also tries `adb mdns services`. The flake's adb (37.0.0-android-tools) answers "mdns is not supported by this version of adb", so the list says "not supported by this adb (use Pair or Connect with the address from the phone)". A newer adb with mDNS would show the discovered phones.
- **A conflict in the brief, and my decision**: item 1 asks for the app check on each device, but item 7 and the Fire TV rule forbid commands to other devices. I run `pm path` only on USB devices (the user plugged them in) and on the selected phone. A Wi-Fi device that is not selected shows "select it to check the app (no command goes to an unselected Wi-Fi device)". The fake adb log in the tests shows that the "TV" serial never gets a command.
- **Selection** (`PhoneSelection`): the user's choice is saved in `ui-settings.json` as `adb_serial`, and it replaces `--adb-serial` / `DEBUG_DEVICES_ADB_SERIAL` while it is set. It is read by the file time, like the other settings (round 26), so all servers use the same phone. "Clear selection" goes back to the config.
  - The server never picks one of several devices. With no choice and no config, `phone_connect` still says "several adb devices are connected; select the phone in the monitor page (Devices), or set …". With exactly one device, it uses that device, as before.
  - A save of the other settings on the page keeps the choice.
- **Page actions** (`ui/device_panel.py`, routes `/api/devices/*`). There is no MCP tool for these. Each action is one log row (source `ui`) with its steps, and the page lists the steps.
  - **Use this phone**: check that the device is in state `device`, stop the phone screen stream and remove the old adb forward (`Monitor.stop_phone`), save the choice, then run `phone_connect`.
  - **Switch to Wi-Fi** (a USB phone): first select that phone (the user's choice, so every following device command goes to it with `-s`). Then read its Wi-Fi address (`ip -f inet addr show wlan0`), run `adb tcpip 5555`, wait 2 s, run `adb connect <ip>:5555` (3 tries, 1 s apart; `adb connect` exits 0 also on a failure, so the output is checked), and then use the Wi-Fi serial with `phone_connect`.
  - **Pair**: the pairing address and the 6-digit code (`adb pair`), then the connect address (`adb connect`). The code is not written to the log.
  - **Connect**: `adb connect <ip:port>` for a phone that is already in tcpip mode.
- **Reconnect**: when the selected serial is a Wi-Fi serial and adb does not list it, `phone_connect` runs `adb connect` one time before it reports the error.
- **MCP tool** `phone_devices()`: read-only, the same data as the page. Its description says "only the user selects the phone in the monitor page (Devices); you cannot select, pair, or connect a phone". The `phone_connect` description names the page choice.
- **Commands without `-s`**: only `devices`, `mdns services`, `connect`, and `pair`. All device commands use `-s <serial>` of the selected phone, and `pm path` also goes to the USB devices of the list.
- **AGENTS.md** (safety rule): "adb: use only the serial that the user selected (page or DEBUG_DEVICES_ADB_SERIAL) … Only the user selects the phone (the Devices part of the monitor page); agents only list the devices (`phone_devices`)."
- **`scripts/fake_adb.py`**: with `FAKE_ADB_STATE` (a JSON file), it has several devices (USB and Wi-Fi serials, any state), `tcpip`, `connect`, `disconnect`, `pair`, `pm path`, and `ip addr`. `connect` and `pair` change the file. Without the variable, it works as before (one device).
- **Page**: a "Devices" part under the phone panel, full panel width, closed by default. It has the table with the buttons, the mDNS note, the Connect and Pair forms, and the steps list. It loads the list when you open it or press Refresh.
- `mcp/README.md`: the `phone_devices` row, the new `phone_connect` text, and the "Devices" page part.

### Tests

- New `mcp/tests/test_devices.py` (8 tests; `scripts/fake_adb.py` runs as a real process with a state file: a USB phone, a Wi-Fi "Fire TV", and an unauthorized device):
  - The list values. The TV is `wifi`, not checked, and gets no command. The unauthorized device has its note. mDNS is not supported.
  - No auto-pick: `phone_connect` with two ready devices and no choice gives the error that names the page.
  - The selection is saved, another server's selection object reads it, the page choice is over the config, and Clear goes back to the config.
  - An unauthorized device cannot be used.
  - Switch to Wi-Fi: all steps pass, the Wi-Fi serial is saved, and only the phone's serials got device commands.
  - A lost Wi-Fi phone gets exactly one `adb connect` and then connects. A second lost address gives "adb connect … failed too".
  - Pair: a wrong code fails; the right code pairs and connects. The code is not in the log. A plain connect to an unknown address fails with the adb text.
  - The tool is read-only: no select, pair, or tcpip tool exists, and the list does not change the saved choice.
- Playwright (headless Firefox; the real MCP server as a process with `fake_adb.py`, the state file, and `fake_phone.py` on the forward port):
  - The list had 3 rows, and the mDNS note was shown.
  - "Use this phone" gave the steps and `phone_connect` on R5CT1234567, and the phone panel showed that serial.
  - "Switch to Wi-Fi" gave 8 steps. The last ones were "adb connect 192.0.2.23:5555: connected …", "select: 192.0.2.23:5555", and "phone_connect: connected to 192.0.2.23:5555".
  - A wrong code showed a failed step, and the right code paired and connected. Clear showed the config phone.
  - The log rows were adb_select, phone_connect, adb_switch_to_wifi, phone_connect, adb_pair (error), adb_pair, and adb_clear, all with source ui.
  - The fake adb log: device commands only for R5CT1234567 and 192.0.2.23:5555.
  - The first page check found a layout bug (the table went under the Board panel). The Devices part now has the full panel width. The screenshot is in my scratch folder only.
- `uv run pytest`: 540 passed, 1 skipped (the total includes the tests of another agent that works in the same tree). ruff and `prek run --files` on my files: pass.

### Notes

- No test with the real phone or a real Wi-Fi connection. The real test:
  1. Open Devices and press "Use this phone" on the USB phone.
  2. Press "Switch to Wi-Fi". Check the steps, then unplug the cable and use the phone.
  3. Test Pair with a second phone in Android wireless debugging.
- `adb tcpip` stays active on the phone until it restarts. A phone that restarts needs "Switch to Wi-Fi" again (or Pair on Android 11+).

## Round 29: bench feedback 1 (boxes outside the phone screen, and secondary servers)

The orchestrator's diagnosis of the live bench session on the S22:

- (a) The `phone_highlight` boxes were outside the visible phone preview. The 9:20 screen is filled from a 3:4 still, so only the middle ~60 % of the width is visible, and the boxes were at `snapshot_x` 0.08–0.17.
- (b) The bench session's server was a secondary. Its boxes never reached the monitor page, and it had no phone screen stream, so there was no live tracking.

The contract has `CameraStatus.preview_region`. The brief put this task before adb-wifi, but adb-wifi was already done (round 28), so this round comes after it.

### What I did

1. **Visibility of each box**:
   - `PreviewRegion` is in the client, and the server keeps the region of the last status (status reads, overlay answers, and the status before each snapshot).
   - `highlight.in_preview()` compares the area of each box with the region: `fully` (≥ 99.9 %), `partly`, `not`, or `unknown` (no region from the app).
   - `phone_highlight`, `phone_point_to`, and `board_locate_in_photo(highlight)` return `visibility` (per box) and `warning` (for example "C12: not visible on the phone screen: move the phone or zoom out so it is near the centre", and "only partly visible on the phone screen").
   - `phone_point_to` now uses the region (in the agent's pixels, through the snapshot's turn and flips) as its view. A part that is in the still but off the phone screen gets an arrow at the screen edge, not an invisible box.
   - The page draws the region as a thin dashed frame on the snapshot.
   - The fake phone reports the contract example (the middle 60 %). `qa_contract.py` checks `preview_region` (null, or a part of the image from 0 to 1).
2. **The boxes of a secondary**:
   - A secondary sends its boxes and arrows to the primary (`POST /api/ingest/overlay`, token-protected, in the background, so a slow primary never slows a tool).
   - The primary's page draws them on its snapshot with "boxes from <origin>" (`PhoneState.overlay_origin`).
   - The page shows "The agent's view": the last annotated snapshot of `phone_highlight` or `board_locate_in_photo` of any server (from the call images, which the forwarder already sends), with the origin, the tool, and the time. This is the fallback that the bench agent had to send by hand.
3. **Frames for a secondary**:
   - There is one scrcpy stream per phone. After its `phone_connect`, a secondary asks the primary to stream the phone (`POST /api/ingest/phone-screen`). Then it reads the newest frame about 4 times per second (`GET /api/ingest/phone-frame?after=<n>`, 204 when there is none newer). Both requests carry the ingest token.
   - A scene watcher on these frames gives the secondary live tracking and scene changes, like the primary.
   - When the primary has no phone screen, the secondary uses its own stream as before.
   - The tracking note says "no live tracking: no phone screen stream in this server or in the primary monitor" only when neither exists.
4. **`bench_start`**: a separate `screen` step after `phone`. It is ok with "this server's phone screen stream (streaming)" or "the phone screen stream of the primary monitor", and an error with the reason when no stream exists anywhere.

### Tests

- New `mcp/tests/test_bench_feedback.py` (8 tests):
  - `in_preview` for the S22 region, including the bench boxes at 0.08–0.17 (`not`), and the warning text.
  - `phone_highlight` with that region returns `not` for C12 and `fully` for U1.
  - `phone_point_to`: with a region of only the right half, U7301 gets an arrow toward the left, and TP9 is in view and `fully` visible.
  - The ingest routes: an overlay with and without the token (403). A screen start without a screen and with a fake screen. The frames: 204 before a frame, the frame with its number, and 204 for `after` = that number.
  - A real primary (on a free port) and a secondary: the primary starts the stream for the secondary, and a frame of the primary reaches the secondary's scene state. `screen_source()` of the secondary names the primary. The secondary's boxes reach the primary's page with the secondary's origin.
  - No stream anywhere gives the error step.
- `test_lazy.py`: the new `screen` step (skipped when not asked; an error with the reason in the fake bench).
- `test_pointing.py`: the new `ImageFrame` argument. It found a bug in my first change (the box of an in-view part reused the names of the view rectangle), which I fixed.
- Playwright (headless Firefox; an in-process monitor with the real tools and the httpx fake phone; temporary settings; no real phone or webcam):
  - The dashed frame was at x 0.201, width 0.601 of the picture, and the warning named C12.
  - "The agent's view" showed "this server · phone_highlight · <time>".
  - A secondary's overlay sent through the ingest route (204) showed "boxes from codex 4242" with its box Q7.
  - The screenshot shows C12 outside the frame and U1 inside it.
- `qa_contract.py --strict` with the fake: 24/24. `uv run pytest`: 561 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- A live bench session ran during this work. I used only the fake phone and the fake adb, temporary settings, and free ports. I did not signal any running server.
- The secondary reads the primary's frames at the primary's tracking rate (4 per second, 720 px wide). The primary's own scene watcher runs on the same stream.
- The real test with the S22 waits for dd-android's `preview_region`. With the S22:
  1. Point at a part near the image edge and check the warning.
  2. Check that the dashed frame matches the phone screen.
  3. Run a second agent session and check that its boxes and "The agent's view" show on the page.

## Round 30: the "Markings" toggle

The user's request: "when in full screen on the web app, put a button to toggle on and off the current markings". The contract: `POST /v1/overlay {"visible": bool}` hides or shows the phone overlay without removing it, and `CameraStatus.overlay_visible` is true after an app start. A live bench session ran; I used only fakes.

### What I did

- **Client**: `CameraStatus.overlay_visible` (None for an older app), and `PhoneClient.overlay_visibility(visible)`. An app that answers 404 or 400 to a body with only `visible` raises `OverlayNotSupportedError` with "the phone app is old: its own boxes stay visible".
- **One choice** (`MarkingsChoice`, `ui-settings.json` key `markings_visible`, default shown). It is read by the file time like the other choices, so every server and page has the same state.
- **Sync** (`MarkingsSync`): the choice goes to the phone at `phone_connect` and after an app restart (the app starts with its overlay shown). It follows the rule of round 26: never again only because the status differs.
- **Page action** `POST /api/phone/markings {"visible": bool}` (strict bool). It is one log row "markings" (source `ui`), for example "hidden; phone: hidden". The MCP server saves the choice and sends `{"visible": …}` to the phone. There is no MCP tool: only the user toggles.
- **Tool results**: while hidden, `phone_highlight`, `phone_point_to`, and `board_locate_in_photo(highlight)` return `markings`: "markings are hidden in the monitor: the user does not see the boxes and arrows now (they appear when the user shows the markings again)". The boxes still go to the phone, and a body with boxes does not change the visibility (contract), so they appear when shown.
- **Page**:
  - A "Markings" button is in the phone panel (next to Clear highlights), in the full-screen live bar, and in the full-screen snapshot bar (`aria-pressed`; struck through when off). The key `k` works in the two full-screen phone views, not in form fields.
  - Off sets one class on the page, which hides the green boxes, arrows, labels, the phone-screen frame, the "boxes from …" label, the focus ring, and the scene note. Nothing is removed.
  - The webcam crop box and the Pick marks of the photo registration stay: they are the user's input, not markings.
  - An old app shows the note "the phone app is old: its own boxes stay visible" in the panel.
  - A save of the other settings keeps the choice.
- **Fake phone**: `{"visible": bool}` alone hides or shows the overlay (400 for another value or other fields in the same body); `overlay_visible` is in the status. **`qa_contract.py`**: `overlay_visible` is a required bool. The new `check_overlay_visible` checks these things:
  - hiding keeps the boxes;
  - new boxes while hidden stay hidden;
  - showing keeps them;
  - three bad bodies give 400.

  `--after-start` checks that `overlay_visible` is true.
- `mcp/README.md`: the "Markings" page part.

### Tests

- New `mcp/tests/test_markings.py` (7 tests):
  - The choice is saved, another server sees it, and the listener hears it.
  - Hide, then highlight 2 boxes, then show: the phone got false and then true, the 2 boxes reached the phone while hidden, and the tool said "markings are hidden in the monitor". After show, the note is gone.
  - An old app: the phone note says so, and the page state is still hidden.
  - An app restart with the choice hidden: exactly one more `{"visible": false}`, and no more on 3 status reads.
  - The page route: 200 with the page state, 400 for "no", the log row, and a settings save keeps the choice. A change through another server reaches this page.
- Playwright (headless Firefox; an in-process monitor with the real tools, the httpx fake phone, and a looping H.264 test clip as the phone screen):
  - The live full-screen bar button hid the markings (the phone got false), and `k` showed them (true).
  - The snapshot full-screen bar button hid them (false). A `phone_highlight` with 2 boxes while hidden returned the note, and the page kept 2 boxes with 0 shown. `k` showed them again (true): 2 boxes and the phone-screen frame visible.
  - 4 log rows, and the panel button state followed.
- `qa_contract.py --strict` with the fake: 25/25. `uv run pytest`: 572 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- No test with the real phone: the bench rule (and dd-android installs its part after "bench done"). The real test: toggle in both full-screen views and with `k`, and check that the phone's own green boxes leave the live view and come back.

## Round 31: highlight layout (tags, legend, badge placement, contrast) and the shared vectors

The problem from the bench session: two small highlighted pads 40–80 px apart. The label above each box covered the other box, the labels overlapped each other, and thin green lines were hard to see on a red board. The spec is `docs/overlay-layout.md`. The contract gives the `/v1/overlay` boxes an optional `tag` (1–3 characters). A live bench session ran; I used only fakes.

### What I did

- **Spec, exact part**: I added a "Geometry" section to `docs/overlay-layout.md`, because two implementations (Python here, Kotlin in the app) can only give the same numbers from exact rules. It defines:
  - the inputs and units (image pixels, or dp on the phone), the tag letters, and the colour order;
  - the size of the drawn box, and the badge and legend sizes as formulas of the character count (no font measurement);
  - rectangle distance and overlap (touching edges do not overlap);
  - the legend corner choice with a stable tie order, and the strip below the image;
  - the 8 badge positions with their exact coordinates for the gaps 4, 12, 24, 40, and the fallback badge on the ray from the cluster centre with a leader line;
  - the arrow anchors, the inset (source, scale, corner), and rounding to 2 decimals.

  At the end, I added the phone rule for a legend that is "outside": the first corner of the sorted list at 50% opacity, because the phone has no strip.
- **The reference** `mcp/debug_devices_mcp/overlay_layout.py`: one pure function `layout()` (pydantic models; no randomness).
- **Shared vectors** `docs/overlay-layout-vectors.json` (10 cases, tolerance 0.01), written by `scripts/make_overlay_vectors.py` (`--check` says if the file is up to date; `--render DIR` draws each case to look at). The cases:
  - the bench case (two 14×12 px pads 60 px apart);
  - 4 boxes in a cluster;
  - a box at each edge;
  - a box under every corner (the legend goes outside);
  - one box and an arrow;
  - arrows only;
  - given tags mixed with free letters;
  - 8 tiny boxes packed tight;
  - a small view full of boxes (every badge falls back, with leader lines);
  - the phone case (411×914 dp, `min_box` 24, no inset).
- **Annotated image** (`overlay_draw.py`, used by `phone_highlight` and `board_locate_in_photo(highlight)`): it draws the layout: a dark outline under each colour outline, badges with the tags, leader lines, the legend (in a strip below the image when it is outside), and the inset (the area around small boxes, enlarged with the same outlines).
- **`phone_highlight`**:
  - Each box takes an optional `tag` (1–3 characters; 4 gives an error). The server gives every box its tag before it sends the boxes, so the phone, the annotated image, and the page show the same tags.
  - The result has `layout`: `tags`, `legend_outside`, `badges_outside`, and `inset`.
  - An app from before `tag` (400 for the unknown field) gets the boxes again without tags, and the server logs it.
- **The page** asks the server for the layout (`POST /api/overlay-layout`, the same function; cached by its input) for the snapshot as it is drawn, in picture pixels. It draws the boxes with the contrast outline, the badges, the leader lines (SVG), the legend, the inset (a canvas crop of the snapshot), and the arrows with their tags; the labels are in the legend. The Markings toggle also hides the badges, the leaders, the legend, and the inset.
- **Contract, fake, check**: `OverlayBox.tag` is in the client. The fake phone accepts an optional `tag` of 1–3 characters. `qa_contract.py` sends a tagged box, and a tag of 4 characters or an empty tag must give 400.
- `mcp/README.md`: the "Highlight layout" page part.

### Tests

- New `mcp/tests/test_overlay_layout.py` (24 tests):
  - The reference passes all vectors.
  - The vectors keep the rules of the spec, independent of this code:
    - every drawn box is at least `min_box` and has the centre of its real box;
    - the tags are unique, and the colours are in order;
    - the legend rows are in tag order;
    - an inside legend covers no box, and an outside one adds the strip;
    - every badge is in the view; a badge next to its box covers no box (with clearance), no earlier badge, and not the legend; a fallback badge has a leader line;
    - the inset covers no box and is at most 25% of the width.
  - The brief's cases are all there (the corner case has an outside legend, and the full view has only fallback badges). The output is stable, the tag rules hold, and the vectors file is up to date.
- `mcp/tests/test_highlight.py`: the bench case through `phone_highlight` gives the tags A and P2 (given), no badge outside, and an inset, and the phone got the tags. A 4-character tag is refused. An app without tags gets the boxes without them. The page layout route answers, and a wrong body gives 400.
- Rendered pictures of all vector cases, and Playwright (in-process monitor, httpx fake phone; screenshots in my scratch folder only):
  - The bench case on the page: two badges A and B beside the boxes, the legend with both labels, and an inset. In the annotated image: the same tags beside the boxes, the legend in the free corner, and the inset.
  - The earlier checks (Markings, preview frame and secondary boxes, arrows and Pick) still pass. The arrow now shows its tag, and its label is in the legend.
- `qa_contract.py --strict` with the fake: 25/25. `uv run pytest`: 599 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### For dd-android

- Implement the "Geometry" section of `docs/overlay-layout.md`, and pass `docs/overlay-layout-vectors.json` (inputs; expected rects, badges with `outside` and `leader`, legend with `rows` and `outside`, arrow anchors, inset null for the phone case; tolerance 0.01).
- The phone uses `min_box` 24 dp and `inset` false, and it draws an outside legend in the first sorted corner at 50% opacity.
- The server sends every box with its tag. An app that gets no tag assigns the free letters by the same rule.
- When the spec changes, run `uv run python scripts/make_overlay_vectors.py` and check the render first (`--render DIR`).

### Notes

- The page computes the layout for the picture size on the screen. The panel image is small, so its badges and legend are placed for that size and can differ from the annotated image (which uses the full image size). The spec allows this: the unit is the pixel of the drawn picture.

## Round 32: automatic re-registration after a move (bench feedback 2, item 4)

The problem from the bench session: the user moved the board or the phone about 10 times, and each time the agent picked 6–10 part centres by eye to register the photo again.

### What I did

- **Carry-over** (`Pointing.carry()`, called at the end of `phone_snapshot`):
  - When the newest registration is stale (after a scene change, lost tracking, or an app restart), the server matches its registered photo with the new snapshot: `tracking.match_photos()` (ORB, 3000 features, ratio test, MAGSAC homography, the whole images, 720 px wide, mapped back to full pixels). It works without the live stream.
  - A good match needs at least 40 inliers, an inlier share of at least 30 %, and an RMS error of at most 6 px (the new snapshot's pixels). Then the registration comes along: a new registration (a new id, `carried_from`, `carry_quality`) with the matrix `H × (registered photo → the kept snapshot) × old`, the size of the new snapshot, the old pairs mapped through H, the capture id of the new snapshot as its `photo_id`, and a new live tracking start. It becomes the newest registration.
  - A bad match leaves the old registration stale, and the result says why ("the board or the phone moved, and the new snapshot does not match the registered photo well enough", or that the photo is not kept, for example after a server restart).
- **The kept photos**: at each registration (also a carried one), the server keeps the agent's snapshot of that moment (the newest 8), so a later carry can start from it.
- **The result**: `SnapshotInfo.registration` gives `carried`, `registration_id` (the one to use now), `carried_from`, `quality` (inliers, matches, inlier_ratio, error_px), and a note. Without a stale registration it is null.
- `tracking.MatchResult` has the RMS reprojection error of its inliers (`error_px`).
- The `Registration` model (`board/tools.py`, dd-mcp's file) has two new optional fields: `carried_from` and `carry_quality`. I placed my call after the capture id step of dd-mcp (`# region: capture id` in `phone_snapshot`), so the carried registration gets the new capture id. **For dd-mcp**: the registration reuse (feedback 1, item 6) and this carry-over work together. Reuse covers the case with no scene change; carry covers a move.
- `mcp/README.md`: the "Registration carry-over" part.

### Tests

- New `mcp/tests/test_carry.py` (a synthetic board texture as the photo; the markings.json registration):
  - Without a move, a new snapshot carries nothing.
  - After a move (a turn of 12° and a shift of 60 px, −40 px), the snapshot result has a carried registration with a new id, `carried_from`, at least 40 inliers, and an error below 6 px. The new registration is not stale and has the new capture id. `phone_point_to` puts U7301 within 8 px of its true place in the moved photo.
  - Another scene (noise) stays stale with the reason, and `phone_point_to` is refused.
- Measured on that move: 1756 of 1822 matches agree, the RMS error is 1.94 px, and mapped points are at most 0.43 px from the truth.
- `uv run pytest`: 601 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- No test with the real phone (bench rule). A real board can have repeated patterns (rows of the same parts). The inlier thresholds and the error limit reject a weak match, but a large turn, a strong zoom change, or a very different light can still fail. Then the agent registers again.

## Round 33: phone_snapshot crop (bench feedback 2, item 5)

The problem from the bench session: to check where a probe tip touches, or to read a small marking, the agent cropped and enlarged the snapshot with its own steps.

### What I did

- **`crop` on `phone_snapshot`** (`snapshot_crop.py`): `SnapshotCrop` is `{x, y, radius}` (default radius 60 px) or `{box: {x, y, width, height}}`, and `zoom` (1 to 8, default 2). A validator requires exactly one area. The position is in pixels of the photo that the agent gets.
- **The crop**: the server cuts the area from the full-resolution still, after the same turn and flips. The area is cut at the photo edges. The zoom is reduced so that the long side is at most 1600 px. LANCZOS resize, JPEG quality 92. An area fully outside the photo gives a tool error.
- **The result**: a second image, tagged with the same capture id, and `SnapshotInfo.crop` (`box_px`, `full_resolution_box_px`, `zoom`, `width`, `height`). No model judges the crop: the agent looks at it.
- The tool docstring and `mcp/README.md` ("Snapshot crop") describe the parameter.

### Tests

- `mcp/tests/test_snapshot_crop.py`: the area validator, and a 4000x3000 still with four colour quadrants: the crop around the centre shows the four colours in place, a box at the edge is cut, and an area outside gives an error.
- `uv run pytest`: 654 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- No test with the real phone (bench rule).

## Round 34: app restart notice (bench feedback 2, item 7)

The problem from the bench session: the phone app restarted, and the agent did not know. The zoom was back at 1x, the still had another turn, and the agent used old boxes and registrations.

### What I did

- **Detection** (`app_restart.py`, `AppRestartWatch`): each status read in `Services.sync_phone` (phone_status, phone_connect, the page's status poll) and in `Services.phone_snapshot` goes to `check_app_start()`. A new `app_start_id` is a restart. The first id that the server sees is not a restart.
- **The clean-up** after a restart:
  - All photo registrations become stale with a reason. `Registration.stale_reason` is new (a small edit in `board/tools.py`), and `BoardSession.registration()` refuses with it: "registration '…' is from before the phone app restarted (…): take a fresh phone_snapshot and call board_register_photo again". A scene change still gives the old move message.
  - The pointing target is cleared and the live tracker stops (`Pointing.stop_tracking()`). The page gets tracking "off".
  - The highlight boxes and arrows are cleared in our record and on the page. The restarted app has no boxes, so nothing goes to the phone.
- **The notice**: `RestartNotice` (message, app_start_id, turn_degrees, zoom_ratio, at). The text is "the phone app restarted: the snapshot turn is now N degrees and the zoom is back to Zx; old boxes and registrations were cleared", with the values from the new status.
- **Tool results**: `add_restart_notice()` in `build_server` wraps `server.call_tool` (inside the monitor's log wrapper, so the Activity log shows it too). Every successful `phone_*` result ends with the notice as a last text block. The agent's next `phone_snapshot` carries it one last time and removes it. A `phone_snapshot` from the page (no MCP context) does not remove it.
- **The page**: `PhoneState.restart_notice` and a banner at the top of the Phone panel (`#restart-note`). It stays until the notice is removed.
- `mcp/README.md`: "App restart notice".

### Tests

- `mcp/tests/test_app_restart.py`: the watch (the first id, no id, a new id), and a full run with the fake phone: register, point to a part, restart the app (a new id, zoom 1, rotation 90). Then phone_status ends with the notice, the boxes, the target, and the tracker are gone, the registration is stale, phone_point_to is refused with the restart reason, a page phone_snapshot keeps the notice, and the agent's phone_snapshot removes it. The listener sees the notice, then None.
- Page check with Playwright and the fake phone (scratchpad `restartcheck.py`): the banner is hidden, shows the notice after the restart, and hides after the notice is removed.
- `uv run pytest`: 677 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- For dd-mcp: `board/tools.py` has a new field `Registration.stale_reason` and an optional `reason` in `mark_registrations_stale()`. `take_phone_snapshot` did not change for this item.
- A tool that fails (a `ToolError`) does not get the notice appended. The registration errors carry the restart reason themselves.
- The carry-over (round 32) can still carry a registration after a restart when the new snapshot matches the registered photo well. With a zoom change, the match usually fails, and the registration stays stale.
- No test with the real phone (bench rule).

## Round 35: plain highlight boxes follow the board (highlight-tracking brief, items 1 to 6)

The user asked: "is it possible to move the markings if the camera moves?" Before this round, only the boxes and arrows of `phone_point_to` (with a board registration) followed the board. Plain `phone_highlight` boxes were cleared at each scene change.

### What I did

1. **A tracker for plain boxes** (`pointing.py`, `TrackedBoxes`, `Pointing.follow_plain_boxes()`): after `phone_highlight` sends its boxes, the server starts a `LiveTracker` with the identity matrix in place of `board_to_snapshot`. It needs the last snapshot image and a phone screen frame (`scene.latest_frame`), and no board registration. `LiveTracker` is reused, not copied. `tracking.frame_features()` and `LiveTracker.follow(features)` are new: on each frame the server computes the features one time, and both trackers (registration and boxes) use them.
2. **Each tracked frame** (`pointer.follow_boxes()`): the box corners go through `board_to_current()` (snapshot at phone_highlight → a snapshot taken now). The server sends the bounding box of the corners with the same label and tag. When the box center is outside the view (`preview_region`, else the whole image), the box becomes an arrow at the edge in its direction, with the box label (or tag). `plan()` and `follow_boxes()` share the new helpers `view_bounds`, `in_view`, and `arrow_angle`. Both use one send rule (`Pointing._send_when_due`: `MIN_POST_INTERVAL_S` and `moved_enough`), and all overlay changes go through `Services.send_overlay`. So the markings rule for an old app stays correct.
3. **One set of boxes at a time**: a new `phone_highlight`, `phone_highlight clear`, and `phone_point_to` replace the tracked boxes. `Pointing.stop()` is now async and drops the box tracker. The registration tracker keeps running: `tracked()`, `guard_registration`, and the pointing target do not change. A start that a newer overlay overtakes is dropped ("no live tracking: newer boxes or a phone_point_to replaced these boxes").
4. **Scene change** (`Services._scene_changed`): while a tracker moves the overlay (`Pointing.overlay_tracked()`: the box tracker follows, or the pointing target is on the followed registration), the boxes and arrows stay. Otherwise the server clears them as before, now through `send_overlay`. When the box tracker loses the board (3 frames without a trusted match), the server clears the boxes and arrows. `Pointing.state` is "following" while one of the trackers follows. So the page does not show "Scene changed: highlights cleared" for boxes that stay. No `ui/static` change.
   - **Notes**: `Services.phone_note` and `overlay_note()`. The agent's next `phone_*` tool result ends with one short sentence: "live tracking lost the board: the server cleared the boxes and arrows" or "the board or the phone moved: the server cleared the boxes and arrows". The note goes out one time, only to a call from the MCP client (not from the page). `add_restart_notice` is now `add_phone_notes`: it carries this note and the app restart notice (round 34).
5. **Tool result**: `HighlightResult.tracking` is "live tracking on: the boxes follow the board while the phone moves", or "no live tracking: <reason>" (no phone screen stream, or the snapshot does not match the live screen). The `ESTIMATE_NOTE` and the `phone_highlight` docstring say: with live tracking the boxes follow the board, and after a move the agent takes a fresh `phone_snapshot` before it says what is visible. New boxes still need a fresh snapshot after a move (the scene guard does not change). `EVIDENCE_RULES` does not change.
6. **Tests** (`mcp/tests/test_box_tracking.py`, 9 tests, synthetic frames from `test_tracking`, fakes only):
   - A shifted frame moves the boxes by the shift (about 60 px), on the phone and in the page state (`monitor.bus.phone.highlights`), with the same labels and tags. The page tracking state is "following".
   - A box whose center leaves the view becomes an arrow (about 180°, with its label). The other box stays a box.
   - Frames without a match clear the boxes. The tracking state goes to "off", and the next `phone_status` ends with the lost note one time.
   - A scene change while the tracker follows keeps the boxes. New boxes are still refused until a fresh snapshot.
   - No stream: `tracking` is the "no live tracking" note. A move then clears the boxes, and the next result gives the scene note. A snapshot that does not match the stream gives the reason.
   - New boxes and `clear: true` replace the tracked boxes.
   - A registration tracker and the box tracker follow the same frame. The registration stays valid after a scene change. `phone_point_to` replaces the plain boxes, and its box follows the next frame.
   - An old app with hidden markings: the tracked boxes do not go to the phone (`[]`), and the server keeps the moved boxes.
- `mcp/README.md`: "Tracked highlight boxes", "Overlay notes", the `phone_highlight` row, and the scene change text.
- `uv run pytest`: 730 passed, 1 skipped. `uv run ruff check`, `ruff format --check`, and `prek run --files` on my files: pass.

### Notes

- `server.py`: I kept the uncommitted work of the orchestrator (`send_overlay`, `set_markings`, `app_hides_markings`) and of dd-mcp. My changes there: `phone_note`, `overlay_note()`, `_scene_changed`, `highlight()`, `clear_highlights()`, `add_phone_notes()`, `SCENE_CLEARED_NOTE`, and the `phone_highlight` docstring.
- With two trackers, each frame costs one feature pass and two matches. Before, it was one feature pass and one match.
- The tracked box is the bounding box of the moved corners. After a large turn, the box is larger than the part.
- No test with the real phone or webcam (bench rule). The running MCP server loads these files only at its next start.

## Round 36: find wireless-debugging phones without adb mDNS (adb-mdns brief)

The problem from the bench session: `phone_devices` said "mdns not supported by this adb". The Nix android-tools 37.0.0 adb and `/usr/bin/adb` have no mDNS. So the Devices list did not find the phone after Samsung changed the wireless-debugging port.

### What I did

1. **Own discovery** (`discovery.py`):
   - The server browses `_adb-tls-connect._tcp` (kind `connect`) and `_adb-tls-pairing._tcp` (kind `pairing`) itself.
   - First with python-zeroconf (new dependency `zeroconf>=0.151.5,<0.152`; it adds `ifaddr`): it browses for 1.5 s, then resolves each service (1 s) to IPv4 addresses.
   - When zeroconf finds nothing or fails, it uses `avahi-browse -rtp <service>` for both types (3 s timeout each, `--avahi-browse-path` / `DEBUG_DEVICES_AVAHI_BROWSE_PATH`, default `avahi-browse`).
   - The result is cached for 5 s.
   - Each candidate has the service name, kind, host, port, `address`, the model (TXT `name=`), the source, and `adb_serial`. `adb_serial` is the adb device with the same address or mDNS name (connect kind), or on the same host (pairing kind).
   - Plain `_adb._tcp` services are not listed (on this network they are two Fire TV devices).
   - avahi can report an IPv4 address on an "IPv6" line (seen on this PC), so the address format decides, and duplicates are removed.
   - `Services.discovery` has no sources by default (tests stay offline). `from_settings` gives zeroconf and avahi-browse.
2. **Devices page**: a "Found on the network" table under the connected devices (name, model, address, kind, in adb).
   - Connect (connect kind) runs `adb connect <address>` through the existing Connect action. It shows "Connected" when adb already has that address.
   - Pair (pairing kind) only fills the pair form with the address, and with the connect address of the same host when the list has it. It then puts the cursor in the code field. Nothing connects by itself.
   - The adb mDNS note shows only when the server's own discovery found nothing.
3. **phone_devices**: `DeviceList.network` and `network_note` (read-only). The browse runs in parallel with the app checks. The tool text and `LIST_NOTE` tell the agent to give the user the exact address. The tool still cannot connect, pair, or select.
4. **Check of the Google platform-tools adb** (only on a separate server port):
   - `$ANDROID_HOME/platform-tools/adb` is version 37.0.1-15733141.
   - `adb -P 5099 mdns check` gave "mdns daemon version [adb discovery 0.0.0]", so it supports mDNS. `adb -P 5099 mdns services` listed the phone (`_adb-tls-connect._tcp`) and two `_adb._tcp` Fire TV devices.
   - `adb -P 5099 kill-server` stopped only that server. The server on 5037 (pid 60619, android-tools 37.0.0) was the same before and after.
   - **Proposal (not switched)**: after the bench, use the platform-tools adb for the MCP server (`DEBUG_DEVICES_ADB_PATH` or the dev shell PATH), and for every adb client on this PC.
     - Gain: `adb mdns services` works, and adb connects a paired phone again by itself when the port changes (mDNS auto-connect of `_adb-tls-connect`).
     - What changes: one adb version for the whole system. The running server (android-tools 37.0.0) must be replaced one time, and that drops the phone connection one time. Do this only with the user's OK. After it, `/usr/bin/adb` and the android-tools adb are a different version: each use of them restarts the server again, so they must not be used (or they must point to the same binary).
     - The server's own discovery stays as the fallback.
5. **Tests** (`mcp/tests/test_discovery.py`, 12 tests):
   - zeroconf with a fake service browser and fake service info: both kinds, the name without the type, the model, IPv4 only, and the browser and zeroconf closed. A zeroconf that cannot start is a discovery error.
   - avahi-browse output from a recorded sample (invented names, 192.0.2.x): `=` lines only, the IPv6 line with an IPv4 address, no fe80 address, and a `\032` name. The command (`-r -t -p`, both types), and a missing program.
   - The order (zeroconf, then avahi when zeroconf finds nothing or fails), the notes, the 5 s cache, and the adb serial match (same address, mDNS name, a changed port, pairing by host).
   - `phone_devices` returns `network`, and the list sends no `connect` or `pair` and no command to the TV.
   - The page route: the list, then Connect on the user's request (fake adb), then "in adb".
   - Playwright with the fake adb (scratchpad `networkcheck.py`): the two rows, Pair fills the form (no command), Connect runs one `adb connect` and the row shows "Connected".
- A live, read-only browse on this PC (both sources, output masked) found the phone in 1.5 s (zeroconf) and 1.0 s (avahi-browse). No connect, pair, or command went to any phone.
- `mcp/README.md` ("Found on the network", the `phone_devices` row) and `.env.example`.
- `uv run pytest`: 746 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- zeroconf opens the mDNS port 5353 next to the avahi daemon (shared with SO_REUSEPORT). This worked on this PC.
- Only IPv4 addresses: the Connect and Pair forms take `host:port` only.

## Round 37: stop or switch a phone that is already gone (phone-stop-bug brief)

The problem from the bench session: stop or switch away from the old phone failed with `adb -s <old Wi-Fi serial> forward --remove tcp:18765 exited with 1: adb: error: device <serial> not found`. The page had no way to disconnect the old phone, and the old Wi-Fi serial stayed selected after the device vanished.

### What I did

1. **Stop and switch**:
   - `adb.py`: `DeviceGoneError` (an `AdbError`). `_run` raises it when stderr says "device ... not found" (with or without quotes), "device offline", "listener ... not found", or "no devices/emulators found". `select_device` raises `DeviceNotListedError` for a serial that is not in `adb devices`.
   - `Monitor.stop_phone()`: it clears the page phone state (`serial`, `status`, scrcpy) first. A `DeviceGoneError` from the forward removal is not an error: it returns "the old phone was already gone: its adb forward and screen stream are cleared". The screen stream stop already ignored adb errors.
   - `DevicePanel._stop_old_phone()`: the step never blocks. It is a "gone" note, or, for another adb error, "the adb forward of the old phone was not removed: ...". The activity log row now shows the step details (for example the gone note), not only "ok". `bench_stop` shows the same note in its phone step.
2. **Disconnect** (`DevicePanel.disconnect()`, `POST /api/devices/disconnect`, log row `adb_disconnect`): it stops the phone and clears the selection (back to the config, or none), also when the device is gone or adb fails. The page button "Clear selection" is now "Disconnect". The old route `/api/devices/clear` does the same (a page opened before the update still works). Disconnect sends no `adb disconnect`: the phone stays paired.
3. **Gone state**:
   - `DeviceList.selected_gone` and `selected_note`: the selected serial (page or config) is not in `adb devices`. The saved selection stays; the user decides.
   - The page shows a red "gone" row at the top with Disconnect, "(selected here, gone)" in the header, and the network candidates of round 36 below.
   - `phone_connect` gives "the selected phone <serial> is gone: it is not in adb devices. The user connects it again in the monitor page (Devices: Found on the network, or Pair), or presses Disconnect there". For a Wi-Fi serial, the one `adb connect` try stays, and its reason is added. A Wi-Fi serial in another state (for example offline) keeps the old message.
4. **Tests** (`mcp/tests/test_phone_stop.py`, 8 tests, fake adb):
   - A switch after the old phone is gone: "device not found", "device offline", and "listener not found". The switch works, the step and the log row have the note, and the TV gets no command.
   - Disconnect with a vanished phone, with an adb failure that is not "gone", and without a phone (no forward command).
   - The gone state keeps the selection and goes away when the phone is back.
   - The `phone_connect` message for a gone USB phone (no `adb connect`).
   - `scripts/fake_adb.py`: with a `forwards` list in the state file, `forward --remove` of an unknown spec fails with "listener '...' not found".
   - `test_devices.py::test_a_lost_wifi_phone_gets_one_connect` now checks the new message.
   - Playwright with the fake adb (scratchpad `gonecheck.py`): the gone row and header, then Disconnect clears the row, the selection, and the "Serial" (not connected).
- `mcp/README.md` (Devices).
- `uv run pytest`: 754 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- No real device test: the phone is not connected now (orchestrator note), and no adb server restart.
- The page state is also cleared at `bench_stop`: after it, the page shows the phone as not connected until the next `phone_connect`.

## Round 38: device selection safety (QA round 4, batch 1: S1 to S6, B-W1, B-W3)

### What I did

- **S1, no automatic device pick**: `Adb.select_device("")` raises `NoSelectionError`: "no phone is selected: select the phone in the monitor page (Devices), or set --adb-serial / DEBUG_DEVICES_ADB_SERIAL. No command went to any device. adb sees: ...". Only `adb devices -l` runs, also with one device. The "only device" fallback is gone from `adb.py`, `config.py`, `.env.example`, `mcp/README.md`, the `phone_connect` docstring, and `PhoneSelection.effective`.
- **S2, `pm path` only on the selected device**: every other device in the list (USB or Wi-Fi) shows `app_installed: null` and "app: unknown (not selected; no command goes to a device that is not selected)". This applies to the page and to `phone_devices`.
- **S3/B-W4, serial classes**: `transport_of` gives Wi-Fi for any serial with a port (IPv4, IPv6 `[...]:port`, a host name) or an mDNS name (`._adb-tls-connect._tcp`, `._adb._tcp`). USB serials have no colon. So a host-name or IPv6 device gets no "Switch to Wi-Fi" and no `pm path`, and `select_phone` sends the one `adb connect` try for it too.
- **S4, page-only device routes**: `POST /api/devices/{select,disconnect,clear,wifi,pair,connect}` need an `Origin` header equal to the page's own origin (`Host`); otherwise 403 "only the monitor page can change the phone". Browsers send `Origin` on every POST, so the page works (checked with Playwright). curl, a script, or an agent without `Origin`, another site, and another local port all get 403, and nothing reaches adb. `GET /api/devices` stays open (read-only).
- **S5/S6/B-W1, stop and switch**:
  - `connect_phone` records the device of its own forward (`Services.forwarded_serial`).
  - `Services.release_forward()` (the monitor's `forward_remover`) removes only that forward, and only while `adb forward --list` shows that the port still goes to that device. (adb removes a local port whatever device it goes to, so another server's forward for another phone on the same port is kept.) A second stop does nothing ("stopped (no adb forward of this server)").
  - "listener ... not found" is not an error (`ListenerMissingError`, `remove_forward` returns False). "device not found" and "device offline" stay "the old phone was already gone" (round 37).
  - `PhoneScreen._adb(serial, ...)`: a session uses its own serial, also in its cleanup, so after a switch the old port is removed on the old serial.
- **B-W3, secrets**: `Adb._run(..., secrets=...)` redacts the pairing code (`******`) in the "exited with" text and in the runner's timeout text, and drops the exception chain that still held it. The log row, its error, and the step details have no code.
- `scripts/fake_adb.py`: the state mode records forwards (serial, local, remote): `forward --list` (no -s) prints them, `forward` on a used local port rebinds it to the new device, and `forward --remove` of an unknown port fails with "listener ... not found" (like the real adb). The single-device mode records nothing (`--list` prints nothing).

### Tests

- New `mcp/tests/test_device_safety.py` (20 tests):
  - S3: 8 serial forms, and a host-name device gets no app check and no switch.
  - S4: 6 routes × no Origin, another site, and another local port; the page origin works.
  - S6: a switch of the screen stream removes the old port on the old serial.
  - B-W1: "listener not found" is not an error.
  - B-W3: the code is not in the pair error, the timeout error, the log row, or the steps.
- `test_adb.py`: no selection refuses, also with one device (it replaces the test that locked in the pick) and with several (the list is in the message).
- `test_devices.py`: no `pm path` without a selection, and `pm path` only for the selected phone. `phone_connect` without a selection sends no device command.
- `test_phone_stop.py`: a second stop does nothing, and "Use this phone" after a stop works. A forward that goes to another phone now is kept (no `--remove`). "listener not found" gives "already removed".
- The test helper `make_services` now selects the fake phone (the user's choice), because nothing is picked by itself.
- `scripts/qa_mcp_stdio.py --skip-webcam` (fakes only): 15/15, with `fire-tv` "no_selection_fire_tv_only" passed.
- Playwright (fake adb): the network list with Connect and Pair (round 36), and the gone row with Disconnect (round 37), still work with the Origin rule.
- `uv run pytest`: 817 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- **After the next server start**, a user with an empty `DEBUG_DEVICES_ADB_SERIAL` and no page choice must select the phone in the monitor page one time; `phone_connect` says so.
- During the run, dd-research-2 edited `scene.py`, `board/identity.py`, and their tests (B-E6, B-E7): three of their tests failed during those edits and passed later, and `board/identity.py` (PLR0913) and `test_board_parts_at.py` (E501) had ruff errors in their unfinished work. They are not in my files.

## Round 39: settings lock, save errors, offline reconnect (QA round 4, batch 2: B-W5, B-W8, B-W9)

### What I did

- **B-W9, one lock for all settings writers**:
  - `SettingsStore.update(change)` reads the file, changes it, and saves it under a lock file next to it (`ui-settings.json.lock`, `fcntl.flock`). The lock waits at most 2 s (`LOCK_TIMEOUT`); after that the save fails with an `OSError`, and the callers report it as before.
  - Every writer uses it: `PhoneSelection.set` (devices.py), the in-sensor zoom, autofocus, and Markings choices (camera_choice.py), `OrientationState` (orientation.py), and the page settings save (`Monitor.update_settings`).
  - The page settings save now takes the values that other parts own (the flips, the camera choices, the Markings toggle, the selected phone) from the file under the lock, not from the memory of this server (also the fix for the suspect B-S8). The owner's value is the fallback when the file has none.
- **B-W8, a failed selection save is a failed step**: `PhoneSelection.set` raises `SelectionNotSavedError` and keeps the old choice. The select action now saves the choice first, then stops the old phone, then runs `phone_connect`. So a choice that cannot be saved fails at "select" and leaves the old phone running. Disconnect still stops the phone, and its "clear the selection" step fails honestly when the save fails.
- **B-W5, an offline Wi-Fi serial**: `select_device` raises `DeviceStateError` (with the state). For a Wi-Fi serial in state `offline`, `select_phone` first runs `adb disconnect <serial>` (a host command for the selected phone only), then the one `adb connect`. Before, adb answered "already connected", and the phone stayed offline.

### Tests

- `mcp/tests/test_device_safety.py`, batch 2 (4 tests):
  - An offline Wi-Fi phone: `disconnect`, then `connect`, then `phone_connect` works.
  - A failed save: the select action has one failed "select" step, no forward command, and an error in the log row. Disconnect: the stop is ok, and "clear the selection" fails.
  - Two writers (two stores, the first one slow inside its change) keep both changes. Without the lock (checked in a scratch run), the second change is lost.
  - A held lock: the save fails after the timeout, and `PhoneSelection.set` raises `SelectionNotSavedError`.
- `test_devices.py`: the new step order ("select" first).
- `uv run pytest`: 828 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

## Round 40: overlay and sync fixes (QA round 4, batch 3)

### What I did

- **B-S3, the dark outline outside**: PIL draws an outline inward from the rectangle. The dark outline now goes on the rectangle grown by 2 px, so it is 2 px wider than the colour outline on both sides (rule 5).
- **B-S4, the inset**: the annotated image and the page draw the real box areas in the inset (the layout input), not the drawn rectangles of at least 32 px (these are larger than the inset, so they were skipped). Each box has the dark and colour outlines and its tag badge. The PIL inset is drawn on the enlarged crop, so nothing goes outside it.
- **B-S5, a read-only flips call**: `phone_snapshot_orientation()` without arguments returns the flips and does not save or push them. A set still saves, pushes, and calls `scene.unwatched_view_change("flips")` (the call of dd-research-2, kept in the set path).
- **B-S6, a resend after an app restart**: `AppStartWatch.failed()`: a send that `may_send` allowed and that failed (for example the camera was not ready) may try again on the next status reads, at most 3 sends per app run (`MAX_RESEND_TRIES`). All four syncs use it (preview flips, in-sensor zoom, autofocus mode, markings). `AfModeSync` now stops only when the app cannot do it (`AfModeUnknownToAppError` for the 400, or an old app); a passing error is retried.
- **B-S7**: `board_locate_in_photo(highlight)` has the same markings note as `phone_point_to` (`Services.highlight_parts`).
- **B-S8**: fixed in round 39 (the page settings save takes the owned values from the file under the lock).
- **B-S9, no flicker during a wheel zoom**: while the page waits for the layout of a new size, it draws the last layout of the same boxes, scaled to the new size. Before, it drew nothing until the answer came.
- **B-S10 and C7, tags**: box tags follow the contract rule `^[A-Za-z0-9]{1,3}$` (`phone.OVERLAY_TAG_PATTERN`) in `OverlayBox` and `PixelBox`. Emoji, accents, and other characters are refused before anything goes to the phone.
- **B-S11, the page snapshot without the raw still**: the monitor keeps the flips of the kept image. After a flip change, it flips the image by the difference, so it matches the boxes that the page draws with the flips of now.
- **B-E9**: a `bench_measure` photo (and its turn and flips) is the page snapshot, like a `phone_snapshot`.
- **B-E10, the rotation of a push**: `PreviewSync.push()` without a status reads a fresh status first, so the preview flips never use an old rotation. `ensure()` passes its status.
- **B-F6, frames after a primary restart**: the frame route also sends the newest frame when the requested number is higher than its own counter (the counter of an older primary run). The secondary starts from 0 again when the primary's URL or token changes.
- **B-F9, overlay order**: a secondary numbers its overlay changes when they happen (`IngestOverlay.seq`), and the primary drops a change older than the last one that it drew from that origin. A sender without a number (an older secondary) is always taken.
- **B-F10, webcam controls**: with auto exposure on, a saved fixed exposure time is left out, and the exposure mode goes first in its own `v4l2-ctl` call.

### Tests

- New `mcp/tests/test_overlay_sync_round4.py` (18 tests), one or more for each item above. B-F10 is in `test_webcam_controls.py` (the split call, and no fixed time in auto).
- Playwright (fake phone, scratchpad `zoomcheck.py`): the layout route was slowed by 300 ms. During four wheel zoom steps in full screen, no frame (of 128) had 0 boxes. The page inset shows both small boxes with the tags A and B.
- `uv run pytest`: 846 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- I did not run the flicker check against the old `app.js`. Before the fix, `drawLayout` drew nothing while a new layout was pending, so the boxes were gone for the time of each request.

## Round 41: contract gaps, dd-ui part (QA round 4: C3, C7, C11, C12, C16, C17, C18)

I read `docs/phone-api.md` again first (the overlay body rules, `app_start_id`, `preview_region`, `overlay_region`, the snapshot headers, and the EXIF rule).

### What I did

- **C3, arrow tags**: `OverlayArrow.tag` (optional, the box tag rule). An arrow of a tracked plain box (round 35) keeps the box's tag, so the phone and the page give it the same tag and colour. An older app that answers 400 to a box or arrow tag gets the overlay again without the tags (before: only box tags); our record and the page keep them.
- **C7, the tag rule**: `^[A-Za-z0-9]{1,3}$` in `OverlayBox`, `OverlayArrow`, and `PixelBox` (round 40). The page gets its tags from the server.
- **C11, the overlay lifetime**: each overlay call with boxes or arrows starts a timer of `OVERLAY_TTL` (10 minutes, `phone.OVERLAY_TTL`), and a clear or a new call replaces it. At the end, the server forgets the boxes and arrows (the page gets them through the overlay listeners), the pointing and the plain box tracker stop, and the next `phone_*` result says "the boxes and arrows were older than 10 minutes: the phone app removed them". No request goes to the phone (the app removed them itself). A tracked box that moves makes a new call, so it stays, like on the app. The app restart part was already in round 34.
- **C12, the snapshot headers**: `PhoneClient.snapshot()` returns a `Still` (the JPEG, `X-Rotation-Degrees`, `X-App-Start-Id`). `Services.phone_snapshot` turns the still by its own rotation, and a different app start id is an app restart (round 34 notice). An app without the headers: the status read before, as before. The monitor recorder keeps only the JPEG, as before.
- **C16, C17**: done in round 38 (no "only device" fallback, no `pm path` to a device that is not selected; the README, `.env.example`, and the tool texts agree).
- **C18, `.env.example`**: 20 missing variables with comments (`DEBUG_DEVICES_ADB_PATH`, `_ADB_TIMEOUT`, `_LOCAL_FORWARD_PORT`, the phone and webcam timeouts, `_OPENROUTER_BASE_URL`, `_METER_MODEL`, `_WEBCAM_CROP`, the phone screen and scrcpy server settings, `_BOARDVIEW_DUMP_TIMEOUT`, and others). The stale `DEBUG_DEVICES_SCRCPY` (the setting is now `scrcpy_window`, default false) is replaced.
- `mcp/README.md`: "Overlay lifetime" and "Snapshot headers".

### Tests

- New `mcp/tests/test_contract_round4.py` (7 tests): the still rotation from its header (a fixed Screen view of 0°, so the turn is the rotation), an old app without headers, an app restart between the status and the still, arrow tags, an app without arrow tags, the TTL (the server and the page forget, the note, no phone request), and a new call restarts the TTL.
- New `mcp/tests/test_env_example.py` (3 tests): every setting is in `.env.example`, no unknown `DEBUG_DEVICES_` variable, and the file parses as a `.env` with the defaults.
- `test_box_tracking.py`: the arrow of a box that left the view has the box's tag.
- `uv run pytest`: 856 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- For dd-qa: `scripts/fake_phone.py` does not send the snapshot headers yet; the server then uses the status (the old-app path). The tests use their own fake with headers.

## Round 42: overlay_region for the "visible on the phone" flag

The contract now has `CameraStatus.overlay_region`: `preview_region` without the system bars, the display cutouts, and the app's status label (the part where the app draws boxes). It changes with the preview flips and the rotation.

### What I did

- `CameraStatus.overlay_region` and `CameraStatus.visible_region()`: `overlay_region`, else `preview_region` (an app from before it).
- `Services.overlay_region` (it replaces `Services.preview_region`): the visible region of the last status. `seen_status` updates it from every status that the server reads.
- It is read again after a flip or a rotation change: the flips set of `phone_snapshot_orientation` and `phone_rotation` pass their new status to `seen_status`. The page status poll, `phone_status`, and each `phone_snapshot` also update it.
- It decides:
  - the per-box `visibility` and the warnings of `phone_highlight`, `phone_point_to`, and `board_locate_in_photo(highlight)`;
  - the pointing view: a part outside it gets an arrow, because the app would not draw its box;
  - the dashed frame on the page snapshot (`status.overlay_region ?? status.preview_region`).

### Tests

- New `mcp/tests/test_overlay_region.py` (3 tests, a fake phone whose region follows the flips and the rotation):
  - A box under the status bar (inside `preview_region`) is "not" visible, with a warning.
  - An older app without `overlay_region`: the same box is "fully" visible (the `preview_region`).
  - The region is read again after a vertical flip (the bar goes to the bottom) and after rotation 90.
- Playwright (scratchpad `regioncheck.py`): the page frame starts at 8 % (`overlay_region`), and at 0 % for an older app.
- `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15. It uses `scripts/fake_phone.py`, which now sends `X-Rotation-Degrees` and `X-App-Start-Id` (dd-qa round 5), so the header path of round 41 runs there too.
- `uv run pytest`: 859 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Notes

- A correction to round 41: `scripts/fake_phone.py` in the working tree already sends the snapshot headers (dd-qa round 5). dd-qa adds `overlay_region` to it now.
- No real phone check: the phone is not connected. On the S22, check that the frame and the warnings follow the bars in portrait and at rotation 90.

## Round 43: follow-up of the final QA run (QA round 6, dd-ui part)

I read `docs/phone-api.md` again first (C6, C9, C13/N9, N8; and the orchestrator's note: a zero-size `overlay_region`).

### What I did

- **C11 (a), the boxes of a secondary server on the primary page**: `forward.RemoteOverlays` keeps, per secondary origin, the order (B-F9), a timer of `OVERLAY_TTL`, and the app run that the secondary saw (`IngestOverlay.app_start_id`, new; a secondary sends the `app_start_id` of its page status). The primary forgets that origin's boxes on its page (only when the page still shows them) after `OVERLAY_TTL` from the last change, and when a phone status of the primary (status poll, a tool result, `phone_connect`, or its restart notice) shows another app run. This also works after the secondary exited.
- **C11 (b), an old app with hidden markings**: the lifetime now follows our record, not what went to the phone. `send_overlay` starts the timer for the boxes of the record, also when the old app got `[]`. When the markings are shown again, the new call to the app starts the timer again.
- **C16**: `mcp/README.md:17` now says "Always select the phone: in the monitor page (Devices), or set `DEBUG_DEVICES_ADB_SERIAL`", and without a selection the phone tools send nothing, also with one device. `:198` says that the server never picks a device by itself, also not the only one.
- **N2, the screen of a secondary**: `POST /api/ingest/screen/start` accepts only the serial that the user selected on the primary (page, then config; `Monitor.selected_serial`, set by `connect_services`). Another serial, or no selection: 403 and a log warning. No adb command goes to it.
- **N3, the settings lock and the event loop**: `SettingsStore.update_async` runs the lock wait and the file work in a worker thread. The async callers use it:
  - the camera choices (`StoredChoice.save`, a small base class for the in-sensor zoom, autofocus, and Markings choices);
  - `OrientationState.save` and `PhoneSelection.save`;
  - the page settings save (`Monitor.save_settings`), and the tools.
  - The listeners (the page state) still run on the event loop. The sync `set` and `update` stay for code without an event loop (the start, tests).
- **N4, the forward record**: `release_forward` forgets `forwarded_serial` only when the forward is gone: removed, not there, another phone's now, or the device is gone. A passing adb failure keeps the record, so a later stop tries again. A new `phone_connect` during the adb calls keeps its own record.
- **N7, saved webcam controls**: `WebcamControls.following(changes)`: a fixed exposure saves `auto_exposure: false`, and auto exposure on removes the saved fixed time. After a restart, the camera gets the same values.
- **Zero-size `overlay_region`** (the orchestrator's contract note): only a missing `overlay_region` falls back to `preview_region` (`visible_region()` checks for None, not truthiness). A zero-size region (`PreviewRegion.empty`) means that nothing shows on the phone: every box is "not" visible with a warning, and the pointing gives arrows (a view with no area has no target in view). The page uses `??`, so it keeps a zero-size region too.

### Tests

- New `mcp/tests/test_qa_round6.py` (8 tests):
  - a secondary's boxes after the TTL, and the newer boxes of another origin stay;
  - another app run removes them;
  - hidden boxes of an old app expire;
  - the screen start refuses a serial that is not selected, and a request without a selection;
  - a held lock for 0.3 s: the event loop ran (at least 10 ticks of 10 ms) and the save worked;
  - a passing removal failure keeps the forward record, and the next stop removes it;
  - the saved webcam controls after "auto", then "exposure 900", then "auto".
- `test_overlay_region.py`: 3 more tests (a zero-size region: not visible and no fallback; arrows, not boxes; only a missing region falls back).
- `test_bench_feedback.py`: the primary's user selected the phone (N2). `test_overlay_sync_round4.py`: the new `remote_overlay(IngestOverlay)` form.
- `uv run pytest`: 883 passed, 1 skipped. ruff and `prek run --files` on my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

### Notes

- One run of the full suite had one failure in `test_bench_state.py` during dd-mcp's work on N5/N6; alone it passed (38 passed), and the next full runs passed.

## Round 44: last round of the follow-up check (QA round 8, dd-ui part)

I read `docs/phone-api.md` again first: an empty `overlay_region` sits at the centre of `preview_region`.

### What I did

- **N2 rest, an empty serial**: the ingest screen-start route refuses every request when nothing is selected (also `{"serial": ""}`), with 403 and a log line. Before, an empty selection matched an empty serial. `PhoneScreen.ensure_running("")` raises `ScreenError` ("no ADB serial for the phone screen"), like `scrcpy.py`, so `adb -s ""` never runs.
- **N19, the app run of the overlay call**: `Services.seen_status` now tells its status listeners about every phone status that the server reads (tools, the page poll, overlay calls). The monitor (`status_seen`) keeps the page status up to date with it and checks the app run of the remote boxes. So a secondary sends the app run of the status of its overlay call with its boxes, not an older one.
- **N20, two flip changes at the same time**: `FlipChange` does the read-modify-write inside the settings file lock. The given flips go onto the flips that are in the file at that moment, so two changes (the page's Flip H and an agent's flip_vertical) both apply. `OrientationState.update` and `save` use it.
- **N12, the client arrow math**: nothing to change. With an empty region at the centre, the view centre is the preview centre: each part gets an arrow from there, with the distance from there. A zero-size view gives no division by zero (`edge_point` skips the zero axis and gives the centre). New tests pin it.
- `mcp/README.md`: an empty region is at the centre of `preview_region`.

### Tests

- New `mcp/tests/test_qa_round8.py` (8 tests):
  - an empty or other serial with nothing selected (2 cases), and `PhoneScreen` with an empty serial (no adb call);
  - a secondary without a status poll: after an app restart, its boxes go out with the new app run;
  - two flip changes at the same time (two states on one file), and a sync update of an old state keeps the other flip;
  - the arrows of an empty region at the centre (J4 right, U2 up, "~3 cm" each), and a plain box becomes an arrow with its tag. Both run under `np.errstate(all="raise")`, so a division by zero would fail them.
- `uv run pytest`: 917 passed, 1 skipped. ruff and `prek run --files` on my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

## Round 45: follow-up of the last-round check (QA round 10, dd-ui part)

I read `docs/phone-api.md` again first (the new N11 sentence: the rotation response waits for the layout of the new orientation). The server already takes the `overlay_region` of that response (`seen_status` in `phone_rotation`, round 42). I did not edit `multimeter.py`, `meter_frames.py`, `config.py`, `constants.py`, or `pyproject.toml` in this round.

### What I did

- **N27, a target at the centre of an empty view**: `edge_point` returns the centre when there is no direction (before, `min()` got an empty list and raised `ValueError`). In `plan`, a part exactly at the centre of an empty view gets no arrow and the note "at the centre of the view, but the phone has no room for boxes now: no arrow". `follow_boxes` gives no arrow for such a box. The live tracking paths in `pointing.py` also catch a `ValueError`, log a warning, and try again with the next frame.
- **N28, the distance for a centred band**: a view with no area (a point, or a band of width or height 0) shows nothing, so `edge_point` measures the distance from its centre. The QA case (a band `(200, 400, 600, 0)` px, a part 0.5 cm left of the centre) now says "~0.5 cm", not "~2 cm".
- **N29, the crop delete**: `Monitor.clear_crop()` removes only `webcam_crop`, in a read-modify-write inside the settings file lock. `DELETE /api/settings/crop` uses it, so a Screen view change or another server's change at the same time stays.
- **N30, a secondary's own screen stream**: `PhoneScreen.selection` (set by `connect_services`, the same selection as the ingest check) is checked before each start and each restart of the session. When the user selected another phone, the stream stops (state error "<serial> is not the selected phone any more: the screen stream stopped") and sends no more adb commands to the old serial. The new phone's stream starts at its `phone_connect`.

### Tests

- New `mcp/tests/test_qa_round10.py` (9 tests):
  - a part and a box at the centre of an empty view, `edge_point` with no direction, and a geometry error in a tracked frame (a warning, the tracking stays);
  - the band distance "~0.5 cm" at 180°;
  - a crop delete keeps a rotation change made by another writer, directly and through the route;
  - the screen stream stops after a restart when another phone is selected, with no adb command after that (all calls to the old serial only); and it does not start for a phone that is not selected.
- `uv run pytest`: 952 passed, 1 skipped. ruff and `prek run --files` on my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

### Notes

- One prek run printed a Nix "unexpected end-of-file" error while another agent edited the tree; a second run on each of my files passed.

## Round 46: the webcam of the running monitor for a server on another port (QA round 11)

The problem (found by dd-meter): a test or a second MCP server started with `--ui-port N` did not find the running monitor on the default port, so it could not share its webcam (only one process can read the V4L2 device).

### What I did

- `RemoteMonitor(..., other_ports=...)`: the lookup (`find_monitor`, `identify`) asks the own port first, then the other ports. The first debug-devices monitor of another process that fits wins (for the webcam: it streams the same device). The next requests (info, stream, frames) go to that port.
- `ui/forward.other_monitor_ports()`: the default page port (18766), then the ports of the running pages. Each primary already writes its token file `ingest-<port>.token` in the runtime dir (`$XDG_RUNTIME_DIR/debug-devices`), so `page_ports()` takes the port from the file names. It never reads a token.
- Only the webcam sharing uses the other ports (`ui/setup.py` for the page path, `Services.from_settings` for `--no-ui`). The call forwarder and the phone screen of a secondary keep only their own `--ui-port`, so a test server never forwards calls or boxes to the user's running monitor.
- `mcp/README.md`: the lookup order.

### Tests

- New `mcp/tests/test_remote_ports.py` (6 tests, an httpx transport that answers per port):
  - the monitor on the default port is found, and the frames come from it;
  - the monitor that streams the device wins over one that does not;
  - this process and another app are not a monitor;
  - without other ports, only the own port is asked;
  - the page ports come from the token file names (other files are ignored);
  - a busy local webcam uses the running monitor on the default port.
- `uv run pytest`: 1053 passed, 1 skipped, 7 failed. The 7 failures are in `test_sevenseg_compare.py`, during dd-meter's work on N37 (`sevenseg/` and `config.py` changed during the run). That file alone: 28 passed.
- ruff and `prek run --files` on my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

## Round 47: QA round 13 (dd-ui part)

Base: `3a2fd23`. No "N44" is in my files (the rename is for dd-mcp and dd-android-2).

### What I did

1. **N50, the primary lookup**: the monitor has its own `RemoteMonitor` for its own page port only (`Monitor.primary_lookup`, set by `ui/setup.py`). `_find_primary` and `_other_monitor_runs` use it; they no longer use the webcam lookup, which also asks other page ports. Only the webcam sharing (`shared.remote`) uses the other ports. The lookup is closed at stop. Without `primary_lookup` (a test that builds a monitor directly), the webcam lookup is used, which there has only the own port.
2. **N51, frames of another monitor**: `SharedWebcam` takes frames of another monitor only with that monitor's crop box. Without one, it raises `RemoteCropMissingError`: "the webcam belongs to the debug-devices monitor at <url>, and it has no crop box: set the crop box on the page that owns the webcam. No frame was sent anywhere". It asks for no frame from the owner.
   - The error is not a "remote unavailable" error, so the server does not read the webcam itself instead.
   - It is in the shared capture path, so `multimeter_read`, `bench_measure`, and `webcam_snapshot` refuse such frames too.
   - The server's own webcam without a crop is as before (the user's own choice).
3. **The stale port**: each `/api/whoami` probe waits at most `remote.PROBE_TIMEOUT` (2 s), not the webcam timeout (20 s). After a lookup that finds nothing, `RemoteMonitor.base_url` goes back to the own port (the cosmetic point of Round 12).
4. **`mcp/README.md`**: only the webcam sharing asks the other ports, and the primary or secondary decision, the forwarding, and the secondary screen use only the own port. It also has the crop rule for another monitor's frames, and the 2 s probe.

### Tests

- New `mcp/tests/test_qa_round13.py` (5 tests):
  - N50, both cases of the check: a server on port 40111 while a monitor runs on 18766 serves its own page and still shares the webcam of 18766; the user's server on 18766 while another page runs serves its own page;
  - N51: a remote monitor without a crop is refused (no frame asked); `multimeter_read` fails with the message, and the vision fake got no request;
  - a stale port (a real local socket that accepts and never answers): the lookup ends within 2 s with a probe timeout of 0.3 s, while the webcam timeout is 20 s.
- `test_remote_ports.py`: the owner in these tests has a crop box now; the fake also records the asked paths.
- `uv run pytest`: 1092 passed, 1 skipped. ruff and `prek run --files` on my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

## Round 48: QA round 14 (dd-ui part)

Base: `2e6793c`.

### What I did

1. **N55, the short probe only for the other ports**: `RemoteMonitor._whoami` uses `remote.PROBE_TIMEOUT` (2 s) only for a port that is not the own port. The own port keeps the client timeout, as before round 13. So the primary lookup, `_other_monitor_runs`, the forwarder, and the remote screen (all only the own port) find a busy primary again. The webcam sharing keeps the short probe for the other ports.
2. **N56, `scripts/record_demo.py`**: the demo server binds a free port for its own page (`free_port()`, as its comment said), never 18766. It still gets the webcam frames of the monitor on 18766 through the webcam sharing (it asks the default port), so that monitor must run and have a crop box (N51). The docstring says so. `DEFAULT_URL` (18766) stays: it is for recording the user's own page without `--fake-phone`.
3. **`test_ui_app.py`** (`test_build_monitor_takes_over_the_webcam_and_the_model`): the settings use a free port (`conftest.free_port()`, shared now with `test_qa_round13.py`), not 18766.
- `mcp/README.md`: the 2 s probe is only for the other page ports, each stale port adds at most 2 s to a webcam lookup (the wording of the Round 13 check), and the own port keeps the normal timeout.

### Tests

- New `mcp/tests/test_qa_round14.py` (2 tests, a real local HTTP responder that answers `/api/whoami` after 0.5 s, and a probe of 0.2 s): on the own port (the primary lookup) it is found; as another port of the webcam sharing, the probe cuts it.
- `scripts/record_demo.py` imports and `free_port()` gives a port that is not 18766. I did not record a demo: that needs the real webcam and the running monitor.
- `uv run pytest`: 1096 passed, 1 skipped. ruff and `prek run --files` on my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

## Round 49: QA round 15, the last fix round (dd-ui part)

Base: `fe66f61`.

### What I did

1. **N70, `scripts/record_demo.py`**: no default URL any more. Without `--fake-phone`, `--url` is required; without both, the script stops at once with "give --fake-phone (a demo server), or --url of a page that you run (only the user records the real page)" (exit 2), before it starts anything. The `--url` help, the module docstring, and the root `README.md` ("Record the demo") say that only the user runs it against the real monitor page, because the script clicks through that page. The demo server's page is on a free port (round 48).
2. **N71, `test_ui_app.py`**: the `build_monitor` test also patches `defaults.PORT` to a free port. The test checks that `other_monitor_ports()` does not list 18766 (the user's page port), so the webcam lookup of that monitor never asks the running dev monitor.

### Tests

- `scripts/record_demo.py` without arguments: exit 2 with the message above (checked with `uv run --with playwright`); `--help` shows the new `--url` text.
- `uv run pytest`: 1103 passed, 1 skipped. ruff and `prek run --files` on my files (`scripts/record_demo.py`, `mcp/tests/test_ui_app.py`, `README.md`): pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

## Round 50: staged captures, part 1 (the page, the capture, the store; docs/briefs/staged-captures.md)

The `server.py` part (the `multimeter_read` pop, the `live` parameter, the `staged_captures` tool) waits for the orchestrator's go. I did not edit `server.py`, `bench_state.py`, or `bench_journal.py`.

### What I did

- **The store** (`staged.py`, `StagedStore`): a folder in the state folder (`~/.local/state/debug-devices/staged/`, mode 0700; files 0600, written to a temporary name first), with a file lock, so every MCP server of the user can list and pop the captures.
  - One capture: a JSON entry (`StagedCapture`: id, time, state pending/ready/failed, origin, the photo facts, the `MeterResult` with its bench notice, notes), the photo, and the meter frames (the crop images that the vision model saw).
  - At most 10 captures: a full queue refuses a new one (`QueueFullError`, a page message). Time to live 30 min.
  - A capture that is still pending after 3 min (its server stopped) becomes failed.
  - `pop_all()` waits (at most 90 s) while a capture is pending (the vision call runs), then gives all, oldest first, and removes them. `pop_ready()`, `list()`, `delete()`, `clear()`, and `photo()` are also there.
  - All methods run the lock wait and the file work in a worker thread.
- **The lock**: `file_lock.locked()` is the shared lock helper; the settings store uses it now too.
- **The capture** (`ui/staged_capture.py`, `StagedCapturer`): a key press stages a pending capture at once, then fills it in the background. The photo and the meter part run at the same time (like `bench_measure`).
  - **Photo**: the still of `Services.phone_snapshot()` (the same turn and flips), scaled like the default `phone_snapshot` result. It does not replace the agent's last snapshot: the pixel tools keep referring to the photo that the agent saw, and `last_view` is restored.
  - **Meter**: `server.read_meter` (unchanged): the vision call, the frame checks, the bench limits, and `note_meter_reading`. So an unsafe voltage closes the bench gate at capture time, before any pop.
  - **No crop box**: no meter part, no frame, and no vision call; a note says why. The photo still comes.
  - **No phone**: a note. With neither part, the capture is failed with both reasons.
- **The page routes** (`ui/routes/staged.py`):
  - `POST /api/staged` (capture; 202, or 409 for a full queue), `GET /api/staged` (the list), `GET /api/staged/<id>/photo.jpg`, `DELETE /api/staged/<id>`, and `DELETE /api/staged`.
  - A capture, a delete, and a clear accept only a same-origin request from the page (like the device actions).
  - Each capture is one log row (`staged_capture`).
- **The page**:
  - A "Capture Space" button and a counter in the header. Space and C capture (a USB foot pedal that sends Space works too), never in a text field or with a modifier. A held key gives one capture. Space on a focused button captures and does not click the button or scroll.
  - A short flash and a beep (WebAudio; without sound, the flash still shows).
  - The "Staged captures" list above the phone panel: the time, the small photo, the value with its status ("reading…" while pending), the bench notice (two lines, the full text in the tooltip), the notes, × to remove one, and "Clear all". The page reads the list every 2 s, because another server can pop it.
- **Tests' state folder**: `conftest.py` gives every test a private `$XDG_STATE_HOME` (like the runtime dir), so no test reads or pops the user's real queue.

### Tests

- New `mcp/tests/test_staged_store.py` (7):
  - pop gives all, oldest first, and removes every file;
  - a full queue;
  - an expired capture;
  - a pending capture: `pop_ready` leaves it, `pop_all` waits and gives it as failed, and after 3 min it is failed;
  - delete, clear, and a late finish of a deleted capture;
  - the file and folder modes;
  - **a second server process pops the captures** (a real subprocess on the same folder).
- New `mcp/tests/test_staged_capture.py` (6):
  - a key press stages the photo and the meter result (4.98 V confirmed, 2 frames, 2 vision calls), and a second press a second capture; the agent's last snapshot stays;
  - no crop box: no webcam frame and no vision call, the note, and the photo;
  - no phone and no crop box: failed with both reasons;
  - **4.98 V closes the bench gate at capture time**, while the capture still waits in the queue;
  - the page routes (403 without the page origin, 202, the list, the photo, delete, clear, the log rows), and a full queue gives 409 with the message.
- Playwright (fakes, a private state folder; scratchpad `stagedcheck.py`): Space and C capture (with the flash); Space and C in a text field are text (no capture); Space on the focused "Take snapshot" button captures and does not take a snapshot; a held Space gives one capture; 4 photos and values in the list; × and "Clear all" work, and the panel hides when empty.
- `uv run pytest`: 1118 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

### Next (after the go for `server.py`)

- `Services.staged` (the same store). `multimeter_read` pops all staged captures (oldest first; it waits while one is pending) and returns them, each with `capture_id`, `captured_at`, `age_s`, the meter result, and the photo. When the queue is empty it reads live. `live: true` skips the queue.
- A new read-only `staged_captures` tool.
- The texts: the tool docstrings, `EVIDENCE_RULES`, and `bench_instructions` (a staged photo is evidence of its capture time only). This needs an OK for `instructions.py`, which is dd-mcp's area.
- `mcp/README.md` (keys, store, tools).
- Tests: the pop order, the next call reads live, `live: true`, and a pop from a second server.

## Round 51: staged captures, the instruction text

With the orchestrator's OK for `instructions.py`:

- `EVIDENCE_RULES` rule 11: "Staged captures: the user can capture the phone photo and the meter reading on the monitor page (Space, C, or Capture). When the user says that they captured, or asks you to read the meter, call multimeter_read: staged captures come first (oldest first); it reads live only when none wait. A staged photo and value are evidence of their capture time only (captured_at), not of now."
- `bench_instructions` returns the evidence rules, so it has rule 11 too. Its description also has one sentence: the next `multimeter_read` returns staged captures first (rule 11).
- A test pins the two key phrases (`test_staged_capture.py::test_the_evidence_rules_name_staged_captures`).
- `uv run pytest`: 1119 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

The rule describes the `multimeter_read` behavior of the next part: it becomes true with the `server.py` step (I still wait for the go).

## Round 52: staged captures, part 2 (the tools) and N88-N91

With the orchestrator's go for `server.py`. I did not edit `bench_state.py` or `bench_journal.py`.

### What I did

1. **The wiring (brief item 5)**:
   - `Services.staged` holds the store (the state folder; `connect_services` gives the same store to the page capture).
   - `multimeter_read` pops all staged captures, oldest first, and returns them: `staged` (each with `capture_id`, `captured_at`, `age_s`, `state`, `origin`, the checked `meter` result with its bench notice, the `photo` facts, `notes`), `count`, and the note "Each photo and meter value shows the moment of its capture (`captured_at`, `age_s`), not now: say so". The photo of each capture follows as an image with its capture id (`_meta.staged: "phone_photo"`); with `include_image`, also its meter frames (`"meter_frame"`). The pop waits while a capture is pending (the vision call runs, at most 90 s). Only when no capture waits does it read live. `live: true` skips the queue (the captures stay). The other parameters apply only to a live read.
   - New read-only tool `staged_captures`: the waiting captures, oldest first, without images; they stay.
   - `multimeter_read` declares no output schema any more: the MCP client checks the structured result against the declared schema, and a staged batch is another shape. A live read returns the same `MeterResult` structure as before.
   - The page log: a `multimeter_read` with staged captures says "N staged captures (the moments of their capture)" and does not show it as one reading; its images are labeled as result images, not as model input.
   - The docstrings, and `mcp/README.md` ("Staged captures", the `multimeter_read` row, and the new `staged_captures` row).
2. **N88, the camera view**: `Services.phone_snapshot(record_view=False)` does not set `last_view`. The staged still uses it; the save and restore is gone. So an agent `phone_snapshot` at the same time keeps its own camera view in its capture record.
3. **N89, the crop during a capture**: `read_meter(..., require_crop=True)` checks the webcam crop before and after each frame. The staged capture uses it. Without the crop, the read stops ("the crop box was removed during the capture: no meter reading ..."), the tasks of the earlier (cropped) frames are cancelled, and the store gets no frame and no meter result. The agent's own `multimeter_read` is unchanged (the older item in the check).
4. **N90**: `StagedStore.photo()` and `delete()` accept only a capture id in the standard UUID form (`is_capture_id`); anything else gives None or False without a file access.
5. **N91**: the staged routes refuse with "only the monitor page can capture, delete, or clear staged captures (a same-origin request)". `refused()` takes the message.

### Tests

- New `mcp/tests/test_staged_tools.py` (10 tests):
  - `multimeter_read` pops two captures oldest first, with a photo and two meter frames each (`include_image`), and then the next call reads live (2 new vision calls); `staged_captures` lists them without removing them;
  - `live: true` skips the queue, and the capture stays;
  - **another server** (other `Services` on the same state folder) pops the page server's captures;
  - **N88**, the dd-qa probe B1: an agent photo at zoom 1, zoom 2, then a capture (its still 0.3 s) and an agent `phone_snapshot` (0.5 s) together: the agent photo's capture view has zoom 2.0. A scratch run with the old save and restore gives 1.0, so the test finds the bug;
  - **N89**: the crop cleared after the first frame, and while the second frame is read: every frame that the webcam read had the crop, at most one vision call, no frame and no meter result in the store, and the note;
  - N90: `photo("../secret")` and `delete("../secret")` touch no file;
  - N91: the refusal text;
  - the page log of a staged batch;
  - the store is in the test's private state folder.
- Playwright (`stagedcheck.py`): the page still works with `services.staged` (keys, text field, focused button, held key, 4 photos and values, delete, clear).
- `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15. Its sessions use a private `XDG_STATE_HOME`, so the run never pops the user's queue; I checked this before the run.
- `uv run pytest`: 1133 passed, 1 skipped. ruff and `prek run --files` on my files: pass.

## Round 53: staged readings can be recorded in any server (N92), and the parameters that were not applied (N93)

### What I did

- **N92**: when `multimeter_read` pops the queue, `staged_result` adds each staged meter result to `services.captures.meter_results` of the popping server under three kinds of id: the staged `capture_id`, the `meter.capture_id`, and each frame's `capture_id`. So `bench_record_measurement` works with any of them in the server that popped: also the bench session, a secondary, and a page server after a restart. An unsafe staged reading (the gate closed at an unknown point at capture time) gets its point name that way. The bench record keeps one entry per result, so the three ids do not make three entries.
- The `multimeter_read` docstring and the batch note say which id to record: the staged `capture_id` (or `meter.capture_id`).
- **N93**: a staged batch has `not_applied` (the given parameters that apply only to a live read: `expected_mode`, `expected_value`, `source` other than webcam, `frames` other than the default) and `not_applied_note`: "expected_mode, frames: given, but not applied to the staged captures (they were read at capture time, without them; for example a staged reading is not checked against expected_mode). Call multimeter_read with live: true to read the meter now with them." Without such parameters, the list is empty and the note is null.
- `mcp/README.md`: both points.

### Tests

- `mcp/tests/test_staged_tools.py`, 3 new tests:
  - a second server (the bench session, with the page server's state folder and bench file, and the open identity board fixture) pops three safe captures and records them by the staged id, the meter id, and a frame id: three safe residual points;
  - an unsafe staged capture closes the gate at an unknown point at capture time; the second server pops it and records it by its staged id as "L501.2": the point is `l501.2` (not safe), and the gate no longer names an unknown point;
  - `expected_mode` and `frames` given: `not_applied` names them, with the note; without them: empty.
- `test_staged_capture.py`: `capture_setup` takes a `reading` (the vision answer).
- `uv run pytest`: 1136 passed, 1 skipped. ruff and `prek run --files` on my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

## Round 54: staged captures with the meter image, SSE instead of polls, and use through a tunnel (docs/briefs/staged-remote-sse.md)

### 1. The meter image in each staged capture

- `staged.py`: `StagedCapture.meter_image_index()` gives the kept frame that gave the reading (the frame with the `capture_id` of the result; `meter_frames.combine` takes the first frame). `StagedItem.meter_image`, `StagedItem.other_frames`, and `StagedStore.meter_image(capture_id)` (only a UUID names a file, N90). The kept frames are crops only: the capture refuses a frame without the crop box (N89).
- `multimeter_read` (`staged_result`): for each capture, always the reading, the phone photo (`_meta.staged` `phone_photo`), and the meter image (`meter_crop`). `include_image` adds the other meter frames (`meter_frame`), so no image comes two times. The docstring says: the photo shows where the probes touch; the value is only the checked `meter` result, never the agent's reading of the image.
- Page: `GET /api/staged/{id}/meter.jpg`, `has_meter_image` in the list view, and a second thumbnail next to the phone photo.

### 2. SSE instead of polls

- Two new event kinds on the existing `/api/events` stream: `staged` (the list view) and `webcam` (the stream info). The page has one event stream for all live data.
- `ui/page_push.py` (`PagePush`): it starts with the page (`_serve`) and stops with it (`stop_page`). It does work only while a page listens.
  - Staged captures: the store listeners push the changes of this process at once (a capture, its result, a delete, a clear, a pop by this server's `multimeter_read`). The changes of other MCP server processes show through the folder version (the folder time and its file names), checked each 1 s. A queue that is not empty is listed again each 10 s (expiry, a pending time-out). A push goes out only when the page view changes (not for the age).
  - Webcam info: checked each 1 s; a push only when the page view changes (start, stop, the first frame, the size, an error), not at each frame.
- `Monitor.webcam_info()` (moved from the route; it also gives the info of another monitor that owns the webcam).
- The event stream has `X-Accel-Buffering: no`, so a proxy passes each event at once.
- Page: I removed `setInterval(loadStaged, 2000)` and `setInterval(refreshWebcamInfo, 3000)`. When the event stream opens (at start and after each reconnect), the page loads the staged list and the webcam info one time. After a capture, a delete, or a clear, the page does not load the list: the push brings it.

| Live data | Before | Now | Why |
|---|---|---|---|
| Staged list | poll each 2 s | `staged` event | moved |
| Webcam info | poll each 3 s | `webcam` event | moved |
| Phone state (status, focus, boxes) | `phone` event | no change | the server polls the phone each 1 s and pushes |
| Tool calls | `call` event | no change | |
| Webcam MJPEG, phone screen (H.264 or MJPEG) | 1 connection each | no change; paused in a hidden tab | image data, not events |
| MJPEG fallback draw each 100 ms | local timer | no change (skips a hidden tab) | local canvas drawing, not a request |
| Devices list | on open and Refresh | no change | it runs adb; only on the user's action |
| Reconnect timers | after an error | no change | |

### 3. Use through a tunnel

- New setting `--ui-allowed-origin` (env `DEBUG_DEVICES_UI_ALLOWED_ORIGINS`, a comma list; the flag can also come again), default empty. Each value must be an origin (`scheme://host[:port]`); the server keeps it in the form that a browser sends (lower case, no default port). A bad value stops the start with a clear error.
- `ui/origins.py` (`PageOrigins`): without the setting, the checks are as before. With it:
  - Host: also the host of an allowed origin (a tunnel that keeps the browser's Host). A tunnel that writes `127.0.0.1` (the frpc `hostHeaderRewrite`) also works.
  - A change (POST, DELETE): also an exact allowed origin. Another scheme, port, or host is refused.
  - `from_the_page` (the device and staged routes): the same-origin check, or an exact allowed origin (the tunnel can change the Host, so the origin decides).
- `LocalOnly` refuses with a JSON error text (before: the plain text "forbidden"), so the page shows the reason: "forbidden: a change from the origin ... (only 127.0.0.1, localhost, or --ui-allowed-origin)".
- Page feedback: a capture shows "sending…" at once (with the flash). The capture sound plays only after 202. A refused or failed POST shows "Capture failed: <error text>" in red and plays a low sound. The POST has a 10 s limit ("no answer in 10 s").
- Hidden tab: the webcam MJPEG and the phone screen stream (H.264 or MJPEG) stop, and they start again when the tab is visible. The event stream stays open.
- `.env.example`, `mcp/README.md` ("Live updates", "Use through a tunnel", the config table, the staged captures part): the tunnel must have its own login, because the page has none, it shows the full webcam frame, and it controls the phone and the camera.

### Tests

- `mcp/tests/test_staged_tools.py`: a staged pop without `include_image` has the reading, the phone photo, and the meter image; the meter image is the first frame (a webcam fake with another width for each frame). The pop test with `include_image` now expects photo, meter image, and the other frame.
- `mcp/tests/test_staged_capture.py`: the page route `meter.jpg` (and 404 for a bad id), `has_meter_image`.
- `mcp/tests/test_ui_origins.py` (new, 6 tests): the setting (default empty, flags, env comma list, bad values); `build_monitor` passes it on; `PageOrigins`; without the setting: the tunnel Host and a capture from the tunnel origin are refused (JSON error text); with `https://bench.example.org`: GET and POST with that Host and Origin pass, also with Host 127.0.0.1; another origin, scheme, port, or host is refused; no Origin is refused; the local page still works.
- `mcp/tests/test_page_push.py` (new, 4 tests): a capture, its result, and a pop by another store on the same folder (another process, no listener) reach the page as `staged` events; a change of this process pushes at once (with a 30 s folder check); the webcam info pushes only when the page view changes; without a page, the push reads nothing.
- Playwright (Firefox headless, fakes, a private state folder, port 0):
  - In 7 s idle: 1 `GET /api/staged` and 1 `GET /api/webcam/info` (the loads at the stream open). After a capture and a pop by another store: still 1.
  - Space: the list shows "4.98 V (confirmed)" through SSE; two thumbnails load (phone photo 640×480, meter image 64×48).
  - The pop by another store empties the list through SSE.
  - Hidden tab: the webcam image has no `src`; visible again: a new stream URL.
  - Through a local proxy that acts like frpc (Host rewritten to 127.0.0.1:<page port>), page opened as `http://bench.example.org:<proxy port>/` (Firefox maps the name to 127.0.0.1): the event stream is live. Without the setting: the POST gets 403, the page shows "sending…", then "Capture failed: forbidden: a change from the origin http://bench.example.org:<port> (only 127.0.0.1, localhost, or --ui-allowed-origin)" in red, count 0. With the setting: 202, "sending…" then empty, "1 staged".
  - Note: Playwright in Firefox does not change the Origin header with `route.continue_`, so I used the local proxy for the tunnel check.
- `uv run pytest`: 1147 passed, 1 skipped. ruff check and format: pass. `prek run --files` on each of my files: pass. `scripts/qa_mcp_stdio.py --skip-webcam`: 15/15.

### Is more needed for a slow tunnel

- Before: 3 long connections (events, webcam MJPEG, phone screen) plus a poll each 2 s and each 3 s. Now: the same 3 long connections, and requests only on the user's actions. A browser has 6 HTTP/1.1 connections for each host, so 3 stay free.
- The webcam MJPEG (10 frames per second, the full frame) is the largest load. The server sends only the newest frame to a slow reader, but the tunnel buffers (the 4 MB send queue) can still add seconds of delay. The hidden-tab pause helps. A possible next step: a lower frame rate and size for a page that is not local (for example `stream.mjpg?fps=2&max_side=640`). I did not do it in this round. Tell me if you want it.

No commit.

## Round 55: QA round 54 fixes N94, N95, and N97 (docs/reports/dd-qa.md "Round 54 (SSE, tunnel origin): check")

### N94: a browser change comes only from the page itself

- `ui/origins.py`: `PageOrigins.from_the_page` accepts two cases:
  - The page itself: only for a local Host (127.0.0.1 or localhost), and the Origin must be `http://` plus the Host of the request (both in the form that a browser sends).
  - An exact origin of `--ui-allowed-origin`.
- I removed `origin_allowed`. It accepted any port and any scheme for a local host name.
- `LocalOnly` now uses `from_the_page` for each change (POST, DELETE) that has an Origin. These origins get 403:
  - a local web page on another port or scheme, for example `http://localhost:8080`, `https://127.0.0.1:5173`, or `https://127.0.0.1:18766`;
  - the other local name, for example `http://localhost:18766` with the Host `127.0.0.1:18766`;
  - `null`.
- A request without Origin (a local process, another MCP server) works as before, and the routes check it.
- A tunnel Host is not "the page itself". With the setting, only the exact allowed origin passes. So `http://bench.example.org` with the Host `bench.example.org` is still refused.
- The error text names the page origin, for example: "forbidden: a change from the origin http://localhost:8080: only from the page itself (http://127.0.0.1:18766) or --ui-allowed-origin".

### N95: any tunnel needs its own login

- `mcp/README.md` "Use through a tunnel" says, in bold, that any tunnel to the page must have its own login, also without `--ui-allowed-origin`. The reason: a tunnel that writes the Host `127.0.0.1` reaches every read (the page, the webcam stream, the phone screen, the photos), and the Host check cannot see it. The setting only lets the changes through too.
- The README also has a list of the checks (Host, a browser change, no Origin).
- `.env.example`, the config table, and the `--help` text of the setting say the same.
- For the user: check that the frpc tunnel has a login now.

### N97: one capture for each key press

- Page: `newRequestId()` makes a UUID (version 4) for each key press, and the capture POST sends `{"request_id": ...}`. It uses `crypto.getRandomValues`, because `crypto.randomUUID` needs https or localhost, and an http tunnel page has neither.
- Server:
  - `CaptureBody`: `request_id` must be a UUID in its standard form, else 400. An empty body works as before.
  - `StagedCapturer.capture(request_id)` keeps the first answer for each id (the last 64 ids, in memory). A repeat gets the same answer: the same capture (202, the same `capture_id`) or the same error (409). It never makes a second capture.
  - A plain repeat adds no second log row (`knows`).
  - Two copies that come at the same time wait for the one answer.
  - A cancelled first request forgets its id.
- README (the staged captures part): the request id.

### Tests

- `mcp/tests/test_ui_origins.py`:
  - `from_the_page` refuses `http://localhost:8080`, `https://127.0.0.1:18766`, `http://127.0.0.1:5173`, and `http://localhost:18766` for the Host `127.0.0.1:18766`.
  - dd-qa's probe: `DELETE /api/settings/crop` with `http://localhost:8080`, `https://127.0.0.1:5173`, `https://127.0.0.1:18766`, or `null` gets 403, and the crop stays. This is tested without and with the setting, and the error text is checked.
  - The page itself and a request without Origin can still clear the crop.
  - The tunnel origin with the setting (Host `127.0.0.1`) can clear it.
- `mcp/tests/test_staged_capture.py`:
  - The same `request_id` two times gives one capture and the same `capture_id`, and the plain repeat adds no log row.
  - Two copies at the same time give one capture.
  - A bad id gets 400.
  - A repeat of a refused capture (full queue) is refused too.
- Playwright (Firefox headless, fakes, a private state folder, port 0; no webcam lookup, so nothing asked 18766):
  - The local page: the same results as in round 54.
  - The frpc-like proxy: without the setting, 403, and the page shows the new error text in red. With the setting, 202 and "1 staged".
  - N97: a TCP proxy like a bad tunnel (Host rewritten) sends the first capture POST to the server and drops the answer; 1 s later, the browser connection closes. Firefox sent the POST again (2 POSTs through the proxy).
    - Now: 1 capture, 1 log row, and the page shows "1 staged" with no error.
    - The old behavior (the script patched the server so it ignores the id): 2 captures from one key press.
  - Chromium is not installed here, so I did not check it.
- `uv run pytest`: 1153 passed, 1 skipped. ruff check and format: pass. `prek run --files` on each changed file: pass. `XDG_RUNTIME_DIR=<private dir> scripts/qa_mcp_stdio.py --skip-webcam`: 15/15, and the private runtime folder stayed empty.

N96 stays in the backlog. No commit.

## Round 56: one more send of a capture after a network error (N98, docs/reports/dd-qa.md "Round 55: check")

### What I did

- `app.js` `stagedPost`: when the capture POST ends in a network error (fetch rejects with a `TypeError`: no HTTP answer), the page shows "sending again…". Then, after 300 ms, it sends the same body (the same `request_id`) once more. The server gives the first answer to that id (N97), so a second send never makes a second capture.
- No second send in these cases:
  - A refusal (an HTTP error with its text, for example 403 or 409) shows its text at once.
  - No answer in 10 s also shows at once, so the longest wait stays 10 s.
- When both sends end in a network error, the text is "Capture failed: <error> (sent 2 times; if the server took it, the list shows it)". The server can have taken the capture, and the list (SSE) shows the truth.
- `mcp/README.md` (the staged captures part): the one more send.

### Test: a proxy like the bad tunnel

The proxy works on TCP:
- It writes the Host `127.0.0.1:<page port>` and `Connection: close` on each request, so each POST uses a new connection.
- For a capture POST, it passes the request to the server and drops the answer. 1 s later, it closes the browser connection.
- Setup: headless Chromium (the local Playwright build 1194) and Firefox, fakes, a private state folder, port 0. Nothing asked 18766.

| Browser, case | POSTs at the proxy | Page texts | Captures, log rows |
|---|---|---|---|
| Chromium, first answer dropped, the page of a1d752d (before) | 1 | "sending…", "Capture failed: Failed to fetch" (N98) | 1, 1 |
| Chromium, first answer dropped (now) | 2 | "sending…", "sending again…", "" | 1, 1 |
| Chromium, all answers dropped | 2 | ..., "Capture failed: Failed to fetch (sent 2 times; if the server took it, the list shows it)" | 1, 1 |
| Chromium, refused (no `--ui-allowed-origin`) | 1 | "sending…", "Capture failed: forbidden: a change from the origin ..." at once | 0, 0 |
| Firefox, first answer dropped | 2 (Firefox sends it again by itself) | "sending…", "" | 1, 1 |
| Firefox, all answers dropped | 20 (Firefox's own sends, 2 times from the page) | ..., "Capture failed: NetworkError when attempting to fetch resource. (sent 2 times; ...)" | 1, 1 |
| Firefox, refused | 1 | the refusal text at once | 0, 0 |

- In each case the page count agrees with the store ("1 staged" or "0 staged").
- `uv run pytest`: 1153 passed, 1 skipped (no Python change). ruff: pass. `prek run --files` on `app.js` and `mcp/README.md`: pass.

No commit.
