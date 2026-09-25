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
