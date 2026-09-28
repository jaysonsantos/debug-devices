# dd-qa report

## What I did

### Phase 1

- Probed the webcam and captured one frame.
- Wrote `scripts/fake_phone.py`: a fake phone that implements `docs/phone-api.md`, with a self-check.
- Wrote `scripts/qa_contract.py`: contract checks against any base URL.
- Wrote `scripts/fake_adb.py`: a fake `adb` so that the MCP can reach the fake phone. It never calls the real adb.
- Wrote `scripts/multimeter_capture.sh`: captures frames and a CSV for the accuracy check.
- Wrote `docs/qa.md`: the test plan.

I did not edit `android/` or `mcp/`. I did not commit.

## Phase 1: hardware probe

- `/dev/video0`: `PC-LM1E` (uvcvideo), USB bus `usb-0000:06:00.3-6.4`. `/dev/video1` is its metadata node.
- Formats (`v4l2-ctl -d /dev/video0 --list-formats-ext`):
  - MJPEG: 1920x1080 at 30/25/20/15/10/5 fps, and 1280x1024, 1280x720, 1024x576, 960x720, 864x480, 640x480, 640x360, 352x288, 320x240 at 30 fps.
  - YUYV: 1920x1080 and 1280x1024 at 5 fps only, 640x480 at 30 fps.
- Capture that works (1.4 s, 86 KB):

  ```sh
  ffmpeg -hide_banner -loglevel error -y -f v4l2 -input_format mjpeg -video_size 1920x1080 \
    -i /dev/video0 -vf "select=gte(n\,10)" -frames:v 1 /tmp/dd-qa/frame_1080p.jpg
  ```

- Recommendation: MJPEG 1920x1080, and skip about 10 frames for the exposure to settle.
- The webcam points at a PROSTER T21D multimeter. The meter is in the lower half of the frame, at a low and oblique angle.
- The LCD is blank. The meter is off, or the selector is at OFF. The LCD fills about 12% of the frame width.
- The frame also shows other objects: photos of people and a medicine label. The multimeter tool sends the full frame to OpenRouter. See the open items.

## Phase 1: what works

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | `self-check PASSED`: 5 modes (default, fixed zoom, no flash, not ready, capture fails), all checks pass |
| `python3 scripts/fake_phone.py --port 18765 --snapshot /tmp/dd-qa/frame_1080p.jpg` then `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict` | 11/11 checks pass. The snapshot check reads 1920x1080. |
| `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18766 --expect not-ready` (fake phone with `--not-ready`) | 2/2 checks pass |
| `python3 scripts/qa_contract.py` with no server | 0/11, each check reports `Connection refused`, exit code 1, no traceback |
| `scripts/fake_adb.py -s 192.0.2.43:5555 shell input tap 1 1` | `error: device '192.0.2.43:5555' not found`, exit 1 |
| `uvx ruff format scripts/ && uvx ruff check scripts/` | Clean, with the repo `pyproject.toml` settings |
| `nix run nixpkgs#shellcheck -- scripts/multimeter_capture.sh` | Clean |
| `printf '\n\n' \| scripts/multimeter_capture.sh /tmp/dd-qa/acc 2` | 2 frames and `readings.csv` |

## Phase 2

### What I did

- Ran `scripts/qa_contract.py --strict` against the real phone `0a1b2c3d`, before and after the new APK (installed 18:26).
- Changed `scripts/fake_phone.py` and `scripts/qa_contract.py` to match the new `docs/phone-api.md` (proposals 1-5, 405 `method_not_allowed`).
- Wrote `scripts/qa_mcp_stdio.py`: MCP tool tests over stdio with the fake phone, the fake adb, and a mock OpenRouter.
- Ran the MCP over stdio against the real phone, with one live `webcam_snapshot` and one live `multimeter_read`.
- Ran the Android unit tests and the MCP unit tests.
- Reviewed both sides against the contract.

I used only `adb -s 0a1b2c3d`. No command went to the Fire TVs. I did not edit `android/` or `mcp/`. I did not commit.

### Changes to the QA tools

- `qa_contract.py`: new error code `method_not_allowed` (405). New checks `method_not_allowed`, `snapshot_keeps_torch`, and `after_start` (flag `--after-start`). The case "both `ratio` and `step`" is now a normal check.
- `qa_contract.py --strict` now adds: `{"ratio": 1e400}`, `{"step": "IN"}`, `PUT /v1/zoom`, and `DELETE /v1/status`.
- `fake_phone.py`: a wrong method on a known path returns 405 `method_not_allowed`. It accepts `PUT` and `DELETE` only to refuse them. The self-check uses `--after-start` on each new server.
- `qa_mcp_stdio.py`: `phone_snapshot` now checks the default downscale (`max_side` 1568) and `max_side=0` (full size).
- `docs/qa.md`: the new checks, the automatic MCP run, the wait after an app start, and the `nix develop` command for Gradle.

### Results

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED: default 14/14, fixed zoom 14/14, no flash 14/14, not ready 3/3, capture fails 13/13 |
| `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict` (old APK) | 10/11. Fail: `zoom_bad_request` (bug 2) |
| Same, new APK, `--after-start`, run at 18:27 | 3/14. The app crashed after `status` (bug 1) |
| Same, new APK, `--after-start`, 6 s after `am start` | 13/14. Fail: `zoom_bad_request` (bug 2). `method_not_allowed`, `snapshot_keeps_torch`, and `after_start` pass. Snapshot 3060x4080, about 1.45 MB, about 0.85 s. |
| `uv run python scripts/qa_mcp_stdio.py --snapshot /tmp/dd-qa/frame_1080p.jpg --real-adb-several-devices` | 19/19 (see the list below) |
| MCP over stdio against `0a1b2c3d` (scratch driver, settings from `.env`) | 11/11 phone calls OK: connect, status, zoom in x2, ratio 100 gives 10.0, ratio 1, torch on, snapshot (torch stays on), torch off |
| `nix develop .. --command ./gradlew :app:testDebugUnitTest --rerun` (in `android/`) | BUILD SUCCESSFUL. `ApiServerTest` 18/18, `ZoomLogicTest` 15/15 |
| `./gradlew testDebugUnitTest` outside the dev shell | Fails: no Java 17 toolchain. Use `nix develop`. |
| `uv run pytest -q` | 48 passed |
| `uv run ruff check` | 8 findings, all in new dd-mcp files (`scrcpy.py`, `ui/`, `webcam_stream.py`). `scripts/` is clean. |
| `curl http://192.0.2.116:8765/v1/health` (phone Wi-Fi address) | No connection. The server listens on `127.0.0.1:8765` only. |

`qa_mcp_stdio.py` cases: stdout is JSON-RPC only; 7 tools listed; `phone_connect` forwards `tcp:<port> tcp:8765` with the fake serial; status; zoom (ratio, step, clamp, both or neither argument gives a tool error); torch; snapshot (default and `max_side=0`); `save_path`; 409, 503, and 500 become tool errors with the code; mock `multimeter_read` (model `openai/gpt-6-luna`, `json_schema` with `strict: true`, one JPEG data URL, bearer token sent); text that is not JSON gives a tool error; JSON in a code fence is accepted; `webcam_snapshot` 1568x882; phone down gives a tool error and the server stays up; empty key gives a tool error that names `OPENROUTER_API_KEY`; missing webcam gives a tool error with the path; real adb with no serial and 3 devices gives "several adb devices" and sends no command to a device.

### Live multimeter test (one call)

- `webcam_snapshot`: OK, 1920x1080, 212,348 bytes, 4.6 s. This call ran before the `max_side` change was in the server process.
- The frame shows the PROSTER T21D. It faces the camera, and the LCD is on. The LCD shows about `0.02`. I cannot read the unit at this resolution.
- `multimeter_read include_image=true`: tool error after 20.5 s. "OpenRouter returned no message content". The body had `"model": "openai/gpt-6-luna"`, `"provider": "OpenAI"`, and `"finish_reason": "length"`. See bug 3.
- The key did not go into any output.
- dd-mcp made one live call at about the same time, and it succeeded: `0.02` V, `dc_voltage`, confidence 0.62. Thus the failure is intermittent.

### Bugs

Severity: high = crash or wrong data, medium = contract break, low = small.

1. **High. The app crashes when an API zoom arrives during the start state.** `android/app/src/main/java/dev/jayson/debugdevices/camera/MainActivity.kt:63-67` runs `camera.applyStartState()` in `lifecycleScope.launch`. The API already answers `/v1/status` with 200 at that time. A `POST /v1/zoom` makes CameraX cancel the start-state `setZoomRatio`. `CameraController.kt:123-128` (`runControl`) turns the `OperationCanceledException` into an `ApiException`. Nothing catches it in that coroutine, so the main thread dies.
   - Steps: `am force-stop`, `am start`, then send `POST /v1/zoom {"ratio":3}` as soon as `/v1/status` returns 200.
   - Expected: 200, or 503 `camera_not_ready` until the start state is set. Actual: `FATAL EXCEPTION: main ... ApiException: Cancelled due to another zoom value being set.` (logcat crash buffer, 18:27:49 PID 2387 and 18:28:44 PID 4630).
   - Fix proposal: catch the cancel in `applyStartState` (a newer zoom wins, so the start state is not needed). Or return 503 until the start state is set.
2. **Medium. `{"ratio": "2"}` returns 200 and sets the zoom to 2.0.** The contract gives `ratio` as a number. `android/app/src/main/java/dev/jayson/debugdevices/camera/Models.kt:39` (`val ratio: Float?`) with `ApiJson` (`Models.kt:8`): kotlinx.serialization reads a quoted number into a `Float`, also when `isLenient` is off. `{"ratio": "abc"}` returns 400 correctly. There is no unit test for this case.
   - Fix proposal: decode `ratio` as a `JsonPrimitive` and refuse `isString`, or use a custom serializer.
3. **Medium. The live `multimeter_read` can fail with `finish_reason: "length"`.** `mcp/debug_devices_mcp/constants.py:91` sets `MAX_TOKENS = 1000`. `openai/gpt-6-luna` is a reasoning model. Its reasoning tokens can use the full budget before it writes the JSON. `mcp/debug_devices_mcp/multimeter.py:277` then reports "no message content". It does not give the cause.
   - Fix proposal: raise `max_tokens` (for example 4000), or set a low reasoning effort in the request. Give a clear error when `finish_reason` is `length`. Do not retry with the same budget.
4. **Low. A two-request race gives the wrong error code.** Two zoom requests at the same time: CameraX cancels the first. `CameraController.kt:126-127` maps the cancel to 503 `camera_not_ready`, but the camera is ready. The MCP (`mcp/debug_devices_mcp/server.py`) treats 503 as "not ready".
   - Fix proposal: put zoom and torch calls behind one `Mutex`, like `captureLock` (`CameraController.kt:35`).
5. **Low. Any unexpected exception becomes 503 `camera_not_ready`.** `android/app/src/main/java/dev/jayson/debugdevices/camera/ApiServer.kt:47-48` (`exception<Throwable>`). A programming error then looks like a camera that is not bound. The contract has no 500 code for this case. Proposal: log the stack trace and keep 503, or add a contract code `internal_error` (500).
6. **Low. A 200 response with a bad body gives an unwrapped pydantic error.** `mcp/debug_devices_mcp/phone_api.py:109-119` call `model_validate_json` outside `_request`. A `ValidationError` is not a `PhoneError`, so `tool_errors()` in `server.py` does not change it into a clear `ToolError`. Proposal: catch `ValidationError` and raise `PhoneProtocolError`.
7. **Low. The downscaled image can be larger than the original.** `phone_snapshot` with the default `max_side` 1568 turned a 1920x1080 JPEG of 86,499 bytes into 1568x882 of 123,595 bytes. The re-encode quality is higher than the source quality. Proposal: keep the source bytes when the source is already small, or lower the quality.
8. **Low. The error preview is mostly white space.** `mcp/debug_devices_mcp/multimeter.py:269` takes the first 500 characters of the body. OpenRouter sends keep-alive white space before the JSON, so the preview cut off `finish_reason`. Proposal: `response.text.strip()[:ERROR_BODY_PREVIEW_CHARS]`.

### Contract gaps (open)

- The contract does not say that a POST body needs `Content-Type: application/json`. The app returns 400 without it (`ApiServer.kt:56`). The MCP always sends the header. Proposal: write the rule in `docs/phone-api.md`.
- The contract does not say what the API returns between "camera bound" and "start state set". See bug 1.

### Notes

- dd-mcp tested the phone at the same time. My restarts of the app (`am force-stop`, `am start` on `0a1b2c3d`) can make their calls fail for a few seconds. Their traffic probably caused the crash at 18:28:44.
- dd-mcp ran `ruff --fix` on my files at 18:24. I checked them: ruff is clean, and the self-check passes.
- `origin` is not set in this repository. I could not make sure that the branch is current with `origin/main`.
- Not done: the manual phone steps in `docs/qa.md` section 3 that need a person (preview, LED, lock screen, permission), and the multimeter accuracy check (section 4). The meter now faces the webcam, so the accuracy check can start.

## Round 2

### What I did

- `scripts/qa_mcp_stdio.py`: the MCP server starts with `--no-ui` (constant `SERVER_ARGS`).
- `scripts/fake_phone.py`: new error code `internal_error` (500). New modes `--internal-error` and `--start-delay S` (503 `camera_not_ready` for `S` seconds, then the start state). It already refused `ratio` as a string.
- `scripts/qa_contract.py`:
  - new error code `internal_error` (500);
  - `{"ratio": "2"}` is a normal check, so `--strict` also runs it;
  - new check `concurrent_zoom`: 8 zoom requests at the same time, each must get 200 with its own ratio;
  - new mode `--expect starting`: only 503 `camera_not_ready` until the first 200, and the first 200 shows the start state;
  - new mode `--expect starting-race`: the bug 1 reproduction. It sends zoom requests during the start, and then the app must answer for 3 s.
- `docs/qa.md`: the new modes and checks, and the webcam note for the MCP run.
- I waited for `docs/reports/dd-android.md` to report the round-3 APK (installed 18:44:38, `lastUpdateTime`). I did not use the phone before that.

### Results

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED: default 15/15, fixed zoom 15/15, no flash 15/15, not ready 3/3, capture fails 14/14, starting 15/15, starting race 15/15, internal error 4/4 |
| `am force-stop`, `am start`, then at once `qa_contract.py --strict --expect starting-race` (3 runs) | 15/15 each time. 13, 14, and 15 x 503 before the first 200 zoom. The app stayed up. |
| `am force-stop`, `am start`, then at once `qa_contract.py --strict --expect starting` | 15/15. 18 x 503, then the first 200 had zoom 1.0 (min) and torch off. |
| `am force-stop`, `am start`, wait 6 s, `qa_contract.py --strict --after-start` | 15/15. Snapshot 3060x4080, 1.07 MB. |
| `adb -s 0a1b2c3d logcat -d -b crash` after these runs | No `FATAL` line |
| `curl` `{"ratio":"2"}` to `/v1/zoom`, `{"enabled":"true"}` to `/v1/torch` | 400 `bad_request` both |
| `nix develop .. --command ./gradlew :app:testDebugUnitTest --rerun` | BUILD SUCCESSFUL. `ApiServerTest` 26/26, `ControlGateTest` 4/4, `ZoomLogicTest` 16/16 |
| `uv run python scripts/qa_mcp_stdio.py --snapshot /tmp/dd-qa/frame_1080p.jpg --real-adb-several-devices --skip-webcam` | 14/15. The failure is finding R2-1. |

### Status of the round 1 bugs

| Bug | Status |
|---|---|
| 1. Crash on zoom during the start state | Fixed and verified on `0a1b2c3d` (`starting-race` 3/3, no crash) |
| 2. `ratio` as a string | Fixed and verified (400) |
| 3. `finish_reason: length` | Fixed in code: `MAX_TOKENS = 4000`, `REASONING_EFFORT = "low"`, a clear error for `length` (`mcp/debug_devices_mcp/multimeter.py:295`). No new live call, so it is not verified live. |
| 4. Two requests cancel each other | Fixed and verified (`concurrent_zoom` passes on the phone) |
| 5. Unexpected error gives 503 | Fixed in the app (`internal_error`). The unit tests cover it. It cannot be triggered on the phone. |
| 6. Unwrapped pydantic error | Fixed in code (`mcp/debug_devices_mcp/phone_api.py:149-154`) |
| 7. Downscaled image larger than the original | Fixed: 1920x1080 at 86,499 B gives 1568x882 at 86,475 B |
| 8. Error preview is white space | Fixed in code (`multimeter.py:285` strips the body) |

### New findings

- **R2-1, low. `multimeter_read` opens the webcam before it checks the key.** `mcp/debug_devices_mcp/server.py:284-285` calls `capture_jpeg()`, then `read_multimeter()`. When the key is empty and the webcam is busy or missing, the caller gets a webcam error and not "OPENROUTER_API_KEY is not set". Proposal: check the key before the capture.
- **R2-2, medium. With the UI on, one MCP process holds `/dev/video0` for the whole session.** Another agent ran `uv run debug-devices-mcp --adb-serial 0a1b2c3d` with the UI on. Its child `ffmpeg ... -f v4l2 -i /dev/video0 -vf fps=10 ... -f mpjpeg pipe:1` kept the device open. Every other process then gets `Device or resource busy`: a second Claude session, `scripts/multimeter_capture.sh`, and the webcam cases of `qa_mcp_stdio.py`. Proposal: start the shared stream only while the monitor window is open or while a tool call needs a frame. Stop it after an idle time. Also put the busy case in the tool error ("the webcam is in use by PID ...").
- I did not stop the process of the other agent. Thus the webcam cases of `qa_mcp_stdio.py` did not run in round 2. In round 1 they passed 4/4.

### Open

- Run `qa_mcp_stdio.py` with the webcam cases when `/dev/video0` is free.
- Live check of bug 3 (one more `multimeter_read` with the new token budget), if the orchestrator allows it.
- The manual phone steps (`docs/qa.md` section 3) and the multimeter accuracy check (section 4).

## Round 2, part 2 (shared webcam)

- R2-1 is fixed: `mcp/debug_devices_mcp/server.py:306` calls `services.vision.require_api_key()` before the capture. `multimeter_no_key` passes while the webcam is busy.
- R2-2 is known. dd-ui added `SharedWebcam` (`mcp/debug_devices_mcp/remote_webcam.py`). I did not stop the dd-monitor process.
- `scripts/qa_mcp_stdio.py` now writes the stderr of each MCP session to `/tmp/dd-qa/mcp_<session>.stderr.log`. The `webcam_snapshot` case reports `path=shared (monitor)` when the log has "using the frames of the monitor". Otherwise it reports `path=local`.
- `uv run python scripts/qa_mcp_stdio.py --snapshot /tmp/dd-qa/frame_1080p.jpg --skip-webcam`: 14/14.
- At first, the dd-monitor process ran the old code (`/api/whoami` returned 404). The user restarted it. At 18:58:46, `GET http://127.0.0.1:18766/api/whoami` returned 200: `app` `debug-devices-monitor`, PID 426013, `webcam_running: true`, crop `591,87,509,804`. Its ffmpeg (PID 426088) holds `/dev/video0`.
- `uv run python scripts/qa_mcp_stdio.py --snapshot /tmp/dd-qa/frame_1080p.jpg --real-adb-several-devices`: 19/19.
  - `webcam_snapshot`: 509x804, 89,975 bytes, `path=shared (monitor)`. The frame has the crop of the monitor.
  - Server log: `webcam /dev/video0: using the frames of the monitor at http://127.0.0.1:18766/`.
  - `multimeter_mock`, `multimeter_bad_output`, and `multimeter_fenced` pass through the shared path, with the mock OpenRouter only.
- The cropped frame (`/api/webcam/frame.jpg?cropped=true`) shows the full PROSTER T21D. The LCD fills about 45% of the crop width, which is much larger than in the full frame (12%). The LCD shows `OL` or `0.L`, and the probes are not connected.
- Round 2 status: all round 1 bugs and R2-1 are fixed. R2-2 is solved by the shared frame source.
- Open (dd-ui): the tool calls of a second process do not show in the log of the owner. The owner must use the known port (default 18766).
- Open (QA): the manual phone steps (`docs/qa.md` section 3) and the multimeter accuracy check (section 4). With the crop, the accuracy check can start. It needs a person to set the meter and fill `readings.csv`.
- No live `multimeter_read` from dd-qa. The webcam cases use the mock OpenRouter only.

## Round 3 (rotation, content type, background)

### What I did

- `scripts/fake_phone.py`:
  - new endpoint `POST /v1/rotation` (`{"degrees": 0|90|180|270}` or `{"auto": true}`);
  - new status fields `rotation_degrees` and `rotation_locked`;
  - a POST without `Content-Type: application/json` gives 400;
  - new mode `--background` (503 `camera_not_ready`, "Camera is not active");
  - new mode `--physical-rotation N` (the auto value);
  - the snapshot gets an EXIF orientation segment for the current rotation (1, 6, 3, 8).
- `scripts/qa_contract.py`:
  - `CameraStatus` has 7 fields, and `rotation_degrees` must be in (0, 90, 180, 270);
  - new checks `rotation`, `rotation_bad_request`, `post_needs_json_content_type`, and `snapshot_rotation`;
  - `snapshot_rotation` compares the displayed size (pixel size turned by the EXIF orientation), so it works for a phone that turns the pixels and for a phone that sets the EXIF tag;
  - the start state includes `rotation_locked: false`;
  - new mode `--expect background`;
  - `GET /v1/rotation` gives 405;
  - `--strict` adds the rotation bodies -90, 360, and null;
  - the restore puts back the rotation lock.
- `scripts/qa_mcp_stdio.py`: the snapshot cases expect the bytes with the EXIF segment of the fake.
- `docs/qa.md`: the new modes and checks.

### Results

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED: default 19/19, fixed zoom 19/19, no flash 19/19, not ready 3/3, capture fails 17/17, starting 19/19, starting race 19/19, internal error 4/4, background 3/3, lying at 270 19/19 |
| `uv run python scripts/qa_mcp_stdio.py --snapshot /tmp/dd-qa/frame_1080p.jpg --real-adb-several-devices` | 19/19. `webcam_snapshot` through the shared path (monitor). |
| `am force-stop`, `am start`, wait 6 s, `qa_contract.py --strict --after-start` on `0a1b2c3d` (APK 19:42:39) | 19/19. Auto rotation 90 (phone in landscape). Snapshot 4080x3060. |
| `snapshot_rotation` on the phone | Displayed sizes 0: 3060x4080, 90: 4080x3060, 180: 3060x4080, 270: 4080x3060. The app turns the pixels. |
| Visual check of lock 0 and lock 180 | The 180 snapshot is the 0 snapshot turned by a half turn. Correct. |
| `am force-stop`, `am start`, at once `qa_contract.py --strict --expect starting-race` | 19/19. 17 x 503, then 200. No crash. |
| Lock 180, HOME key, `qa_contract.py --strict --expect background` | 3/3: health 200, status, zoom, torch, rotation, and snapshot 503 `camera_not_ready` |
| `am start` (back to the front), `GET /v1/status` | `rotation_degrees` 180, `rotation_locked` true: the lock stays through the background |
| `adb -s 0a1b2c3d logcat -d -b crash` | No `FATAL` line |
| `nix develop .. --command ./gradlew :app:testDebugUnitTest --rerun` | BUILD SUCCESSFUL. 63 tests: `ApiServerTest` 29, `ControlGateTest` 4, `OrientationLogicTest` 10, `RotationStateTest` 4, `ZoomLogicTest` 16 |

At the end, I set the zoom to 1.0 and the rotation to auto. The app runs (PID 15447).

### Findings

- **R3-1, low. The snapshot has EXIF `Orientation` 0.** The app turns the pixels. Most snapshots then have the tag value 0 ("Unknown"), and lock 90 gave 1. The EXIF standard allows only 1 to 8. Viewers treat 0 as 1, so the image shows correctly. Proposal: always write 1 (normal) after the pixels turn.
- **Contract gap.** A rotation lock stays when the app goes to the background and comes back. Only an app start resets it to auto. dd-android reports the same. Proposal: write this rule in `docs/phone-api.md`.
- `snapshot_rotation` cannot tell 0 from 180, or 90 from 270, by size. I checked 0 and 180 by eye one time. A script check needs image content analysis, and I did not add it.

A process restart (Herdr) killed one run of `qa_mcp_stdio.py`. I ran it again. No process of mine was left.

## Round 4: QA review of the work up to 2026-09-28

### Scope

- The working tree at `e7f0da9`, with the uncommitted work of other agents (network discovery, "device gone" handling, meter decimal point). The brief items are in `e7f0da9`. The uncommitted work does not change their status.
- No real phone (not connected), no real webcam, no browser, and no request to the monitor page.
- Four read-only review agents compared the code with the briefs. I checked the safety findings and the main bugs in the code myself. These have "checked" in the lists below.
- File names without a directory: Python files are in `mcp/debug_devices_mcp/`, `test_*.py` files are in `mcp/tests/`, and Kotlin files are in `android/app/src/main/java/dev/jayson/debugdevices/camera/` (tests in `android/app/src/test/...`). `M/`, `T/`, `A/`, and `AT/` are short forms of these directories.
- Line numbers are from about 19:00 to 19:10. Other agents edited `server.py`, `ui/monitor.py`, and `app.js` at the same time, so some line numbers can be a few lines off.

### What I ran

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED in 11 modes: default, fixed zoom, ultrawide (new), no flash 29/29 each; not ready 3/3; capture fails 26/26; starting 29/29; starting race 29/29; internal error 4/4; background 3/3; lying at 270 29/29 |
| The committed fake phone with `--min-zoom 0.6`, then `python3 scripts/qa_contract.py --strict --after-start` | 27/29. The two new checks fail as they must: start zoom 0.6 (expected 1.0), and `preview_region` does not follow the rotation. This shows that the new checks find these faults. |
| `uv run python scripts/qa_mcp_stdio.py --skip-webcam --scratch <scratch dir>` (the server runs with `--no-ui`) | 14/15. The failure is safety finding S1 (see Bugs). |
| `uv run pytest -q` | 746 passed, 1 skipped |
| `uv run ruff check` and `uv run ruff format --check` | Clean |
| `nix develop .. --command ./gradlew :app:testDebugUnitTest --rerun` (in `android/`) | BUILD SUCCESSFUL. 141 tests in 13 suites, 0 failures |
| Not run | Real phone, webcam cases of `qa_mcp_stdio.py`, `prek`. The repository has no Playwright tests. |

### Changes to the QA tools

- `scripts/fake_phone.py`:
  - The start zoom is 1.0 when 1.0 is inside `[min, max]`, else min (contract). Before, it was always min.
  - `preview_region` follows the rotation: at 90 and 270, width and height swap.
  - New self-check mode `ultrawide` (`min_zoom_ratio` 0.6).
- `scripts/qa_contract.py`:
  - The start state uses the contract start zoom (the check expected min before, which is wrong for a phone with an ultrawide lens).
  - `--expect not-ready` and `--expect background` check 503 on `/v1/preview`, `/v1/camera`, `/v1/focus`, and `/v1/overlay`.
  - The content-type check covers the same four routes.
  - `check_overlay_visible` also checks that a `visible` body keeps the arrows.
  - New `--strict` checks: `preview_region_rotation`, `camera_both_fields`, and `preview_keeps_snapshot`.
- `scripts/qa_mcp_stdio.py`:
  - Each run uses a fresh `XDG_STATE_HOME`. Before, the server read the user's saved phone selection from `~/.local/state/debug-devices/ui-settings.json`, and `phone_connect` failed with the saved Wi-Fi serial. The run used the fake adb only, so no command went to a real device, and the file did not change.
  - `phone_snapshot` compares the image size with the fake still turned by `turn_degrees`, not the bytes: the server turns and flips each still.
  - New group `fire-tv`: a fake Fire TV in state `device`, the phone `unauthorized`, and no selection. It proves S1.
- `scripts/fake_adb.py`: the single-device mode accepts `forward --remove tcp:<port>` (it refused it before, so every `stop_phone` failed with the fake).
- `docs/qa.md`: the new checks, the state isolation, the `fire-tv` group, and the adb version warning for `--real-adb-several-devices`.

### Status of the brief items
#### docs/briefs/bench-feedback-1.md


| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| dd-android: `preview_region` in `CameraStatus` | done | `A/Overlay.kt:262-284`, `A/CameraController.kt:575-606`; tests `AT/OverlayLogicTest.kt:174-202`, `AT/ApiServerTest.kt:170`, `:404` | Zoom and flips are not inputs (contract gap C9). Install and check on the S22: not verified, no phone. |
| dd-ui 1a: `phone_highlight` gives `in_preview` per box and a warning | done | `M/highlight.py:83-126`, `M/server.py:462-497`; test `T/test_bench_feedback.py:42-65` | – |
| dd-ui 1b: `phone_point_to` | done | `M/pointing.py:426-434`, `:474-500`; test `T/test_bench_feedback.py:86` | – |
| dd-ui 1c: `board_locate_in_photo(highlight)` | done | `M/board/tools.py:855-866`, `M/server.py:418-430` | No test checks the visibility on this path. |
| dd-ui 1d: the page draws `preview_region` as a thin frame | done | `M/ui/static/app.js:1196-1217`, `style.css:480` | No test in the repository. |
| dd-ui 2a: a secondary server sends its boxes and arrows to the primary | done | `M/ui/monitor.py:440-445`, `M/ui/forward.py:231-242`, `M/ui/routes/ingest.py:33-36`, `:72-81`; tests `T/test_bench_feedback.py:108`, `:166` | Order problem: B-F9. |
| dd-ui 2b: source label | done | `app.js:1219-1224`, `M/ui/monitor.py:461-463` | – |
| dd-ui 2c: latest annotated snapshot | done | `app.js:93`, `app.js:1341-1347`, `index.html:124-127` | No test in the repository. |
| dd-ui 3: a secondary gets live frames from the primary | partly | `M/ui/routes/ingest.py:84-107`, `M/ui/remote_screen.py:55-96`, `M/ui/monitor.py:467-541`; tests `T/test_bench_feedback.py:140-201` | A secondary asks for frames only after its own `phone_connect` (`monitor.py:541`). Without it, the tools say "no phone screen stream", also when the primary streams (`M/pointing.py:55`). See B-F6. |
| dd-ui 4: `bench_start` has its own screen step | done | `M/ui/tools.py:123-134`; test `T/test_lazy.py:190-217` | – |
| dd-mcp 5: `board_parts_at_photo` | partly | `M/board/tools.py:875-972`; test `T/test_board_parts_at.py:30` | No mapping through the live tracker ("tracked"). Pixels of a newer photo of another size are not scaled (B-F2). |
| dd-mcp 6: registration reuse for a newer snapshot | partly | `M/evidence.py:37-62`, `:146-170`, `M/server.py:331-347`; tests `T/test_board_parts_at.py:52-87` | `phone_point_to` has no camera-view check (B-F1, checked). The docstring `M/board/tools.py:841` still says that the registered photo must be the last one. |
| dd-mcp 7: webcam controls for the meter, dim-LCD hint | done | `M/webcam_controls.py:104-222`, `M/multimeter.py:237-251`; test `T/test_webcam_controls.py:49-137` | No page setting yet (the brief says "later"). Suspect B-F10. |
| Addition: keep the board session over a server restart | partly | `M/board/session_store.py`, `M/board/tools.py:316-370`; tests `T/test_board_restart.py:56-112` | Every restored registration comes back stale, with a wrong reason (B-F7). The note goes only to the log. The page Board panel does not restore. |

#### docs/briefs/bench-feedback-2.md

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| 1 Multi-frame meter check and plausibility limit | partly | `M/meter_frames.py:131-202`, `M/server.py:703-750`, `M/config.py:86-94`; tests `T/test_meter_frames.py:48-115` | The limit checks only frames that are already confirmed (B-F4). `frames=1` confirms one frame with no agreement check. The default is 2 frames 1 s apart, not about 2 s. |
| 2 The user-confirmed meter mode is context | partly | `M/multimeter.py:449-529`, `M/bench_state.py:469-478`; tests `T/test_meter_frames.py:139-182` | No "no dial change seen" check. Recording a measurement erases the user's mode (B-F3, checked). |
| 3 `bench_measure` | done | `M/server.py:1123-1165`, `M/instructions.py:83`; tests `T/test_bench_measure.py:12-51` | It does not carry the app restart notice (item 7). |
| 4 Automatic re-registration (ORB and RANSAC) | partly | `M/tracking.py:123-163`, `M/pointing.py:59-66`, `:232-294`; tests `T/test_carry.py:58`, `:87` | Carry runs only when the scene watcher marks the registration stale. Without the live stream, a move is not seen. The test marks the change by hand. See B-F5. |
| 5 `phone_snapshot` crop | done | `M/snapshot_crop.py:24-93`, `M/server.py:672-682`; tests `T/test_snapshot_crop.py:34-47` | B-F8 (low). |
| 7 App restart notice | done | `M/app_restart.py`, `M/server.py:309-321`, `:1167-1196`, `app.js:1311-1315`; tests `T/test_app_restart.py:25-34` | Only on `phone_*` tools, and only when a status is read. The status that `phone_highlight` or `phone_zoom` returns does not trigger it. |
| 8 Board side labels | partly | `M/board/model.py:169-195`, `:255-290`, `M/board/constants.py:47-56`; tests `T/test_board_sides.py:43-126` | The mixed-file check counts over the whole board, not per area. In a mixed file, `pointer.plan` (`M/pointer.py:167`) and `board_match_marking` with `side` (`M/board/marking.py:83`) still rule parts out by the label. |
| 9 Bench record | done | `M/bench_state.py:92-95`, `:251-341`, `M/server.py:687`; tests `T/test_bench_state.py:237-296` | B-F3 is in the same function. |
| 10 `schematic_find` | done | `M/schematic.py:44`, `:255-374`, `M/config.py:184-192`, `flake.nix:135`; tests `T/test_schematic.py:113-211` | A note in `bench_instructions`, not an MCP resource (the brief allows both). |
| 11 Rotated markings and value codes | done | `M/board/rotation.py`, `M/board/marking_readings.py`, `M/board/value_code.py:59-100`; tests `T/test_marking_rotation.py:39-170` | 180 degrees and resistor codes only. |

#### docs/briefs/sync-fix.md

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| dd-android: `app_start_id` UUID v7, once per start, in `CameraStatus` | done | `android/.../AppStart.kt:8-28`, `CameraController.kt:79-80`, `Models.kt:34` | The app makes the id once per activity start (`MainActivity.kt:82`), not once per process. See contract gap C8. |
| dd-android: unit test (stable; new after a restart) | partly | `AppStartTest.kt:13-39`, `ApiServerTest.kt:170` | The test makes new `AppStart` objects, not a new `CameraController`. |
| dd-android: install and check `/v1/status` on the phone | not verified | – | The phone is not connected. |
| dd-ui: resend a setting only after an app restart or a real change | partly | `app_start.py:23-53`, `orientation.py:267-283`, `camera_choice.py:95-110`, `camera_choice.py:196-212`, `server.py:511-523` | (1) The first status after each server start counts as a connect (`app_start.py:27-44`), so every server start or reload sends once. (2) `PreviewSync` also sends when the wanted flips change, for example after a phone turn in Auto (`orientation.py:275-278`). (3) Bug B-S5. |
| dd-ui: one source of truth (the settings file) | done | `ui/settings.py:77-122`; test `test_settings_sync.py:90-103`, `:152-162` | The monitor keeps an in-memory copy and writes `markings_visible` from it (`monitor.py:262`, `:578`, bug B-S8). |
| dd-ui: a status that differs with the same id updates the local view and logs once | partly | `app_start.py:46-53`; test `test_settings_sync.py:121-133`; `app.js:875-884` | The page shows the phone value as text only. The toggle states do not follow the status. |
| dd-ui: old app: resend only at `phone_connect` | partly | `app_start.py:39-41`; test `test_settings_sync.py:136-149` | It also sends at the first status after a server start. |
| dd-ui: tests (no ping-pong, one resend after a restart, file change, old app) | done | `test_settings_sync.py:75-162` | The no-ping-pong check is loose (at most 6 sends). |

#### docs/briefs/p0-orientation.md

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| 1 One transform in pure functions, table tests | done | `orientation.py:40-143`, `server.py:525-541`, `images.py:102-128`; test `test_image_transform.py:131-148` | – |
| 2 `turn_degrees` and flips in `SnapshotInfo` and in the tool text | done | `server.py:184-203`, `server.py:623-645`; test `test_image_transform.py:244-250` | Confirmed by `scripts/qa_mcp_stdio.py` (the text part has `turn_degrees`). |
| 3a Saved images | done | `server.py:626` | No test with a turn other than 0. |
| 3b `multimeter_read(source=phone)` | done | `server.py:649-661` | No test with a turn. |
| 3c Focus points | done | `focus.py:186-190`; test `test_image_transform.py:252-258` | – |
| 3d Boxes and arrows | done | `highlight.py:166-181`, `pointer.py:72-79`, `pointing.py:424-433`; test `test_image_transform.py:151-170` | Arrows through a turn: only the angle math has a test. |
| 3e Registration and tracking | done (code) | `pointing.py:397`, `pointing.py:446`, `evidence.py:54` | No test with a turn. |
| 3f Page snapshot and full-screen view | done (code) | `monitor.py:104-113`, `monitor.py:363-382`, `app.js:1243-1287` | No committed test with a turn; `test_orientation.py:139-175` tests flips only. |
| 3g Preview flips in the phone frame | done | `orientation.py:138-142`; test `test_image_transform.py:173-177`, `:281-297` | – |
| 4 Contract | done | `docs/phone-api.md:77` | – |
| 5 Acceptance (target, every choice and flip, sizes, focus, boxes, rotation, restart, Playwright) | partly | `test_image_transform.py:59-78`, `:225-278`; `scripts/make_orientation_target.py` | No Playwright test in the repository. The server-level test uses no flip and Flip H only. After a rotation or restart, only the image is checked. |
| 6 Real-phone steps in the report | done | `docs/reports/dd-ui.md:1193-1204` | The steps did not run: no phone. |

#### docs/briefs/overlay-layout.md

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| Contract `tag` and the layout spec | done | `docs/phone-api.md:89`, `docs/overlay-layout.md:20-42` | – |
| One pure function per language, same vectors | done | `overlay_layout.py:330-359`, `OverlayLayout.kt`; tests `test_overlay_layout.py:41-112`, `OverlayLayoutTest.kt:26-146` | – |
| dd-android: tags | done | `Overlay.kt:20-21`, `Overlay.kt:139-142`, `OverlayLayout.kt:98`; test `ApiServerTest.kt:597-603` | – |
| dd-android: legend | partly | `OverlayLayout.kt:193-207`, `OverlayView.kt:157-177` | Bug B-S1: an outside legend is always drawn top left, at 40 % and not 50 %. |
| dd-android: badges, minimum size, outline, colours, arrows | done | `OverlayView.kt:28-36`, `OverlayView.kt:109-148` | Bug B-S2 (colour and tag shift). |
| dd-android: install after the bench | pending | – | The phone is not connected. |
| dd-ui: `phone_highlight` annotated image | partly | `overlay_draw.py:46-116`, `highlight.py:201-212` | Bugs B-S3 and B-S4. The inset has no tags. |
| dd-ui: page snapshot view and inset | partly | `app.js:1190-1242`, `app.js:1397-1569` | The page inset has no tags and no dark outline (`app.js:1557-1567`). An outside legend is clamped into the view and can cover the picture (`app.js:1509-1514`). |
| dd-ui: vectors file | done | `docs/overlay-layout-vectors.json` (10 cases), `scripts/make_overlay_vectors.py`; test `test_overlay_layout.py:89-109` | – |
| dd-ui: optional tag per box; result gives tags and legend | done | `highlight.py:44-80`, `server.py:359-367`, `server.py:462-497`; test `test_highlight.py:341-367` | – |

#### docs/briefs/markings-toggle.md

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| dd-android: visible flag, request rules, `overlay_visible` | done | `CameraController.kt:134-136`, `CameraController.kt:541-547`, `Overlay.kt:37-52`, `Overlay.kt:113-126`; test `ApiServerTest.kt:557-590` | – |
| Button in the live and snapshot full-screen bars and in the panel | done | `ui/static/index.html:39`, `:110`, `:120` | – |
| Key "k" in the full-screen views | done | `app.js:95-96`, `app.js:1378-1386` | – |
| One persisted page state | done | `ui/settings.py:47`, `camera_choice.py:224-261`, `monitor.py:447-450` | – |
| Off hides the page markings | partly | `app.js:1351-1363`, `style.css:498-503` | The "agent's view" image with drawn-in boxes (`index.html:124-127`) stays visible. |
| Send `{"visible": ...}` to the phone | done | `server.py:373-386`, `camera_choice.py:264-294`, `phone_api.py:395-403` | – |
| Markings kept; new highlights while hidden; note in the tool result | partly | `server.py:168-171`, `server.py:349-357`, `server.py:1036`; test `test_markings.py:76-93` | Bug B-S7: the `board_locate_in_photo(highlight)` result has no note. |
| Old app: hide the page markings only, with a note | done, differs | `server.py:173`, `server.py:369-401`; test `test_markings.py:96-138` | The code removes the boxes from the phone and sends them again when shown. The dd-ui report describes the brief text, not the code. |
| Tests (Playwright and fake phone) | partly | `test_markings.py:65-182`, `scripts/qa_contract.py` (`check_overlay_visible`) | No Playwright test in the repository. |

#### docs/briefs/adb-wifi.md

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| 1a List: serial, transport, state, model, product | done | `mcp/debug_devices_mcp/adb.py:48-57`, `devices.py:47-61`, `devices.py:137-181`; test `mcp/tests/test_devices.py:82-100` | Only an IPv4 `ip:port` serial counts as Wi-Fi (bug B-W4). No test for the `adb-….__adb-tls-connect._tcp` serial form. |
| 1b `adb mdns services` when supported | done | `adb.py:130-139`, `devices.py:130-134` | Only the "unsupported" path has a test: `scripts/fake_adb.py` always answers "not supported". |
| 1c App check with `pm path` | partly | `devices.py:118-127`, `adb.py:141-152` (3 s timeout) | It checks USB devices and the selected phone only. It skips an unselected Wi-Fi device. The dd-ui report records this decision; the brief does not. |
| 2a Page list with "Use this phone" | done | `ui/static/index.html:130-160`, `ui/static/app.js:1806-1837`, `ui/routes/devices.py:70-77` | No page test in the repository. |
| 2b Choice saved as `adb_serial`, over the config, Clear | done | `ui/settings.py:45`, `devices.py:85-115`, `ui/device_panel.py:125-132`; test `test_devices.py:111-125` | No test for "a save of the other page settings keeps the choice" (`monitor.py:575-577`). |
| 2c No automatic pick of several devices | done | `adb.py:110-115`; tests `test_devices.py:103-108`, `test_adb.py:36-39` | With exactly one ready device, the server uses it by itself (safety finding S1). |
| 2d Change device: stop the stream, remove the old forward, `phone_connect` | partly | `ui/device_panel.py:101-120`, `ui/monitor.py:821-839` | Bugs B-W1 and B-W2. The test checks only the step names. |
| 2e "app not installed", no install | done | `devices.py:29`, `devices.py:127`, `app.js:1793` | No test with a USB device without the app. |
| 3 Switch to Wi-Fi | partly | `ui/device_panel.py:134-161`, `adb.py:154-171`; test `test_devices.py:135-145` | Bug B-W1(a): the switch fails at the last step when a phone was connected before. |
| 4a Pair form (`adb pair`, then `adb connect`) | done | `ui/device_panel.py:163-171`, `adb.py:173-178`; test `test_devices.py:168-179` | Bug B-W3: a failed pair writes the code to the log. |
| 4b Plain "Connect IP:port" | done | `ui/device_panel.py:173-177`; tests `test_devices.py:180-182`, `test_discovery.py:298-301` | The address pattern also accepts host names (B-W4). |
| 5 Reconnect a lost Wi-Fi phone once | done | `server.py:764-776`; test `test_devices.py:148-165` | Suspect: no effect when adb keeps the phone as `offline` (B-W5). |
| 6a `phone_devices`, read-only, same data as the page | done | `server.py:823-833`, `ui/device_panel.py:75-77`; test `test_devices.py:185-193` | The tool sends `pm path` to unselected USB devices (S2). |
| 6b Tool text: only the user selects | done | `server.py:826-827`; test `test_devices.py:189` | – |
| 6c No agent tool for pair, connect, or `tcpip` | done | the tool list; `adb_*` names are log rows only (`ui/constants.py:213-217`) | The page device routes have no token (S4). |
| 7 Only `-s <selected serial>`, except list, connect, pair | partly | `adb.py:106-163`, `server.py:782`, `server.py:787`, `phone_screen.py:77-84` | Exceptions S1, S2, S3, S5, S6. |
| 8 AGENTS.md adb rule | done | `AGENTS.md:55` | – |
| 9a fake adb with several devices, USB and Wi-Fi, `tcpip`, `connect`, `pair` | done | `scripts/fake_adb.py:88-236` | `forward --remove` never fails in the state mode. The single-device mode refused `forward --remove` (B-W7, fixed in this round). |
| 9b pytest: list, persistence, no automatic pick, switch, reconnect, read-only | done | `test_devices.py:82-193` | – |
| 9c Playwright test of the page | missing | – | The dd-ui report says that it ran from a scratch folder only. |

#### docs/reports/bench-workflow-improvements.md

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| P0 one transform for the preview and `phone_snapshot` | done | `orientation.py:40-94`, `orientation.py:121-135`, `server.py:525-542` | – |
| P0 turn before the flips and the scale; no double turn | done | `images.py:102-128`, `server.py:629`; test `test_image_transform.py:140-148` | – |
| P0 turn and flips in `SnapshotInfo` | done | `server.py:193-198`, `server.py:641-644` | – |
| P0 same transform for saved images, `multimeter_read(source=phone)`, focus points, overlays, arrows, board photo coordinates | done | `server.py:627`, `server.py:649-661`, `focus.py:185-190`, `highlight.py:165-175`, `pointer.py:72-79`, `evidence.py:37-62` | – |
| P0 `docs/phone-api.md` updated | done | `docs/phone-api.md:77` | The lines about flips (`:79`, `:87`) do not mention the turn (contract gap). |
| P0 acceptance with a synthetic target (Screen view, flips, rotations) | done | `test_image_transform.py:59-79`, `:131-148`, `:229-278`; `scripts/make_orientation_target.py` | The server-level cases use no flip and Flip H only. Flip V is tested in the pure functions only. |
| P0 acceptance on the real phone | missing | – | No report records a real-phone run. The phone is not connected. |
| P1 meter: unknown unit, LCD text apart from the value, mode/unit check | done | `multimeter.py:186-233`, `multimeter.py:259-278`, `multimeter.py:418-434` | – |
| P1 meter: a conflict gives uncertain or disputed and no number | done | `multimeter.py:486-489`, `multimeter.py:524` | The frame combine ignores unit and mode differences (B-E1). |
| P1 meter: request for a frame with the LCD symbols and the dial; expected mode is context | done | `multimeter.py:253-256`, `multimeter.py:490-501`; test `test_multimeter.py:322` | – |
| P1 meter acceptance: Ω, kΩ, MΩ, V, OL; obscured symbols; "443 V" in a resistance test | done | `test_multimeter.py:261-313` | "443 V" is disputed only with `expected_mode` or a user mode, else only by the 30 V limit. |
| P1 meter: the model image through `include_image` and the monitor log | partly | `server.py:1089-1093`, `monitor.py:188-196` | A phone-source read without `include_image` puts no image in the log. The log image has no capture id. The page shows the value without its status (`app.js:712-724`), so a disputed value looks like a measurement. |
| P1 capture id and UTC time on every photo and meter result | done | `evidence.py:107-120`, `server.py:686-698`, `server.py:729-741` | With several frames, the result id is the id of the first frame (`meter_frames.py:152`). |
| P1 a position statement needs a current photo | partly | `evidence.py:189-198`, `instructions.py:80-84`, `board/tools.py:994-995`, `bench_state.py:394-395` | Free text cannot be enforced. The ids never go stale without the scene watcher (B-E7). |
| P1 identity states: visible marking, candidate, confirmed | done | `board/identity.py:48-79`; test `test_board_identity.py:81-181` | – |
| P1 confirmed needs a fresh photo, a marking or landmark, and a valid registration | partly | `board/tools.py:994-1003`, `identity.py:244-308` | `photo_id` is not tied to the photo of the registration (B-E5). A landmark has no visual input (B-E6). |
| P1 look-alike parts stay candidates; estimates never become visual facts | partly | `identity.py:109-122`, `identity.py:309-323` | See B-E6. |
| P1 a scene change or the other board side removes a confirmation | done | `identity.py:166-184`, `identity.py:248-253` | Not for boards with "mixed" side labels (by design). |
| P2 compact record: power, meter mode, contact, measurements, candidates, photo ids, next step | done | `bench_state.py:78-150`, `bench_state.py:333-341` | `probe_contact` does not go stale after a scene change. |
| P2 a low-confidence result cannot enter | done | `bench_state.py:260-264`; test `test_bench_state.py:61` | – |
| P2 a step completes when its evidence enters | partly | `bench_state.py:286-303` | Only with `step_id`. A power-check step completes with an unsafe reading (B-E4). |
| P2 `bench_instructions` shows the current step | done | `instructions.py:112-113`, `bench_state.py:480-486`; test `test_bench_state.py:144` | – |
| P2 record is local and git-ignored | done | `.gitignore:22-23`, `instructions.py:19-21` | A custom `DEBUG_DEVICES_BENCH_STATE_FILE` path is not git-ignored. |
| P2 safety gate: isolation, user confirmation, safe residual voltage | partly | `bench_state.py:179-197`, `bench_state.py:441-455`; tests `test_bench_state.py:74-127` | Bugs B-E3 and B-E8. Bypasses: `done_before_gate`, and a measurement without `step_id`. `multimeter_read` never checks the gate. |
| P2 after a probe short, go back to the power check | done | `bench_state.py:344-351`; test `test_bench_state.py:127` | See B-E4. |
| P2 update of the user's `instructions.md` | not verified | – | The file is git-ignored and belongs to the user. |

#### docs/briefs/p1-p2-evidence.md

The items of this brief are the P1 and P2 items of the list above. Their status is the same.

| Item | Status | Evidence | Missing / note |
|---|---|---|---|
| 1 meter: unknown unit, LCD text, mode/unit check, conflict status, request for LCD and dial, `expected_mode`, tests | done | `multimeter.py:186-233`, `multimeter.py:253-278`, `multimeter.py:456-530`, `server.py:1059`; tests `test_multimeter.py:261-313` | B-E1, B-E2 (frame combine). |
| 1 keep `include_image` and the monitor log image | partly | `server.py:1089-1093`, `monitor.py:188-196` | Phone source not in the log; the log shows no status. |
| 2 capture id (UUID v7) and UTC time; meter id = model image; stale ids refused; evidence rule | done | `evidence.py:96-120`, `evidence.py:171-176`, `server.py:686-741`, `board/tools.py:995`, `instructions.py:80-84` | Gap B-E7. |
| 3 identity state per claim; `board_identify`, `board_identity` | done | `identity.py:48-79`, `board/tools.py:976-1014` | – |
| 3 confirmed needs a fresh photo, a marking or landmark, and a valid registration | partly | `board/tools.py:994-1003`, `identity.py:218-308` | B-E5, B-E6. No test for a photo and registration mismatch. |
| 4 local record, fields, tools, `bench_instructions` step, low-confidence refusal, probe short | done | `bench_state.py:78-150`, `bench_state.py:260-264`, `bench_state.py:344-466`, `instructions.py:194-195` | – |
| 4 safety gate | partly | `bench_state.py:179-197`, `bench_state.py:441-455` | B-E3, B-E4, B-E8. |
| General: do not edit `instructions.md` | done (per the dd-mcp report) | `docs/reports/dd-mcp.md:511` | Not verifiable from git. |

### Summary of the status

- Done: most items of all 8 briefs and of the improvement list.
- Partly done, main gaps:
  - adb-wifi 2d, 3, and 7: device switch and the "only the selected serial" rule (bugs B-W1, S1 to S6).
  - bench-feedback-1 items 3, 5, 6: live frames for a secondary server, tracked pixels, and the view check of `phone_point_to`.
  - bench-feedback-2 items 1, 2, 4, 8: meter limit, user mode, re-registration without a stream, mixed side labels.
  - sync-fix: resend rules.
  - overlay-layout: legend and annotated image.
  - markings-toggle: one tool note, and the agent's view image stays visible.
  - p1-p2-evidence: safety gate and identity rules.
- Missing:
  - Playwright tests in the repository (adb-wifi 9c, p0-orientation 5, markings-toggle).
  - The real-phone acceptance runs (no phone).

### Contract gaps

`docs/phone-api.md`:

- C1: `visible` together with `boxes` or `arrows` returns 400 in the app (`A/Overlay.kt:117-120`) and in the fake. The contract (line 93) does not state it.
- C2: A body with `arrows` and no `boxes` returns 400 (`A/Overlay.kt:122`). The contract (lines 89-90) does not say that `boxes` is required.
- C3: `docs/overlay-layout.md:24-25` gives arrows an optional `tag`. `docs/phone-api.md:90`, the app, and the MCP client have no arrow tag.
- C4: `overlay_boxes` and `overlay_arrows` are "the number on the screen now" (lines 91-92). While the overlay is hidden, the app and the fake give the kept count.
- C5: The resend list (line 95: preview flips, in-sensor zoom, af_mode) is not complete. The MCP also sends the overlay visibility again, and the preview flips after a Screen view change or a phone turn in Auto (`orientation.py:275-278`).
- C6: No document says what happens to boxes outside the phone view. The app removes them before the layout, so colours and tags shift (B-S2).
- C7: "1-3 characters" of a tag is not defined. Kotlin counts UTF-16 units (`A/Overlay.kt:139-142`); Python counts code points (`phone_api.py:214`).
- C8: `app_start_id` is made "once at each app start". The app makes it once per activity start, not per process.
- C9: `preview_region` (line 94) takes "the zoom, the rotation, the preview flips" into account. In the app, zoom and flips do not change it (`A/Overlay.kt:262-266`), because zoom crops the still and the preview alike. It can also be null when the view has no size. Proposal: say that it depends on the rotation and the screen, and when it is null.
- C10: "The box must be inside the image" has a tolerance in the app (`EDGE_TOLERANCE = 1e-4`) and in the client (`phone_api.py:218`). Not stated.
- C11: The MCP server, the page, and the tracker keep boxes after the app removes them (`OVERLAY_TTL`, 10 min). The contract rule has no client side.
- C12: `rotation_degrees` is "the rotation of the next snapshot". The server reads `/v1/status`, then `/v1/snapshot` (`server.py:530-535`). A phone turn between the two gives a wrong turn. The snapshot response does not give its own rotation.
- C13: The contract does not fix EXIF orientation or turned pixels (line 77). The Xiaomi phone turned the pixels (round 3). `optics.output_width_px` "before rotation" depends on this choice.
- C14: Lines 79 and 87 say that the MCP server applies "its own flips". The server also turns the still (line 77).

Other documents:

- C15: `docs/boardview-json.md:62` gives `side` as `top`, `bottom`, or `both`. The MCP sets `both` for through-hole parts and treats labels as unreliable in "mixed" files (`board/model.py:252-290`). Not stated.
- C16: AGENTS.md: "use only the serial that the user selected". `config.py:114`, `.env.example:17`, `mcp/README.md:34`, and `adb.py:107-116` keep an "only device" fallback (S1).
- C17: AGENTS.md and adb-wifi item 7 do not allow `pm path` on unselected devices. The code sends it (S2). `mcp/README.md:35` and `:195` contradict each other.
- C18: `.env.example` does not list `DEBUG_DEVICES_ADB_PATH`, `DEBUG_DEVICES_ADB_TIMEOUT`, or `DEBUG_DEVICES_LOCAL_FORWARD_PORT` (`config.py:115-128`). AGENTS.md says that it lists every variable.

Report claims that the code does not support:

- `docs/reports/dd-ui.md` rounds 27, 30, and 31 describe Playwright tests. None are in the repository.
- `docs/reports/dd-ui.md` round 30: the old-app note says "its own boxes stay visible". The code removes the boxes from the phone (`server.py:173`).
- `docs/reports/dd-ui.md` round 30: `board_locate_in_photo(highlight)` returns the markings note. It does not (B-S7).
- `docs/reports/dd-ui.md:1228`: "The code is not written to the log". A failed pair writes it (B-W3).

### Bugs

Order: safety first, then by effect. "Suspect" means that the review explains it from the code, but it needs a test or a real run.

Safety:

- **S1 (checked, proven): with no selection, an agent tool sends commands to an unselected device.** `adb.py:107-116` returns the only device in state `device`, with no check that it is the phone. `phone_connect` then sends `adb -s <device> forward tcp:<port> tcp:8765` and `shell am start -n dev.jayson.debugdevices.camera/.MainActivity` (`server.py:782`, `:787`).
  - Proof: `scripts/qa_mcp_stdio.py`, group `fire-tv`. With a fake Fire TV `192.0.2.50:5555` in state `device` and the phone `unauthorized`, `phone_connect` sent both commands to the Fire TV.
  - Real case: the phone asks for USB authorization, and one Fire TV is connected.
  - `test_adb.py:29-33` locks this behaviour in. Fix proposal: no automatic pick; `phone_connect` without a selection lists the devices and stops.
- **S2: `phone_devices` sends `pm path` to every USB device in state `device`, selected or not** (`devices.py:124-126`, `adb.py:144-146`). This breaks AGENTS.md (C17).
- **S3: a host-name or IPv6 serial counts as USB** (`devices.py:23`, `:47-48`). After `adb connect firetv.lan:5555` (the page Connect form accepts host names), `phone_devices` sends `pm path` to it, and the page offers "Switch to Wi-Fi" (`tcpip`).
- **S4: the page device routes have no token** (`ui/routes/devices.py:70-77`). `LocalOnly` (`ui/app.py:47-58`) accepts a POST without an `Origin` header, so any local process can select, pair, connect, or switch a device. Only the AGENTS.md rule stops an agent.
- **S5 (suspect): `stop_phone` removes the forward of the last phone of this server** (`ui/monitor.py:836-839`). When the user changed the phone through another server's page, `bench_stop` can remove the shared forward of the new phone.
- **S6 (suspect): the phone screen cleanup uses the new serial for the old port** (`phone_screen.py:271-274`, `ui/monitor.py:541`). The old forward can stay.
- **B-E3 (checked): a later safe voltage reading hides an earlier unsafe residual voltage.** Every confirmed voltage reading after the isolation replaces `power.residual` (`bench_state.py:283-285`). Case: 5.10 V blocks the gate; then 0.01 V at another point opens it, and the open power checks complete (`:300-303`).
- **B-E4: a power-check step completes with any confirmed voltage reading** when the agent passes its `step_id` (`bench_state.py:286-298`). Case: after a probe short, "5.10 V" with the power-check `step_id` completes the step.
- **B-E8 (checked): the residual voltage knows only the prefix "m"** (`bench_state.py:173-176`). "450 µV" counts as 450 V (the gate blocks). "0.4 kV" counts as 0.4 V (safe). "MV" counts as millivolts. `meter_frames.PREFIX_FACTORS` has the correct factors.

Wrong data or blocked work:

- **B-W1: after a stop, a second stop fails** (`ui/monitor.py:836-839` does not clear `bus.phone.serial`). The real adb answers "listener 'tcp:18765' not found", and the step wrapper fails. Effects: "Switch to Wi-Fi" with a connected USB phone fails at the last step; Clear, then "Use this phone", fails; `bench_stop`, then "Use this phone", fails. The fake adb hides it. Related to `docs/briefs/phone-stop-bug.md`.
- **B-W2 (known): when the old device is gone, `forward --remove` fails with "device not found" and blocks the switch.** This is `docs/briefs/phone-stop-bug.md`. The working tree has part of a fix in progress.
- **B-W3: the pairing code goes into the activity log.** `adb.py:190` puts the full command into the error text; `ui/device_panel.py:87-96` copies it into the log.
- **B-E1 (checked): the meter frame combine ignores unit and mode differences.** `multimeter.signature` holds only the digits and the point (`multimeter.py:291-296`); the result takes the value of the first frame (`meter_frames.py:163-190`). Case: "4.98 V" and "4.98 mV" give a confirmed 4.98 V.
- **B-E2 (checked): the frame combine ignores the sign.** "-0.12 V" and "0.12 V" give a confirmed -0.12 V.
- **B-F1 (checked): `phone_point_to` has no camera-view check.** `pointing.py:417-424` calls only `scene.guard()`, not `guard_snapshot_registration`. Case: register at 1x without a stream, zoom to 2x, new photo, `phone_point_to`: the boxes go to the wrong place with no error. The board tools refuse this case.
- **B-F2: a newer photo of another size is not scaled.** `CameraView` has no image size (`evidence.py:37-62`). Case: register a `max_side=0` photo, take a default photo, call `board_parts_at_photo` with its pixels: wrong board point, no error.
- **B-F3 (checked): recording a measurement erases the user's meter mode.** `bench_state.py:281` writes the meter mode with the source "reading", so `recent_user_mode` returns None.
- **B-F4: the plausibility limit misses frames that are not confirmed** (`meter_frames.py:106-141`). "93.2 V" at confidence 0.6 gives "uncertain", not "disputed". The limit text can also appear once per frame.
- **B-E5: `board_identify` does not tie `photo_id` to the photo of the registration** (`board/tools.py:994-1006`). Case: photo A at 1x, zoom 2x, photo B, register on B, identify with A and pixels of A: the result can be "confirmed" at a wrong point.
- **B-E6 (suspect, design): a "landmark" confirmation has no visual input** (`identity.py:125-141`, `:299-308`). Only the agent's pixel and the boardview data decide.
- **B-E7: capture ids and registrations never go stale without the scene watcher** (`evidence.py:130-144`). With `--no-ui`, `--no-phone-screen`, or a failed stream, a moved board keeps "current scene".
- **B-S5 (checked): a read-only call sends the preview flips.** `phone_snapshot_orientation()` with no argument calls `orientation.update(None, None)` and `preview_sync.push()` (`server.py:924-927`). A read can undo the flips of another client.
- **B-S2: the Android app shifts box colours and tags.** `A/CameraController.kt:563-565` drops boxes outside the preview before the layout. Case: two boxes, the first outside the view: the phone shows the second as "A" in green, the page and the annotated image show "B" in cyan.
- **B-S1: the Android legend is always top left, at 40 %** (`A/OverlayView.kt:159`, `:205`). The spec says the first corner of the sorted list, at 50 % (`docs/overlay-layout.md:31`).
- **B-S3: the annotated image has no dark outline on the outer side** (`overlay_draw.py:46-48`: PIL draws the outline inward). Rule 5 asks for 2 px on each side.
- **B-S4: a small box gets no outline in the inset** (`overlay_draw.py:90-99`), and the inset draws no tags (also `app.js:1557-1567`).
- **B-S7: `board_locate_in_photo(highlight)` gives no "markings are hidden" note** (`server.py:418-431`). Only `phone_point_to` adds it (`server.py:1036`).
- **B-F5: a carried registration keeps its old stale reason** (`pointing.py:271-283`). After an app restart and a later tracking loss, the message says "from before the phone app restarted".
- **B-F6: a secondary server gets no frames for a long time after a primary restart** (`ui/remote_screen.py:78-96`). It keeps its frame counter; the new primary counts from 0 (`ui/routes/ingest.py:104` returns 204).
- **B-F7: after a server restart, a restored registration has a wrong stale message** (`board/tools.py:360-364`): "the board or the phone moved".
- **B-F8 (low): a bad crop area loses the still** (`server.py:674-678`) after the view state has already changed.
- **B-W7 (fixed in this round): the fake adb refused `forward --remove`** in its single-device mode.

Suspect, lower effect:

- **B-S6: a failed resend after an app restart is never retried** (`app_start.py:35-44`). `AfModeSync` also stops on any phone error (`camera_choice.py:209-211`).
- **B-S8: a page settings save can write an old `markings_visible` value** (`ui/monitor.py:578`).
- **B-S9: boxes flicker on the page during a wheel zoom** (`app.js:1422-1442`).
- **B-S10: a tag of two emojis passes the MCP check and fails in the app** (C7). The server then sends the boxes without tags.
- **B-S11: mirrored boxes after a flip change when the raw still is missing** (`ui/monitor.py:369-370`).
- **B-F9: overlay changes that a secondary sends to the primary can arrive out of order** (`ui/forward.py:231-235`).
- **B-F10: saved webcam controls can fail to apply** (`webcam_controls.py:66-79`, `:170-181`) when auto exposure and a fixed exposure time go in one call.
- **B-F11 (Android): `preview_region` takes the preview aspect from the ImageCapture resolution** (`A/CameraController.kt:586-590`).
- **B-W4: host-name and IPv6 Wi-Fi serials get no reconnect** (`server.py:770`), see S3.
- **B-W5: a Wi-Fi serial that adb keeps as `offline` is not reconnected** ("already connected" counts as success).
- **B-W8: a failed selection save still shows "select: ok"** (`devices.py:102-106`, `ui/device_panel.py:110-111`).
- **B-W9: the settings writers have no lock across processes** (`devices.py:103-104`, `camera_choice.py:63-64`, `orientation.py:204-205`).
- **B-E9: `bench_measure` photos do not update the page snapshot** (`ui/monitor.py:425-433`), so a later `phone_highlight` is drawn on an older picture.
- **B-E10: preview flips can use a stale rotation** (`orientation.py:227-254`).
- **Docs: `board/tools.py:841`** still says that the registered photo must be the last `phone_snapshot`.

### What to do next

1. Fix S1 first: no automatic device pick. The `fire-tv` group of `scripts/qa_mcp_stdio.py` shows the fix (it passes when `phone_connect` refuses and sends nothing).
2. Fix the safety gate bugs B-E3, B-E4, and B-E8.
3. Fix B-W1 together with `docs/briefs/phone-stop-bug.md`, then B-W3.
4. Decide the contract gaps C1 to C14 in `docs/phone-api.md`.
5. When the phone is connected: `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict --after-start` on the selected serial, and the real-phone steps in `docs/reports/dd-ui.md:1193-1204`.

## Round 5: QA tools for the contract gaps C1-C18

Task: `docs/briefs/qa-round4-fixes.md`, last section (dd-qa part). I read the new `docs/phone-api.md` first. I changed only `scripts/` and `docs/qa.md`. I did not commit.

### What changed

- `scripts/fake_phone.py`:
  - The fake turns the pixels of the still, like the app now must. EXIF `Orientation` is absent. The built-in test image has four embedded turned copies (made once with ffmpeg), so the fake still needs only the standard library. A `--snapshot` file turns only when Pillow is installed; the fake prints a note otherwise.
  - `/v1/snapshot` sends `X-Rotation-Degrees` (the rotation that it used for this still) and `X-App-Start-Id`.
  - Tags of boxes and arrows must match `^[A-Za-z0-9]{1,3}$`. Arrows can have a `tag`.
  - The edge tolerance is 0.0001 at the right and bottom edges, as in the app (`A/Overlay.kt:141-143`).
  - `visible` together with `boxes` or `arrows`, and a body without `boxes`, already gave 400. No change.
- `scripts/qa_contract.py`:
  - `snapshot`: EXIF `Orientation` 1 or absent; both headers present; the rotation header equals the status; the start id header equals the status.
  - `snapshot_rotation`: compares pixel sizes (not the EXIF-turned size); `X-Rotation-Degrees` equals the locked rotation.
  - `overlay`: a box at the edge tolerance passes; a box beyond it gives 400; tags `A-B`, `A B`, `É`, two emojis, and a number give 400; tags `U1` and `9Z9` pass.
  - `overlay_arrows`: an arrow tag `A1` passes; arrows without `boxes` and bad arrow tags give 400.
  - `overlay_visible`: `visible` together with `boxes` or `arrows` gives 400. The kept counts while hidden were already checked (C4).
  - `--strict` `preview_region_rotation`: a zoom to max and both preview flips do not change `preview_region`.
- `docs/qa.md`: the check table has the new rules and the checks of the newer endpoints.

### Results

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED in 11 modes (default 29/29) |
| The committed fake phone (before this round), then `python3 scripts/qa_contract.py --strict` | 23/28. The new checks fail as they must: EXIF 6 in `snapshot` and `snapshot_rotation`, the edge-tolerance box and the arrow tag in `overlay` and `overlay_arrows`, and `preview_region_rotation`. |
| `uv run python scripts/qa_mcp_stdio.py --skip-webcam --scratch <scratch dir>` (`--no-ui`) | 15/15. The `fire-tv` case passes now: `phone_connect` answers "no phone is selected" and sends no command to the Fire TV. S1 is fixed. |
| `uv run ruff check` and `uv run ruff format --check` on my three scripts | Clean |

### Notes

- The contract says "inside the image allows an edge tolerance of 0.0001". The app applies it only at the right and bottom edges (`x >= 0`, `y >= 0` stay strict). My checks test only the right edge. Proposal: say "at the right and bottom edges" in `docs/phone-api.md`.
- The app does not have the tag regex, the arrow tag, or the snapshot headers yet (dd-android task). When the phone is connected, `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict --after-start` will show these rules on the real app.
- During this round, another agent edited `scripts/fake_adb.py` (`forward --list`). For a short time it did not pass ruff. At the end of the round it passes. I did not touch it.

### Round 5, part 2: the exact edge rule

The contract now says: `snapshot_x >= 0` and `snapshot_y >= 0` (strict), `snapshot_x + width <= 1.0001` and `snapshot_y + height <= 1.0001`.

- `scripts/fake_phone.py:558` follows this text exactly. No logic change; the comment now quotes the rule.
- `scripts/qa_contract.py` `overlay` has new cases: a box inside the tolerance at the bottom edge passes; a box beyond it at the bottom edge gives 400; `snapshot_x` or `snapshot_y` of -0.00005 gives 400 (0 is strict, also inside the tolerance range).
- `python3 scripts/fake_phone.py --self-check`: PASSED (default 29/29). ruff is clean on both files.

### Round 5, part 3: `overlay_region`

- `scripts/fake_phone.py`: new status field `overlay_region`. The fake has a safe area in the natural portrait frame of the preview: 0.04 at the top (status bar and label) and 0.06 at the bottom (navigation bar). It maps this area through the preview flips and the rotation into `preview_region`. At rotation 0 with no flip, it gives the contract example `{0.2, 0.04, 0.6, 0.9}`. A vertical flip gives `{0.2, 0.06, 0.6, 0.9}`. It is `null` when `preview_region` is `null`.
- `scripts/qa_contract.py`:
  - Every status: `overlay_region` is a key, has the region format, and has the same null rule as `preview_region`.
  - `--strict` `overlay_region`: inside `preview_region` (2 % tolerance) with no flip and with a vertical flip; a vertical flip moves it by more than 0.001. The check puts the flips back at the end.
- Results: `python3 scripts/fake_phone.py --self-check` PASSED (default 30/30, "lying at 270" gives `{0.06, 0.2, 0.9, 0.6}`). `uv run python scripts/qa_mcp_stdio.py --skip-webcam`: 15/15. ruff is clean.
- Note: an app without `overlay_region` now fails the status key check in every check. This is correct for the new contract. The real app gets the field from dd-android.

## Round 6: final run

Date: 2026-09-28. The working tree at `e7f0da9` with the uncommitted fixes of all agents (QA round 4). All other agents had stopped editing. I did not change product code, and I did not commit.

### 1. Tests and hooks

| Command | Result |
|---|---|
| `uv run pytest -q -p no:cacheprovider` | 859 passed, 1 skipped |
| `nix develop --command boardview/tests/run.sh` | 13 passed, 1 skipped |
| `nix develop --command prek run --all-files` (on a copy of the working tree, with the new files added in the copy only, because the prek fixers change files) | All hooks pass except `end-of-file-fixer`: `docs/reports/dd-qa.md` (my file) ended with an empty line. Fixed in this round. |
| `(cd android && nix develop .. --command ./gradlew --no-daemon assembleDebug testDebugUnitTest)` | BUILD SUCCESSFUL. Gradle reused the results (`UP-TO-DATE`), so I also ran `testDebugUnitTest --rerun`: BUILD SUCCESSFUL, 160 tests in 14 suites, 0 failures. |

### 2. QA scripts

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED in 11 modes (default 30/30) |
| `python3 scripts/fake_phone.py --port 18897`, then `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18897 --strict --after-start` | 30/30 |
| `uv run python scripts/qa_mcp_stdio.py --skip-webcam --scratch <dir>` (server with `--no-ui`) | 15/15, with `fire-tv`: `phone_connect` answers "no phone is selected" and sends no command to the fake Fire TV |
| The same with `--real-adb-several-devices` (the real adb 37.0.0, the same version as the running adb server; no selection) | 16/16. Only `adb devices -l` ran. The adb forwards did not change. |

Two QA tool fixes in this round (my files only):

- `scripts/qa_contract.py`: `FOCUS_STATES` was defined two times. The second definition (without `unknown`) replaced the first one, so every status with `focus.state` `unknown` failed. The contract allows `unknown`. The second list is now `FOCUS_STATES_AFTER_FOCUS` (used only right after `POST /v1/focus`). The first real-phone run failed 3 checks for this reason alone (see 4).
- `scripts/qa_mcp_stdio.py`: the opt-in group `--real-adb-several-devices` expected the old text "several adb devices". The server now answers "no phone is selected" (S1 fix). The group now expects this text.

### 3. Re-check of the round 4 findings

Three read-only agents re-checked every finding against the current code. I checked S1 end to end (`fire-tv` and real-adb groups above). All fixes are in uncommitted files; the new tests are untracked files. Paths are in `mcp/debug_devices_mcp/` unless they start with another directory.

#### Devices and safety (S1-S6, B-W*, C16-C18)

`uv run pytest -q mcp/tests/test_adb.py mcp/tests/test_devices.py mcp/tests/test_device_safety.py mcp/tests/test_phone_stop.py mcp/tests/test_env_example.py`: 55 passed.

| Id | Status | Evidence | Remaining |
|---|---|---|---|
| S1 | fixed | `adb.py:44-47`, `adb.py:153-158` (no selection: only `devices -l`), `devices.py:123-130`, `server.py:847-858`; tests `test_adb.py:29-40`, `test_devices.py:109-115`; `scripts/qa_mcp_stdio.py` `fire-tv` and `real-adb` pass | – |
| S2 | fixed | `devices.py:133-143` (`pm path` only for the selected serial); test `test_devices.py:82-106` | – |
| S3 | fixed | `devices.py:23-27`, `devices.py:53-55` (any colon or mDNS name is Wi-Fi), `ui/device_panel.py:170-172`; tests `test_device_safety.py:38-65` | Display only: the "gone" row uses an IPv4-only pattern (`ui/static/app.js:1890`), so a gone host-name, IPv6, or mDNS serial shows "usb". |
| S4 | fixed (same-origin check) | `ui/routes/devices.py:19-28`, `:38-93`; tests `test_device_safety.py:82-106` | `Origin` is not a secret: a local process can send `Origin: http://127.0.0.1:18766`. This is a check, not authentication. |
| S5 | fixed | `server.py:244`, `server.py:556-564`, `adb.py:185-195` (owner check with `forward --list`), `ui/monitor.py:898-907`; test `test_phone_stop.py:162-171` | Edge case: two servers forward the same serial on the same port; a `bench_stop` on one removes the forward of the other. |
| S6 | fixed | `phone_screen.py:270-278`, `:319`; test `test_device_safety.py:114-145` | – |
| B-W1 | fixed | `ui/monitor.py:898-899`, `server.py:559`, `adb.py:177-183`, `ui/device_panel.py:109-115`; tests `test_phone_stop.py:148-159`, `test_device_safety.py:153-156` | No direct test for "Switch to Wi-Fi with a connected USB phone". |
| B-W2 | fixed | `adb.py:39`, `adb.py:280-283`, `ui/monitor.py:903-907`, Disconnect `ui/device_panel.py:150-164`, gone state `devices.py:171`, `:197-198`; tests `test_phone_stop.py:91-201` | For "device offline", adb can keep the forward listed; the note says that it is cleared. Harmless. |
| B-W3 | fixed | `adb.py:131-135`, `:261-284` (the code is removed from the error), `ui/device_panel.py:201-202`; tests `test_device_safety.py:159-186` | – |
| B-W4 | fixed | `devices.py:53-55`, `server.py:856-870` | No host-name or IPv6 reconnect test. |
| B-W5 | fixed | `server.py:861-865` (`disconnect`, then `connect`), `adb.py:161-163`, `:256-259`; test `test_device_safety.py:195-206` | Suspect, real adb only: `select_device` runs at once after `connect` (`server.py:870`); a phone that is `offline` for a moment fails. |
| B-W7 | fixed | `scripts/fake_adb.py:271-284`; state mode `:183-206` fails like the real adb | – |
| B-W8 | fixed | `devices.py:113-121`, `ui/device_panel.py:117-138`; tests `test_device_safety.py:209-228`, `test_devices.py:118-133` | – |
| B-W9 | fixed | `ui/settings.py:125-150` (`flock`, 2 s limit), all writers use it; tests `test_device_safety.py:231-259` | New N3 below. |
| C16 | partly | `.env.example:31-33`, `config.py:120-123`, `mcp/README.md:34`, `:263` | `mcp/README.md:17` still says "If more than one ADB device is connected, set `DEBUG_DEVICES_ADB_SERIAL`" (it implies the old single-device pick). `mcp/README.md:198` is weaker than the rule. |
| C17 | fixed | `mcp/README.md:35`, `:198`, `devices.py:1-7`, `:133-143` | – |
| C18 | fixed | `.env.example:36-40`; test `test_env_example.py:22-40` | `DEBUG_DEVICES_DEV_RELOAD` (`scripts/mcp-server.sh:45`) is a script variable, not a Settings variable. |

adb audit: every `forward`, `shell`, `tcpip`, and `push` uses `-s` with the selected serial (or the serial that `phone_connect` connected). By design, `forward --remove` of this server's own forward goes to the previous phone after a switch.

New findings (devices):

- N1 (low, fixed in this round): `scripts/qa_mcp_stdio.py` expected "several adb devices" (see 2).
- N2 (low, hardening): `ui/routes/ingest.py:83-92` and `ui/monitor.py:516-522` accept any `serial` from a secondary server. The primary then runs `adb -s <serial> push/forward/shell` on it. Only the ingest token protects this route; there is no check against the page selection.
- N3 (low): `ui/settings.py:139-146` waits for the file lock with `time.sleep` on the event loop: the server can stop for up to 2 s.
- N4 (low): `server.py:559` clears `forwarded_serial` before the adb calls. When `forward --remove` fails for a temporary reason, the forward stays and this server forgets it.

#### Evidence, meter, bench, and tracking (B-E*, B-F*)

`uv run pytest -q` on the 10 test files of this area: 165 passed.

| Id | Status | Evidence | Remaining |
|---|---|---|---|
| B-E1 | fixed | `meter_frames.py:169-173`, `:185-198`, `:226-237`; tests `test_meter_frames.py:196-251` | – |
| B-E2 | partly (checked) | `meter_frames.py:176-182` (`sign_key`); tests `test_meter_frames.py:200`, `:223-230` | `sign_key` reads only the first character, but `signed_value` (`multimeter.py:299-304`) finds a minus anywhere before the first digit. "DC -5.10" and "DC 5.10" give a confirmed -5.1 V. Fix: use the `signed_value` rule in `sign_key`. |
| B-E3 | fixed as the brief asks | `bench_state.py:200-218` (residual per point `label`), `:237-240`; tests `test_bench_state.py:337`, `:358` | The point is the free-text `label`. The same label at two points ("residual") lets a later safe reading replace an unsafe one, and the gate opens. After a new isolation confirmation (`:477`), a safe reading at any point opens the gate. A confirmation does not discharge a capacitor. |
| B-E4 | fixed | `bench_state.py:299-309`, `:364-376`; test `test_bench_state.py:374` | `complete_step` with `step_reason` can still close a power-check step as `user_report` (`:392-395`). It does not open the gate. |
| B-E5 | fixed | `board/tools.py:1001-1016`, `:1075-1077`, `evidence.py:204-229`; tests `test_evidence_round4.py:124`, `:147` | Low: a registration with no `photo_id` (registered before any `phone_snapshot` of the session) skips the check (`board/tools.py:761-764`). |
| B-E6 | fixed as the brief asks | `board/identity.py:322-343`, `board/tools.py:1044-1085`; test `test_board_identity.py:141` | The agent sets `user_confirmed`; the server cannot check it. |
| B-E7 | fixed | `scene.py:163-196`, `server.py:959`, `:982`, `:1031`, `:1073`, expiry task `server.py:1318`; tests `test_evidence_round4.py:184-243` | Low: without a watcher, a Screen view change from the page, flips from another server, and an app restart (capture ids stay valid, `server.py:323-335`) do not make photos stale before the 5 min expiry. |
| B-E8 | fixed | `bench_state.py:180-191` (`PREFIX_FACTORS`); tests `test_bench_state.py:409`, `:425` | – |
| B-E9 | fixed | `ui/monitor.py:73-74`, `:112-124`, `:471-481`; test `test_overlay_sync_round4.py:237` | – |
| B-E10 | fixed | `orientation.py:248-259`; test `test_overlay_sync_round4.py:112` | – |
| B-F1 | fixed | `pointing.py:426-437`; test `test_evidence_round4.py:60` | – |
| B-F2 | fixed for the QA case | `evidence.py:52-61`, `:90-100`, `:175-181`, `board/tools.py:754-771`, `:1036-1041`; tests `test_evidence_round4.py:79`, `:114` | Low: `board_register_photo` does not check that the declared `photo_width_px` matches the photo of the capture log (`board/tools.py:850-869`). A registration with pixels of the full-size `save_path` file, then pixels of the returned smaller image, gives scale 1: a wrong board point with no error. |
| B-F3 | fixed | `bench_state.py:345-347`, `:545-546`; tests `test_bench_state.py:441`, `:458` | – |
| B-F4 | fixed | `meter_frames.py:113-124`, `:142-152`, `:217-220`; tests `test_meter_decimal.py:364`, `:411-434` | – |
| B-F5 | fixed | `board/tools.py:209-212`, `pointing.py:286`, `:347`, `server.py:486-501`, `evidence.py:145-155`; tests `test_evidence_round4.py:267`, `:291` | – |
| B-F6 | fixed | `ui/routes/ingest.py:96-109`, `ui/remote_screen.py:86-88`; test `test_overlay_sync_round4.py:255` | – |
| B-F7 | fixed | `board/tools.py:269-272`, `:380`; test `test_evidence_round4.py:299` | – |
| B-F8 | fixed | `snapshot_crop.py:67-74`, `server.py:738-757` (the photo stays, with `crop_error`); test `test_evidence_round4.py:317` | – |
| B-F9 | fixed | `ui/forward.py:235-249`, `ui/monitor.py:504-512`; test `test_overlay_sync_round4.py:269` | – |
| B-F10 | fixed (apply) | `webcam_controls.py:66-77`, `:168-174`; tests `test_webcam_controls.py:57`, `:68` | New N7 below. |
| B-F11 | fixed | `A/CameraController.kt:130-131`, `:576-605`, `A/Overlay.kt:202-221`, `:317-336`; tests `AT/OverlayLogicTest.kt:218-260` (pass in the Gradle run) | – |
| Docs `board/tools.py:841` | partly | The docstring is correct (`board/tools.py:885-887`) | `mcp/README.md:110` still says "the registered photo must be the last `phone_snapshot`". |

New findings (evidence and bench):

- **N5 (safety, checked, also in `e7f0da9`): an AC voltage reading counts as the residual-voltage check.** `VOLTAGE_MODES` has `AC_VOLTAGE` (`bench_state.py:42`), and the residual check and the power-check step use it (`:69-70`, `:350`). Case: a capacitor holds 5 V DC, the meter is on AC V and reads 0.01 V; recorded with the power-check `step_id`, it opens the gate and completes the power check. Fix: only DC voltage for the residual check.
- **N6 (safety): a refused `bench_record_measurement` loses an unsafe residual reading.** `record_measurement` adds the residual (`bench_state.py:351-352`), then raises for an unknown `step_id` or a mode that does not fit the step (`:354-357`). The tool saves only after a success (`:512-514`). Case: the gate is open after "C12 0.01 V"; the agent records "VBUS 5.10 V" with a resistance `step_id`: error, the 5.10 V is not stored, and `bench_begin_step` still allows the resistance step.
- **N7 (low): saved webcam controls can disagree with the camera.** `webcam_controls.py:75`, `:189-194`. Case: `auto_exposure=true` is saved, then `webcam_controls(exposure=900)`: the camera is at manual 900, the file keeps `auto_exposure: true, exposure: 900`. After a restart, only `auto_exposure=3` goes to the camera, and the fixed exposure for the dim LCD is lost without a warning.

#### Overlay, sync, and contract gaps (B-S*, C1-C15)

`uv run pytest -q` on the 19 test files of this area: 315 passed. The Android tests pass in the Gradle run (1.).

| Id | Status | Evidence | Remaining |
|---|---|---|---|
| B-S1 | fixed | `A/OverlayLayout.kt:56-62`, `:207-225`, `A/OverlayView.kt:170-189`, `:216` (alpha 0x80); test `AT/OverlayLayoutTest.kt:175` | The 50 % drawing has no unit test (View code). |
| B-S2 | fixed | `A/CameraController.kt:567` keeps every box, `A/OverlayLayout.kt:21-25`, `:138-149`, `A/OverlayView.kt:67-81`; test `AT/OverlayLayoutTest.kt:157` | Contract text: see C6. |
| B-S3 | fixed | `overlay_draw.py:25-29`, `:48-50`; test `test_overlay_sync_round4.py:58` | – |
| B-S4 | fixed | `overlay_draw.py:88-111`, page `ui/static/app.js:1559-1604`; test `test_overlay_sync_round4.py:70` | No page test in the repository. |
| B-S5 | fixed | `server.py:1021-1023` (no argument: read only, no save, no push); test `test_overlay_sync_round4.py:97` | – |
| B-S6 | fixed | `app_start.py:21-45` (up to 3 tries), `orientation.py:292-295`, `camera_choice.py:108`, `:209-216`, `:297`; tests `test_overlay_sync_round4.py:134`, `:161` | – |
| B-S7 | fixed | `server.py:460-466`; test `test_overlay_sync_round4.py:180` | – |
| B-S8 | fixed | `ui/monitor.py:618-639` | No test for `markings_visible` (the same rule for flips and in-sensor zoom has tests). |
| B-S9 | fixed | `ui/static/app.js:1426-1451` | No test in the repository. |
| B-S10 | fixed | `constants.py:84` (regex), `phone_api.py:231`, `:249`, `highlight.py:43`; tests `test_overlay_sync_round4.py:196`, `:203` | – |
| B-S11 | fixed | `ui/monitor.py:392-424`; test `test_overlay_sync_round4.py:221` | – |
| C1 | done | `docs/phone-api.md:99`, `A/Overlay.kt:126-128`, `phone_api.py:252-261`; test `AT/ApiServerTest.kt:655`; fake and `qa_contract.py` (`visible with boxes`) | – |
| C2 | done | `docs/phone-api.md:94`, `A/Overlay.kt:130`, `phone_api.py:253`; test `AT/ApiServerTest.kt:655`; `qa_contract.py` (`arrows without boxes`) | – |
| C3 | done | `docs/phone-api.md:94`, `docs/overlay-layout.md:25-26`, `A/Overlay.kt:26-33`, `:150-154`, `phone_api.py:249`, `pointer.py:156`, `app.js:1421`; tests `AT/ApiServerTest.kt:607`, `test_contract_round4.py:97`, `:113` | – |
| C4 | done | `docs/phone-api.md:97-98`, `A/CameraController.kt:810-811`; test `AT/ApiServerTest.kt:655` | Old comments in the client: `phone_api.py:137-140` ("on the phone screen"), `highlight.py:69` ("shows now"). |
| C5 | done | `docs/phone-api.md:102`, `orientation.py` (`PreviewSync.ensure`), `camera_choice.py` (`MarkingsSync`) | – |
| C6 | partly (text) | App: see B-S2 | `docs/phone-api.md:95` says that the app draws "only the boxes that are inside `preview_region`"; `:101` and `docs/overlay-layout.md:24` say the safe area (`overlay_region`). The app draws a whole box when a part of it is in the safe area (`A/OverlayLayout.kt:464`, `A/OverlayView.kt:70`). The text must say one rule. |
| C7 | done | `docs/phone-api.md:94`, `A/Constants.kt:92`, `A/Overlay.kt:157-162`, `constants.py:84`; tests `AT/ApiServerTest.kt:607`, `:677`; real phone 29/29 | – |
| C8 | done | `docs/phone-api.md:102`, `A/CameraController.kt:79-80`, `A/MainActivity.kt:86`; test `AT/AppStartTest.kt:31` | – |
| C9 | partly (text) | `A/Overlay.kt:317-335`; tests `AT/OverlayLogicTest.kt:208`, `AT/ApiServerTest.kt:405`; real phone: zoom and flips keep it | `docs/phone-api.md:100` says both "It takes the zoom, … the preview flips … into account" and "Zoom and the preview flips do not change it". The null sentence is there two times. |
| C10 | done | `docs/phone-api.md:94`, `A/Overlay.kt:142-145`, `A/Constants.kt:98`; test `AT/ApiServerTest.kt:627`; fake and `qa_contract.py` | The client is stricter (`phone_api.py:200`, 1e-9). Correct: the app accepts every box that the client sends. |
| C11 | partly | `docs/phone-api.md:96`, `server.py:388-407` (TTL), `server.py:324-339` (app restart); tests `test_contract_round4.py:128`, `:153` | (a) The primary page never forgets the boxes of a secondary server (`ui/monitor.py:504-512` has no TTL; `server.py:334` clears only this server's boxes). Case: a secondary draws boxes and exits; the primary page shows them after 10 min and after an app restart. (b) Old app with the markings hidden: `server.py:367` sends `[]`, which stops the TTL (`:388-393`), but `self.highlights` keeps the boxes with no expiry. |
| C12 | done | `docs/phone-api.md:80`, `A/ApiServer.kt:103-108`, `A/CameraController.kt:750-783`, `phone_api.py:424-432`, `server.py:587-605`; tests `AT/ApiServerTest.kt:599`, `test_contract_round4.py:62-81`; real phone: headers correct | – |
| C13 | done (app); text and client differ | `docs/phone-api.md:79`, `A/JpegTurner.kt:19-35`, `A/ExifTurn.kt`, `A/CameraController.kt:781-782`; test `AT/ExifTurnLogicTest.kt:18-40`; real phone: pixels turned | `JpegTurner` has no test. `docs/phone-api.md:78` still says "the JPEG can carry this as its EXIF orientation" (against `:79`). The client still applies EXIF (`images.py:35`, `:113-115`); no effect with the new app. |
| C14 | done | `docs/phone-api.md:82`, `:90` | – |
| C15 | partly | `docs/boardview-json.md:62`, `board/model.py:264-266` (`side_ok`, used in `identity.py` and `board/tools.py`) | `pointer.py:168` and `board/marking.py:83` still use the side label alone. Case: in a "mixed" file, a part labelled bottom that is on top: `phone_point_to` says "on the other side" and draws no box; `board_match_marking(side="top")` does not list it. |

New findings (overlay and contract):

- **N8 (checked): `overlay_region` can be `null` while `preview_region` is not.** `A/Overlay.kt:366` returns `null` when the safe area has no size; test `AT/OverlayLogicTest.kt:288` locks this in. This breaks "Same `null` rule" (`docs/phone-api.md:101`). The client then uses `preview_region` (`phone_api.py:153-155`), so `phone_highlight` can say "fully visible" for a box that the app does not draw. Case: the label band covers the safe area in a small or split window.
- **N9 (low): `A/JpegTurner.kt:23` sends the JPEG unchanged, with its EXIF turn, when the decode fails.** This breaks C13 with no error. The re-encode also drops the other EXIF tags. The dd-android report gives a snapshot time of about 2.3 s on the S22 (before: about 0.9 s).
- **N10 (suspect, low): `overlay_region` right after a rotation change can use the label band of the old orientation** (`A/MainActivity.kt:107-113`, `A/CameraController.kt:616-625`). The server keeps that value (`server.py:981`); with `--no-ui`, no status poll corrects it.
- Still open (no id): the page clamps an outside legend into the view, so it can cover the picture (`ui/static/app.js:1541-1546`).

### 4. Real phone

- Selected serial (`~/.local/state/debug-devices/ui-settings.json`, `adb_serial`): a Wi-Fi serial of the Samsung S22 (`SM_S901B`). `adb devices -l` listed only this device, in state `device`.
- I used the adb binary of the running adb server (`android-tools-37.0.0` from the nix store), so the server did not restart.
- Separate forward: `adb -s <selected serial> forward tcp:18790 tcp:8765`. Health: 200, `app_version` 0.1.0. Status: 200 (camera ready, `app_start_id` UUID v7, `in_sensor_zoom` `unsupported`, zoom range 0.6-10).
- First run, `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18790 --strict`: 26/29. The 3 failures (`snapshot_keeps_torch`, `snapshot_rotation`, `preview_region_rotation`) were the QA tool fault above: the S22 reports `focus.state` `unknown` after a still.
- Second run, after the fix: 29/29. Results on the S22:
  - The snapshot is 3060x4080 at rotation 0 and 4080x3060 at 90 and 270: the app turns the pixels. EXIF `Orientation` is absent, or 1 at rotation 90. Both are allowed.
  - `X-Rotation-Degrees` equals the locked rotation; `X-App-Start-Id` equals the status.
  - `preview_region` at rotation 0 is `{0.192, 0, 0.615, 1}`; at 90 it is the same turned. Zoom and flips do not change it.
  - `overlay_region` is `{0.192, 0.109, 0.615, 0.829}`. A vertical flip moves it to y 0.062. It is inside `preview_region`.
  - The in-sensor zoom request gives `unsupported`, and `af_mode` `macro` gives `continuous` (no macro mode on this phone). Both are allowed.
  - Tags, arrow tags, `visible` with boxes, the edge tolerance, the counts while hidden, the start zoom rule (not tested: no app start), concurrency, and the 405, 404, and content-type rules pass.
- State after the runs: the first run left `in_sensor_zoom` `off`, because the checks end with `false`. The saved setting is `true`, so I sent `{"in_sensor_zoom": true}` again. Then the status was equal to the first status in every field except `focus`.
- I removed the forward (`forward --remove tcp:18790`). The MCP server forward (18765) and the scrcpy forward did not change. No command went to another device.
- Not tested on the phone: `--after-start` and `--expect starting-race` (they need an app restart during a live session).

### Summary

- Tests: all pass (pytest 859, boardview 13, Android 160, fake self-check, contract 30/30 on the fake and 29/29 on the S22, MCP stdio 16/16). prek: one failure in my report file, fixed.
- Round 4 findings: the 6 safety findings (S1-S6) are fixed. Of the 40 other bug ids, 39 are fixed and 1 is partly fixed (B-E2). Some fixed ids have small open points in the "Remaining" column. Contract gaps: 12 of 18 are done; C6, C9, C11, C13, C15, and C16 are partly done (mostly text). The docs item of `board/tools.py:841` is partly done (`mcp/README.md:110`).
- New findings: 2 safety (N5 AC voltage as residual check, N6 lost unsafe reading), 1 contract break (N8 `overlay_region` null rule), and 7 low (N2-N4, N7, N9, N10, and the page legend).

### What to do next

1. Safety: N5 (only DC voltage for the residual check), N6 (store the reading before a step error, or check the step first), and B-E2 (use the `signed_value` rule in `sign_key`). Also consider B-E3: one residual per point label lets a later safe reading at the same label replace an unsafe one.
2. Contract: N8 (return the `preview_region` null rule, or change the contract), and the text of C6 and C9 in `docs/phone-api.md`, `docs/phone-api.md:78` (C13), `mcp/README.md:17` (C16), and `mcp/README.md:110`.
3. C11 (a) and (b): forget the boxes of a secondary server and of an old app after the TTL.
4. On the real phone: `--after-start` and `--expect starting-race` after an app restart, when the user allows a restart.

## Round 7: contract text after the final run

The contract changed after `fe0ecdb`. I changed only `scripts/fake_phone.py`, `scripts/qa_contract.py`, and `docs/qa.md`. Not committed.

- `overlay_region` can now be `null` while `preview_region` is set (the safe area is not measured yet). It must be `null` when `preview_region` is `null`.
  - `qa_contract.py`: every status checks only the second rule now. The `--strict` check `overlay_region` waits up to 2 s for a value. When it stays `null`, the check passes with the note "the safe area is not measured".
  - `fake_phone.py`: new mode `--safe-area-unmeasured` (and self-check mode "safe area unmeasured").
- 500 `capture_failed` when the app cannot turn the still: app side only. The fake mode `--capture-fails` and `check_capture_failed` already cover the error code. No change.
- `python3 scripts/fake_phone.py --self-check`: PASSED in 12 modes (the new mode 30/30). ruff is clean.
- I wait for the check of `docs/briefs/qa-round6-followup.md`.

### Round 7, part 2: an empty `overlay_region`

The contract says: a measured safe area with no room gives `width` 0 or `height` 0, not `null`, and clients do not fall back to `preview_region`.

- `qa_contract.py`: the region format check accepts a zero width or height for `overlay_region` only (`preview_region` must still have a size). The `--strict` check `overlay_region` still checks "inside `preview_region`"; for an empty region it does not ask for a move with a vertical flip, and it notes "empty (measured, no room)".
- `fake_phone.py`: new mode `--safe-area-empty` (self-check mode "safe area empty"): the label band reaches the navigation bar, so `overlay_region` has height 0 (`{0.2, 0.94, 0.6, 0}`; with a vertical flip `{0.2, 0.06, 0.6, 0}`).
- `python3 scripts/fake_phone.py --self-check`: PASSED in 13 modes. ruff is clean.

## Round 8: follow-up check

Date: 2026-09-28. Scope: `docs/briefs/qa-round6-followup.md` (all sections, with the B-E3 decision) and the contract changes (C6, C9, C13/N9, N8, empty `overlay_region`, C15 text). The working tree at `fe0ecdb` with the uncommitted follow-up fixes. All other agents had stopped editing. I did not change product code or my scripts. I corrected one row of my `docs/qa.md` (see C16). I did not commit.

### 1. Tests and hooks

| Command | Result |
|---|---|
| `uv run pytest -q -p no:cacheprovider` | 909 passed, 1 skipped |
| `nix develop --command boardview/tests/run.sh` | 13 passed, 1 skipped |
| `nix develop --command prek run --all-files` (on a copy of the working tree, new files added in the copy only) | All hooks pass. No hook changed a file. |
| `(cd android && nix develop .. --command ./gradlew --no-daemon assembleDebug testDebugUnitTest)` | BUILD SUCCESSFUL (`UP-TO-DATE`). Forced `testDebugUnitTest --rerun`: BUILD SUCCESSFUL, 165 tests in 15 suites, 0 failures. |

### 2. QA scripts

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED in 13 modes (default 30/30, "safe area unmeasured" 30/30, "safe area empty" 30/30) |
| `python3 scripts/fake_phone.py --port 18896`, then `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18896 --strict --after-start` | 30/30 |
| `uv run python scripts/qa_mcp_stdio.py --skip-webcam --scratch <dir>` (server with `--no-ui`) | 15/15, with `fire-tv` ("no phone is selected", no command to the fake Fire TV) |

### 3. Re-check of the follow-up items

Three read-only agents re-checked every item against the code and the contract text. I checked the main findings in the code myself ("checked"). All fixes are in uncommitted files. Paths are in `mcp/debug_devices_mcp/` unless they start with another directory; `A/` and `AT/` are the Android source and test directories.

#### Bench safety and meter (N5, N6, B-E2, B-E3, N7)

`uv run pytest -q mcp/tests/test_bench_state.py mcp/tests/test_meter_frames.py mcp/tests/test_webcam_controls.py mcp/tests/test_qa_round6.py`: 114 passed. The reviewer also ran each QA case in memory (a temporary directory for the state file).

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N5 | fixed | `bench_state.py:44-50`, `:337-349`, `:419-423`, `:452`, `:464-465`; tests `test_bench_state.py:525`, `:542` | AC 0.01 V after isolation with the power-check step: recorded, note "residual check needs DC V", the step stays open, the gate stays closed. AC 0.60 V counts as unsafe and closes the gate. |
| N6 | fixed | `bench_state.py:352-353`, `:441-451`, `:504-506`, tool `:699-703` (save, then refuse); tests `test_bench_state.py:557`, `:578`, `:591` | Both orders pass: "VBUS 5.10 V" with a resistance step is refused with "recorded without the step" and closes the gate; an unknown step first, then a safe reading elsewhere: gate closed. After a reload of the file, the point stays unsafe. See N13. |
| B-E2 | fixed | `meter_frames.py:175-181`, `:190`; tests `test_meter_frames.py:201-226` | "DC -5.10" + "DC 5.10": disputed (both orders). "−5.10" (U+2212) + "5.10", "- 5.10" + "5.10", "-0.01" + "0.01": disputed. "-0.00" + "0.00": confirmed. |
| B-E3 rule 1 (label = part.pin or net) | fixed, low gap | `bench_points.py:15-17`, `:41-42`, `:52-82`, `bench_state.py:375-387`, `:424`, `server.py:1385-1390`; tests `test_bench_state.py:619-654` | With a board: "C12.1", "c12 pin 1", "C12:1" give one key; a bare part, a missing pin, and free text are refused. Without a board, `is_generic` accepts "residual1", "test1", "point-A", ".", and "?" with a warning only (with a board they are refused). |
| B-E3 rule 2 (the gate rule) | **partly** | `bench_state.py:185-190`, `:246-279`, `:535-545`, `:585-592`; tests `test_bench_state.py:344`, `:365`, `:414`, `:542` | The QA case "safe reading at another point after a new confirmation" passes (gate closed). Three cases still open the gate: (a) `:419-421`: confirm, capture "C12.1" 5.10 V, the user confirms again, then record that capture: "ok", but no unsafe point is kept; then "VBUS" 0.01 V opens the gate. (b) `:378-386`, the QA case "same generic label at two points": with a board, "residual" 5.10 V is refused and sets only `last_unsafe_at`, no point; after a new confirmation, "C12.1" 0.01 V opens the gate; a later record of the 5.10 V capture as "C12.2" is "ok" but adds no point (older than the confirmation). (c) `:250-252` replaces a point with no time check: 0.01 V at t1 and 5.10 V at t2, both at "C12.1"; record t2, then t1: the point becomes safe with the older reading; a new confirmation and "VBUS" 0.01 V open the gate. |
| B-E3 rule 3 (user clears one point) | fixed | `bench_state.py:469-497`, `:633-634`, `:667-668`; test `test_bench_state.py:385` | An empty reason, an unknown point, or "residual" is refused. The clearance counts only for that point; the gate still needs a newer confirmation. |
| N7 | fixed (QA case), low rest | `webcam_controls.py:66-74`, `:201`; test `test_qa_round6.py:181` | Auto on, then `exposure=900`: camera and file both manual 900, also after a restart. Rest: one call with `auto_exposure=true, exposure=900` sets the camera to auto but saves `exposure: 900` (`:70-74` has no case for both); a file from before the fix (`auto_exposure: true, exposure: 900`) is not migrated. |

New findings (bench):

- **N13 (safety, medium, checked): an unsafe voltage reading that is not "confirmed" does not close the gate.** `bench_state.py:412-416` refuses a result that is not confirmed before any gate logic, and `multimeter_read` does not change the bench state. Case: the gate is open after "C12.1" 0.01 V; then "VBUS" frames "DC -5.10" + "DC 5.10" (now disputed, because of the B-E2 fix) or "5.10" + "5.12" (uncertain): `bench_record_measurement` refuses, the gate stays open, and a resistance step is allowed. Before the B-E2 fix, the first case was a confirmed -5.1 V that closed the gate. Fix: a voltage result above the safe limit after the confirmation closes the gate (sets `last_unsafe_at` or an unsafe point, and saves), also when it is not confirmed.
- **N14 (low): one capture can clear several points.** `bench_state.py:425` returns the stored measurement for a repeated capture id, and `:429-438` sets a point from the new label. Case: "C12.1" 5.10 V, new confirmation, a 0.01 V capture recorded as "VBUS", then the same capture recorded as "C12.1": gate open.
- **N15 (design question): an unsafe reading from before the current confirmation never becomes a residual point** (`bench_state.py:419`). Case: power unknown, "C12.1" 12.00 V, isolate and confirm, then "VBUS" 0.01 V: gate open. Rule 2 says "every label whose latest reading was unsafe"; that can include a reading taken with power on.
- **N16 (low): the net "GND" is a valid residual point** (`bench_points.py:67-69`). A reading at GND is always about 0 V, so it gives "one safe DC reading after the confirmation". Other unsafe points still block the gate.
- **N17 (low): a refused step can open the gate but leave the power-check step open** (`bench_state.py:441-448` raises before `:464-465`). The refusal also says "do not record it again", so the step stays open until another reading.

#### Android and contract (N8, N9/C13, N10, C6, C9, empty `overlay_region`)

`uv run pytest -q mcp/tests/test_overlay_region.py mcp/tests/test_highlight.py mcp/tests/test_phone_api.py mcp/tests/test_pointing.py`: 48 passed. The Android tests pass in the Gradle run (1.).

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N9 + C13 | fixed | `A/JpegTurner.kt:26-39` (no turn: the same bytes; a failed decode, encode, or out-of-memory: `ApiException(CAPTURE_FAILED, "could not turn the still")`, `A/Constants.kt:154`), `A/CameraController.kt:781`; tests `AT/JpegTurnerTest.kt:20-54`. Contract `docs/phone-api.md:78-79` agrees (the old "can carry this as its EXIF orientation" is gone). Real phone: turned pixels, EXIF absent or 1. | The real `BitmapPixelTurner` has no unit test (the tests use fakes). The client still applies EXIF (`M/images.py:35`, `:97-115`), although `docs/phone-api.md:79` says that clients do not read it; no effect with the new app. |
| N10 | fixed (status reads) | `A/OverlayLayout.kt:462-475` (no label band until the overlay has the width of the new orientation), `A/MainActivity.kt:106-132`, `A/CameraController.kt:620`; test `AT/OverlayLayoutTest.kt:218-230` | The MainActivity wiring has no test. New N11 below: the rotation POST response itself has `null`. |
| N8 + empty region | fixed | App: `A/CameraController.kt:614-623`, `A/Overlay.kt:367` (no room: size 0, not `null`); test `AT/OverlayLogicTest.kt:288-291`. Client: `M/phone_api.py:158-161`, `M/server.py:464-466`, `:551-552`, `M/pointing.py:499-500`, `M/highlight.py:107-118` (size 0: not visible, warning), `M/pointer.py:128-132`, page `app.js:1199`; tests `mcp/tests/test_overlay_region.py:87`, `:117`, `:129`, `:137` | Edge: `A/OverlayLayout.kt:470` returns `null`, not size 0, when the window has a size but the insets use all of it (`A/MainActivity.kt:107-108`, a very small split window). Clients then use `preview_region`. See also N12. |
| C6 | fixed | `docs/phone-api.md:95`, `:101`, `docs/overlay-layout.md:24` agree; `A/OverlayView.kt:68-97`, `A/OverlayLayout.kt:142-146`, `:487`; test `AT/OverlayLayoutTest.kt:242-249` | – |
| C9 | partly (text) | `docs/phone-api.md:100`: the zoom and flips contradiction is gone ("rotation and the screen size") | The null rule is still there two times on `docs/phone-api.md:100`: "`null` before the camera is bound." and at the end "It is `null` before the camera is bound or while the preview has no size." Remove the first one. |

New findings (Android and contract):

- **N11 (low, confirmed on the S22): the `POST /v1/rotation` response has `overlay_region: null` for a change between portrait and sideways.** `A/CameraController.kt:741-746` reads the status in the same main-thread block, before the layout pass of the new orientation (`A/MainActivity.kt:225-250`), so `labelBand` returns `null` (`A/OverlayLayout.kt:473`). On the phone: `{"degrees": 90}` and `{"degrees": 0}` both returned `null`; a status 1 s later had a region. The contract allows `null` only "while the safe area is not measured yet (the window has no size)". The client stores this `null` (`M/server.py:1007-1009`) until the next status and then uses `preview_region` (`M/pointing.py:442`); `phone_snapshot` reads a fresh status first, so the effect is small. Fix: extend the contract text, or answer after the layout pass. `mcp/tests/test_overlay_region.py:94` uses a fake that returns a region here.
- **N12 (low): an empty region from the app is at the top-left corner of the preview** (`A/Overlay.kt:367`: `{preview.x, preview.y, 0, 0}`). The client takes the arrow angles and distances from the centre of the view (`M/pointer.py:176-178`), which is now that corner. Case: the label covers the safe area and the target is in the middle of the still: the arrow points down-right with the full distance. My fake gives a region with the width and a real y (`{0.2, 0.94, 0.6, 0}`), so the fake and the app differ. The contract does not say where an empty region is.
- Cosmetic: an old comment at `A/MainActivity.kt:98` stays above the new one; `M/phone_api.py:120-123` (`PreviewRegion.empty`) is used only in tests.
- Note: on the S22, `overlay_region` also changes with the status label text. It was `{0.192, 0.109, 0.615, 0.829}` and `{0.192, 0.091, 0.615, 0.847}` at the same rotation and flips, a few seconds apart. The contract says to read it again after a flip or rotation change only.

#### Server and page (C11, C16, N2, N3, N4, C15)

`uv run pytest -q` on the 14 test files of this area: 131 passed.

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| C11 | fixed, low edge | (a) `ui/forward.py:93`, `:96-139` (`RemoteOverlays`: forgets the boxes of a secondary server after `OVERLAY_TTL` and on a new app run), `ui/monitor.py:497-500`, `:516-528`, `:564`, `:598`, `:609`, `:957`, `server.py:343-344`. (b) `server.py:376-381`, `:411-418`, `:452-453` (the TTL follows the server's own record, also for an old app). Tests `test_qa_round6.py:42`, `:54`, `:65`, `:80` | The paths that call `app_run` (status tools, poll, restart notice) have no test. See N19. |
| C16 | fixed | `mcp/README.md:17`, `:34`, `:199`, `:264`, `.env.example:31-33`, `config.py:120-123`, `AGENTS.md:55` agree | `docs/qa.md:203` still said "asks for `--adb-serial`" for several devices. My file: corrected in this round. |
| N2 | **partly (checked)** | `ui/routes/ingest.py:96-106` (403 and a log line), `ui/setup.py:31`; tests `test_qa_round6.py:100-113`, `test_bench_feedback.py:142`, `:172` | With no selection, `selected` is `""` (`ingest.py:98`), so a body `{"serial": ""}` passes the check and calls `start_screen_for("")`. `PhoneScreen` has no empty-serial guard (`phone_screen.py:287-302` runs `adb -s "" push`, `forward`, `shell app_process`); `scrcpy.py:70` has one. The request needs the ingest token. I did not test what the real adb does with `-s ""` (the adb server has one device now, so it can reach it). Fix: refuse when nothing is selected, and add a test. |
| N3 | fixed | `ui/settings.py:135-139` (`asyncio.to_thread`), all async callers (`camera_choice.py:70-75`, `orientation.py:219-227`, `devices.py:124-131`, `ui/monitor.py:643-645`, `ui/routes/settings.py:41`, `:49`, `server.py:426`, `:1052`, `:1098`, `:1112`, `ui/device_panel.py:117-123`); test `test_qa_round6.py:121-145` | See N20. |
| N4 | fixed, one edge | `server.py:571-592`, `adb.py:185-195`, `:280-283`, `ui/monitor.py:932-938`; test `test_qa_round6.py:153-173` | "device offline" also clears the record (`adb.py:39` counts it as gone; `server.py:583-585`), but adb keeps the forward. The brief allows the clear only for "not found". The test makes `forward --list` fail, not `forward --remove`. |
| C15 | fixed | `board/model.py:264-266`, `pointer.py:168-209`, `pointing.py:461`, `board/marking.py:70`, `:86`, `marking_readings.py:38`, `:43`, `board/tools.py:545`, `:554`, `:821`, `:984`, `model.py:349`; tests `test_side_labels_c15.py:63-114` (synthetic fixtures `sides.json`, `markings.json`, listed in the fixtures README) | Low, outside the brief: the page net search `ui/board.py:295` still orders parts by the label alone; in a mixed file, a part on the other side goes last and can be cut at 8 boxes. |

adb rule re-audit: the only new adb call is `release_forward` (`forward --remove` of this server's own forward, after an owner check with `forward --list`). With no selection, only `devices -l` runs. The only gap is the N2 empty serial.

New findings (server and page):

- **N18 = N2 rest (see the table).**
- **N19 (low): the app run that a secondary server sends with its boxes can be old** (`ui/monitor.py:497-500` reads `bus.phone.status`, which only the status tools, `phone_connect`, and its poll update). Case: a secondary without its own poll calls `phone_status` (run 1); the app restarts; `phone_snapshot` sees run 2; `phone_highlight` sends run 1; the primary poll sees run 2 and removes the new boxes from the page, while the phone still shows them. Fix: send the app run of the status of the overlay call (`server.py:378`).
- **N20 (low): two flip changes at the same time can lose one.** `orientation.py:219-224` computes the new flips before it waits for the file lock. Case: the page "Flip H" and an agent's `phone_snapshot_orientation(flip_vertical=true)` at the same time: the second write drops the first flip. Fix: compute the flips inside the locked update.

### 4. Real phone

- Selected serial (`~/.local/state/debug-devices/ui-settings.json`, `adb_serial`): the Wi-Fi serial of the Samsung S22 (`SM_S901B`). `adb devices -l` listed only this device, in state `device`. I used the adb binary of the running adb server (`android-tools-37.0.0`).
- Separate forward `tcp:18791 tcp:8765` on the selected serial only. Health 200. Status 200: the app is in the foreground, so the phone is unlocked.
- `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18791 --strict`: 29/29.
  - Snapshot 3060x4080 at rotation 0 and 4080x3060 at 90 and 270 (turned pixels); EXIF `Orientation` absent, or 1 at 90; `X-Rotation-Degrees` equals the locked rotation.
  - `preview_region` `{0.192, 0, 0.615, 1}`; turned at 90; zoom and flips keep it.
  - `overlay_region` `{0.192, 0.109, 0.615, 0.829}`; a vertical flip moves it to y 0.062; inside `preview_region`.
- After the run I sent `{"in_sensor_zoom": true}` again (the saved setting). Right after that call, `overlay_region` was `{0.192, 0.091, 0.615, 0.847}`. Within 3 s it was back to `{0.192, 0.109, 0.615, 0.829}`: the status label changed for a short time. Then every field (except `focus`) equalled the first status.
- Later, to confirm N11, I made the same kind of forward again (`tcp:18791`) and sent `POST /v1/rotation {"degrees": 90}`, `{"degrees": 0}`, then `{"auto": true}`. Both locked rotations returned `overlay_region: null`; a status 1 s later had a region. The rotation is back to auto (0, not locked). At that time `overlay_region` was `{0.192, 0.091, 0.615, 0.847}` again: it follows the label text.
- I removed the forward both times. Only the MCP server forward (18765) remains. No command went to another device.
- Not tested: `--after-start` and `--expect starting-race` (they need an app restart).
- Note: `overlay_region` follows the height of the status label, so it can change for a moment after a command that changes the label text. The contract says to read it again after a flip or rotation change; it does not mention label changes.

### Summary

- Tests: all pass (pytest 909, boardview 13, Android 165, prek, fake self-check 13 modes, contract 30/30 on the fake and 29/29 on the S22, MCP stdio 15/15).
- Follow-up items: N5, N6, B-E2, N3, C6, C13/N9, N8, the empty `overlay_region`, C15, and C16 are fixed. B-E3 rule 1 and rule 3 are fixed. C11, N4, N7, and N10 are fixed with small open edges.
- Not fully fixed:
  - B-E3 rule 2 (the gate rule): three cases still open the gate (`bench_state.py:419-421`, `:378-386`, `:250-252`).
  - N2: an empty serial passes the ingest check when nothing is selected (`ui/routes/ingest.py:98-106`).
  - C9: the null sentence is still there two times (`docs/phone-api.md:100`).
- New:
  - Safety, medium: N13, an unsafe voltage that is not confirmed does not close the gate (`bench_state.py:412-416`).
  - Low: N11 (confirmed on the S22), N12, N14 to N17, N19, and N20.

### What to do next

1. Safety: N13, then B-E3 rule 2 cases (a), (b), and (c), then N14 and the N15 decision.
2. N2: refuse the ingest screen start when nothing is selected, and guard `PhoneScreen` against an empty serial.
3. Contract text: C9 (one null sentence), N11 (the rotation response can have `null`, or answer after the layout pass), N12 (where an empty region is), and whether `overlay_region` follows the label text.
4. When the user allows an app restart: `--after-start` and `--expect starting-race` on the S22.

## Round 9: N12 in the fake and the checks

Task: `docs/briefs/qa-round8-followup.md`, N12 (my part). I changed only `scripts/fake_phone.py`, `scripts/qa_contract.py`, and `docs/qa.md`. Not committed.

- Contract: an empty `overlay_region` (`width` 0 or `height` 0) is at the centre of `preview_region`, so that directions from it stay meaningful.
- `qa_contract.py`: every status with an empty `overlay_region` checks that its centre equals the centre of `preview_region` (2 % tolerance). A centred point and a centred band both pass.
- `fake_phone.py`: `--safe-area-empty` now gives a point of size 0 at the centre of `preview_region` (`{0.5, 0.5, 0, 0}` at rotation 0), not `{0.2, 0.94, 0.6, 0}`.
- `python3 scripts/fake_phone.py --self-check`: PASSED in 13 modes. ruff is clean.
- Proof: the committed fake (`598ff0a`) with `--safe-area-empty`, then `qa_contract.py --strict`: every check fails with "empty overlay_region … is not at the centre 0.5000, 0.5000 of preview_region".
- The app still returns the top-left corner of `preview_region` (`A/Overlay.kt:367`, `PreviewRegion(preview.snapshotX, preview.snapshotY, 0f, 0f)`); the brief gives this to dd-android. The strict run on the phone will show it when the safe area is empty.
- I wait for the check request.

## Round 10: last-round check

Date: 2026-09-28. Scope: `docs/briefs/qa-round8-followup.md` (all sections), the D7 default (bulk clear), and the contract changes (C9 duplicate removed; an empty `overlay_region` at the centre of `preview_region`). The working tree at `598ff0a` with the uncommitted fixes. All other agents had stopped editing. I did not change product code or my scripts, and I did not commit.

### 1. Tests and hooks

| Command | Result |
|---|---|
| `uv run pytest -q -p no:cacheprovider` | 943 passed, 1 skipped |
| `nix develop --command boardview/tests/run.sh` | 13 passed, 1 skipped |
| `nix develop --command prek run --all-files`, first on a copy (new files added in the copy only) | All 15 hooks pass. No hook changed a file in the copy. |
| The same in the real tree, with `sha256sum` of every file (`git ls-files -co --exclude-standard`, 292 files) and `git status --porcelain` before and after | All 15 hooks pass (also ktlint). The hashes and the git status are unchanged: prek leaves the tree unchanged now. |
| `(cd android && nix develop .. --command ./gradlew --no-daemon assembleDebug testDebugUnitTest)` | BUILD SUCCESSFUL (`UP-TO-DATE`). Forced `testDebugUnitTest --rerun`: BUILD SUCCESSFUL, 169 tests in 16 suites, 0 failures. |

### 2. QA scripts

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED in 13 modes (with "safe area empty": a point at the centre of `preview_region`) |
| `python3 scripts/fake_phone.py --port 18894`, then `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18894 --strict --after-start` | 30/30 |
| `uv run python scripts/qa_mcp_stdio.py --skip-webcam --scratch <dir>` (server with `--no-ui`) | 15/15, with `fire-tv` ("no phone is selected", no command to the fake Fire TV) |

### 3. Re-check of the last-round items

Three read-only agents re-checked every item against the code and the contract text, and ran the bench cases in memory. I checked the main findings in the code myself ("checked"). All fixes are in uncommitted files. Paths are in `mcp/debug_devices_mcp/` unless they start with another directory; `A/` and `AT/` are the Android source and test directories.

#### Bench safety (N13, B-E3 rule 2, N14-N17, D7)

`uv run pytest -q mcp/tests/test_bench_state.py mcp/tests/test_meter_frames.py mcp/tests/test_qa_round8.py`: 132 passed. The reviewer ran every case in memory against `bench_state.py` (state files in a temporary directory).

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N13 | **partly (checked)** | `bench_state.py:258-289` (the LCD voltage of the result and of each frame, no confirmed value needed), `:466-541`, `:567-577` (save, then refuse), `:651-664`, `server.py:866` (`multimeter_read` and `bench_measure` also note the reading); tests `test_bench_state.py:697`, `:711`, `:724` | Pass: "DC -5.10" + "DC 5.10" (disputed) and "5.10" + "5.12" (uncertain) close the gate; an unconfirmed 0.01 V opens nothing; unsafe AC, `OL`, 600 mV, and V with no unit close the gate. Gaps: (1) `:265-266`: a diode-mode reading never counts, with no upper limit. The model reads mode "diode" with "5.10 V" in both frames; `combine` says "the dial is probably on DC V" (uncertain), but the gate stays open. (2) `:287`: the frames use `result.mode` (the first frame), not their own mode. Frames "0.60" diode + "5.10" DC V give a disputed result, and the gate stays open. (3) Low: mode "other", no unit, "5.10": the gate stays open. |
| B-E3 rule 2 (a) | fixed | `bench_state.py:482-483`, `:510-533`; test `test_bench_state.py:750` | – |
| B-E3 rule 2 (b) | fixed | `bench_state.py:476-483`, `:522-528`; test `test_bench_state.py:659` | – |
| B-E3 rule 2 (c) | fixed | `set_point` `bench_state.py:319-326`; test `test_bench_state.py:763` | At the same time stamp, the unsafe reading wins in both orders. |
| N15 | fixed | `bench_state.py:482`; test `test_bench_state.py:778` | – |
| N14 | fixed | `label_conflict` `bench_state.py:551-564`, `:605-607`; test `test_bench_state.py:792` | – |
| N16 | fixed, low gap | `bench_points.py:25` (`GROUND_NET_PATTERNS`), `:45-46`, `:72-73`, `:86-87`, `:101-102`, `bench_state.py:719`; tests `test_bench_state.py:809-820`, `:960` | Refused as ground: gnd, GND1, PGND_2, VSS_IO, DGND, AGND, VSS, 0V, EARTH, CHASSIS_GND, and a board pin on a ground net. Accepted (gap): "GROUND", "Ground", "GRND", "0 V" (also a board net named GROUND, `:86`). `gate()` (`bench_state.py:351-353`) does not filter ground points (only old records). |
| N17 | fixed, low wording | `step_refusal` `bench_state.py:435-451`, `:612`, `:621-632`; test `test_bench_state.py:832` | For a mode mismatch (DC V with a resistance step), the text says "record the same capture_id again with that step_id", which is refused again every time. It must say "with a step that fits". |
| D7 bulk clear | **partly** | `clear_all_points` `bench_state.py:698-762`, tool `:900-901`, `:941`; tests `test_bench_state.py:872-981` | Pass: empty or missing `user_words` refused; no confirmation, or power on: refused; no safe DC reading after the confirmation: refused; AC or a single clear cannot be the anchor; the words are stored with `cleared_at` and `cleared_points`; an unsafe reading after the confirmation still needs a new confirmation and a new safe reading. It opens the gate by itself only when every unsafe reading came before the confirmation (by design). See N24. |

New findings (bench):

- **N24 (safety, medium): a point that the bulk clear kept is lost when its capture gets a name.** `bench_state.py:528` removes the unknown point without a condition, then `set_point` (`:532`) refuses because the named point was cleared (`:744` gives a cleared point the clear time). Case: "C12.1" 12 V with power on, confirm, "VBUS" 0.01 V (the anchor); then `multimeter_read` 5.10 V at C12.1 (kept as an unknown point); the bulk clear clears C12.1 and keeps the unknown point; `record_measurement` of that capture as "C12.1" returns "ok" and says the gate is "closed at 'C12.1'", but the unknown point is gone and C12.1 stays cleared. After a new confirmation and "VBUS" 0.01 V, the gate is open, although the 5.10 V came after the anchor.
- **N25 (safety, low): a parallel write can lose the unsafe point that `multimeter_read` adds.** No bench-state tool locks the file from load to save, and `note_meter_reading` (`server.py:866`) is a new writer. `bench_record_measurement` loads, then awaits `open_board()` (`bench_state.py:972-973`), then saves; `bench_measure` runs `add_photo` in a thread (`server.py:806`) during the meter read. Simulated with a slow `open_board`: the notice said "above the safe residual limit", but the saved state had no unsafe point and the gate was open.
- N26 (low): a later unsafe reading closes the gate but does not reopen a power-check step that is done (`complete_power_checks`, `bench_state.py:793-797`, only closes steps). The gate still blocks; only the next step is wrong.
- Note (D7 design): the bulk clear also clears unsafe readings taken after the isolation, when they are older than the anchor. The gate then still needs a new confirmation.
- Low: `user_words` without `clear_all_residual_points: true` is ignored with no message (`bench_state.py:941`).

#### Server and page (N2, N19, N20, N12 client)

`uv run pytest -q` on 13 test files of this area: 46 + 86 passed.

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N2 rest | fixed | `ui/routes/ingest.py:97-106` (`not selected or data.serial != selected`: 403 and a log line), `phone_screen.py:188-192` (refuses an empty serial before any adb call); other guards `adb.py:151-155`, `scrcpy.py:68-70`, `devices.py:146-153`; tests `test_qa_round8.py:42-60` | The test does not check the log line. Cosmetic: the log line says "asked to stream , but the selected phone is none" for an empty serial. |
| N19 | fixed | `server.py:380-390` (the status of the overlay POST updates the app run first), `server.py:468-478`, `ui/setup.py:28`, `ui/monitor.py:496-500`, `:561-566`; test `test_qa_round8.py:76-94` | – |
| N20 | fixed | `orientation.py:208-247` (the flips merge inside the locked update), `ui/settings.py:126-133`; tests `test_qa_round8.py:102-117` | 100 concurrent pairs in memory lost no flip. See N29 for another writer. |
| N12 client | fixed, one edge | `phone_api.py:158-161`, `server.py:470-473`, `:562-563`, `highlight.py:103-114` (empty region: not visible, warning), `pointing.py:438-447`, `:499-501` (no fallback), `pointer.py:128-132`, `:150`, `:177`; tests `test_qa_round8.py:125-145`, `test_overlay_region.py:116-143` | See N27 and N28. |

New findings (server and page):

- **N27 (low): `edge_point` raises `ValueError` when a target is exactly at the centre of an empty view** (`pointer.py:87-96`, called at `:195`): the direction is (0, 0), so `min()` gets an empty list. `pointing.py:353-357` catches only `ToolError`, so the error reaches `phone_point_to` or the live tracking refresh. Unlikely with real homographies. The dd-ui report says that this case gives the centre; it does not. Fix: return the centre when there is no direction.
- **N28 (low): the arrow distance is wrong for a centred band** (width > 0, height 0; the contract allows it, the app sends only a point). `pointer.py:195-197` measures from the band end. Case: band `(200, 400, 600, 0)` px, a part 0.5 cm left of the centre: the label says "~2 cm".
- **N29 (low): `delete_crop` builds the new settings from an old in-memory copy** (`ui/routes/settings.py:46-50`). A `screen_rotation` change and a crop delete at the same time can undo the rotation change. Only the page does this.
- **N30 (adb rule, low, code from before this round): a secondary server's own screen stream keeps using the old serial after the user selects another phone.** `phone_screen.py:260-271`: `_run` restarts the session with the same serial and never checks the selection again. Case: the primary has `--no-phone-screen` (or cannot be reached), so the secondary streams itself (`ui/monitor.py:620-624`); the user selects another phone in the primary page; the primary stops only its own stream (`ui/monitor.py:919-930`); the secondary sends `adb -s <old serial>` push, forward, and shell again after each restart delay. This breaks the AGENTS.md rule "adb only to the selected serial".

#### Android and contract (N11, N12, C9)

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N11 | fixed in the app (confirmed on the S22); contract text partly | `A/CameraController.kt:744-753` (the rotation POST waits for the layout pass of the new orientation), `A/Waits.kt:10-14`, `A/Constants.kt:89-90` (500 ms limit, 16 ms steps, named); test `AT/WaitsTest.kt:15-33` (the helper only). S22: each rotation response has the region of the new orientation (section 4). | (1) `docs/phone-api.md:101` allows `null` only "while the safe area is not measured yet (the window has no size)". The app still gives `null` with a sized window in three short cases: (a) the rotation response after the 500 ms limit (`CameraController.kt:749` ignores the result); (b) a `GET /v1/status` during the rotation wait or after a physical turn in auto mode (`status()` does not wait, `CameraController.kt:526-529`, `OverlayLayout.kt:480`); (c) no wait when a layout is already pending before the POST (`measuredBefore` false, `:744`). The contract text must name these cases, or the app must avoid them. (2) No test of the `setRotation` wiring (`AT/ApiServerTest.kt:98` uses a fake camera). |
| N12 | fixed | `A/Overlay.kt:367-374` (size 0 at the centre of `preview_region`; the only place that makes an empty region), caller `A/CameraController.kt:614-624`; tests `AT/OverlayLogicTest.kt:288-299`, `AT/OverlayLayoutTest.kt:229-239`. The fake and the contract agree. | Low test gap: both test geometries have `preview_region` centred at (0.5, 0.5), so a constant (0.5, 0.5) also passes. No real effect: the app crop is always centred. |
| C9 | fixed | `docs/phone-api.md:100`: the null rule once, and "zoom and the preview flips do not change it" agrees with `OverlayLogic.previewRegion` (test `AT/OverlayLogicTest.kt:208-216`) | – |
| `overlay_region` and the label text | not fixed (open since round 8) | `A/MainActivity.kt:116`, `:147-153`, `A/OverlayLayout.kt:481` | The region follows the height of the status label, and the label is drawn again every 1 s with the focus distance. On the S22 (round 8), y was 0.109 and 0.091 a few seconds apart at the same rotation and flips. `docs/phone-api.md:101` says to read it again only "after a flip or rotation change". Also: while `overlay_region` is `null`, the app draws no boxes (`A/OverlayView.kt:63`), but the contract tells clients to use `preview_region` then. |

New findings (Android):

- Threading: no deadlock. Ktor CIO runs the handler (`A/ApiServer.kt:25`, `:81`); `setRotation` suspends on Main (`CameraController.kt:742`), and the wait uses `delay` (`Waits.kt:12`), so the layout pass runs between the checks. The control mutex is held for at most 500 ms (`A/ControlGate.kt:28-31`); status reads do not wait for it.
- N21 (low): `onDestroy` stops the server on Main (`A/MainActivity.kt:206`) and waits up to 500 ms (`A/Constants.kt:9`) for running requests. A rotation request in its wait needs Main to resume, so Main stays blocked for the full 500 ms. It has a limit; it is not a deadlock.
- N22 (low): after the wait, `readStatus` (`CameraController.kt:753`) does not check the foreground again: if the app pauses during the wait, the response is 200 with a status, not 503 `camera_not_ready`. Other `runControl` endpoints have the same pattern.
- N23 (low): a timeout of the rotation wait writes no log line (`CameraController.kt:749`), so a `null` in a response cannot be traced on the phone.

### 4. Real phone

- Selected serial (`~/.local/state/debug-devices/ui-settings.json`, `adb_serial`): the Wi-Fi serial of the Samsung S22 (`SM_S901B`). `adb devices -l` listed only this device, in state `device`. I used the adb binary of the running adb server (`android-tools-37.0.0`).
- Separate forward `tcp:18792 tcp:8765` on the selected serial only. Health 200. Status 200: the app is in the foreground, so the phone is unlocked. The `app_start_id` is new since round 8 (the new APK).
- `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18792 --strict`: 29/29. Turned pixels (3060x4080 at 0, 4080x3060 at 90 and 270), `preview_region` turned at 90, `overlay_region` `{0.192, 0.109, 0.615, 0.829}` moves with a vertical flip. The safe area was not empty, so the N12 centre rule did not apply on the phone.
- N11 on the phone: `POST /v1/rotation` with `{"degrees": 90}`, `{"degrees": 0}`, `{"degrees": 270}`, and `{"auto": true}`. Each response has the `overlay_region` of the new orientation (for example 90: `{0.035, 0.268, 0.904, 0.540}`; 270: `{0.062, 0.268, 0.904, 0.540}`), in about 55-80 ms. In round 8, the responses at 90 and 0 were `null`.
- After the run I sent `{"in_sensor_zoom": true}` again (the saved setting) and the rotation is auto. 3 s later, every field (except `focus`) equalled the first status.
- I removed the forward. Only the MCP server forward (18765) remains. No command went to another device.
- Not tested: `--after-start` and `--expect starting-race` (they need an app restart).

### Summary

- Tests: all pass (pytest 943, boardview 13, Android 169, fake self-check 13 modes, contract 30/30 on the fake and 29/29 on the S22, MCP stdio 15/15). prek passes and leaves the tree unchanged (292 file hashes and the git status equal before and after).
- Fixed: B-E3 rule 2 cases (a), (b), (c), N2 rest, N12 (app and client), N14, N15, N19, N20, and C9. N11 is fixed in the app (confirmed on the S22). N16 and N17 are fixed with small gaps.
- Not fully fixed:
  - **N13 (safety, checked):** a diode-mode reading never counts for the gate, with no upper limit ("5.10 V" in diode mode leaves the gate open, `bench_state.py:265-266`); the frames use the mode of the first frame (`:287`).
  - **D7 bulk clear (safety):** N24, a kept point is lost when its capture gets a name (`bench_state.py:528`, `:532`, `:744`).
  - N11 contract text: `docs/phone-api.md:101` does not name the short `null` cases with a sized window.
  - `overlay_region` and the label text (open since round 8).
- New: N24 (safety, medium), N25 (safety, low, parallel writes), N30 (adb rule, low, secondary stream after a phone switch), and low N21-N23, N26-N29.

### What to do next

1. Safety: N13 gaps (a diode reading above a named limit, for example the meter's diode test voltage, counts as a voltage; use each frame's own mode), N24, then N25 (one lock from load to save for the bench state).
2. N30: the secondary stream checks the selection before each restart.
3. Contract text: the N11 `null` cases, and whether `overlay_region` follows the label text.
4. Low: N27 (centre target), N28 (band distance), N17 wording, N16 names ("GROUND", "GRND", "0 V").
5. When the user allows an app restart: `--after-start` and `--expect starting-race` on the S22.

## Round 11: check

Date: 2026-09-28. Scope: `docs/briefs/qa-round10-followup.md` (all sections), the N11 contract sentence (`ROTATION_LAYOUT_WAIT`), the dd-android-2 crash fix (fast Back and restart: `BindException`, `ServerHost`), and the new local 7-segment decoder (`docs/briefs/sevenseg.md`, `docs/reports/dd-meter.md`). The working tree at `681cecf` with the uncommitted changes. All other agents had stopped editing. I did not change product code or my scripts, and I did not commit.

### 1. Tests and hooks

| Command | Result |
|---|---|
| `uv run pytest -q -p no:cacheprovider` | 1045 passed, 1 skipped |
| `nix develop --command boardview/tests/run.sh` | 13 passed, 1 skipped |
| `nix develop --command prek run --all-files`, first on a copy (new files added in the copy only) | All 15 hooks pass; no file changed in the copy |
| The same in the real tree, with `sha256sum` of every file (`git ls-files -co --exclude-standard`, 313 files) and `git status --porcelain` before and after (after pytest had ended) | All 15 hooks pass. Hashes and git status unchanged: the tree stays unchanged. |
| `(cd android && nix develop .. --command ./gradlew --no-daemon assembleDebug testDebugUnitTest)` | BUILD SUCCESSFUL (`UP-TO-DATE`). Forced `testDebugUnitTest --rerun`: BUILD SUCCESSFUL, 177 tests in 16 suites, 0 failures. |

### 2. QA scripts

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED in 13 modes |
| `python3 scripts/fake_phone.py --port 18893`, then `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18893 --strict --after-start` | 30/30 |
| `uv run python scripts/qa_mcp_stdio.py --skip-webcam --scratch <dir>` (server with `--no-ui`; the local decoder is off by default) | 15/15, with `fire-tv` |

### 3. Re-check

Three read-only agents re-checked every item against the code and the contract text, ran the cases in memory, and compared the decoder modes with `HEAD`. I checked the main findings myself ("checked"). All changes are uncommitted. Paths are in `mcp/debug_devices_mcp/` unless they start with another directory; `A/` and `AT/` are the Android source and test directories.

#### Bench safety (N13 rest, N24, N25, N26, N16, N17)

`uv run pytest -q mcp/tests/test_bench_state.py mcp/tests/test_meter_frames.py`: 144 passed. The reviewer ran every case in memory and through the real tools (`register_bench_state_tools`, file store in a temporary directory).

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N13 rest | fixed, low gap | `bench_state.py:274-302` (diode rule `:295-297`: counts above the limit), `:305-311` (each frame uses its own mode, `:309`), `:69` (mode "other" counts), `:391-394`, `server.py:690`, `:838`; tests `test_bench_state.py:1003-1031` | Gate closed (correct): diode "5.10 V" in both frames; "0.60" diode + "5.10" DC V; diode 3.01 V and 3001 mV; -5.10 V; "OL" diode + "OL" DC; mode "other" with no unit and "5.10". Gate stays open (correct): diode 3.00 V, 2.95 V, 0.60 V; diode "OL" alone (an open diode test shows OL). Gap: diode mode with an unreadable unit and "5.10" never counts (`:69`, `:288`). |
| N24 | fixed | `bench_state.py:819-825`, `:604`, `:608`; test `test_bench_state.py:1038` | The round 10 case through the real tools: after naming the capture "C12.1", the point stays unsafe at 5.10 V, and after a new confirmation and "VBUS" 0.01 V the gate stays closed. |
| N25 | fixed | `bench_state.py:398-413` (`update`: load, change, save under one lock; `update_async` in a thread), `:415-436` (thread lock and `flock` on `bench-state.json.lock`, 5 s limit); tests `test_bench_state.py:1059`, `:1086` | Every writer uses the lock: `bench_state_update` `:1070`, `bench_record_measurement` `:1110`, `bench_probe_short` `:1137`, `add_photo` `:930`, `note_meter_reading` `:740`. The round 10 case (slow `open_board` with a 5.10 V reading in the middle) keeps both entries and closes the gate. 60 photo ids and 10 unsafe readings from two stores on one file: all saved. The event loop kept running while another holder had the lock. See N31 and N33. |
| N26 | fixed | `reopen_power_checks` `bench_state.py:867-872`, `:608-609`, `:702-703`; test `test_bench_state.py:1099` | See N35. |
| N16 | fixed | `bench_points.py:25`, `:45-48`, `:86-89`, `:103`, `bench_state.py:374`, `:800`; tests `test_bench_state.py:1121`, `:1126` | "GROUND", "Ground", "GRND", "0 V", "G N D", and board nets with these names are refused. See N36. |
| N17 | fixed | `bench_state.py:706-710`; test `test_bench_state.py:1139` | The tool docstring (`:1092-1093`) still says "record the same capture_id again with the step". |

New findings (bench):

- **N31 (low, checked): `bench-state.json.lock` is not git-ignored.** The running MCP server made `bench-state.json.lock` in the repository root at 22:17:58 (`bench_state.py:55`, `:422`); `git status` shows `?? bench-state.json.lock`. `.gitignore:23` lists only `bench-state.json`.
- **N32 (safety, low): a recent user-confirmed diode mode hides a DC voltage from 0.5 to 3.0 V.** The user mode (up to 10 min old, `bench_state.py:59`) replaces the model's mode (`multimeter.py:464`), and `lcd_voltage` does not look at the model's mode (`bench_state.py:308-309`). Case: user mode diode, the model reads DC V "2.50 V" in 2 frames: confirmed diode, it does not count, and the gate stays open.
- **N33 (safety, low): an unsafe reading is lost when the lock wait passes 5 s.** `note_meter_reading` then returns only a notice (`bench_state.py:739-744`, `:424-431`). Case: another process holds the lock file, then a 7.00 V reading: after 5 s, "cannot record it", and nothing is saved.
- **N34 (low): the refusal text can say "closed" while the gate is open.** `unsafe_event` always writes "the bench safety gate is closed at 'C12.1'" (`bench_state.py:611-618`), also when `set_point` (`:608`) keeps a newer safe entry. Case: a newer safe 0.02 V at C12.1, then the older 5.10 V capture named "C12.1". Also, `refuse_unconfirmed` joins notes with no separator (`:654-655`).
- **N35 (low): a reopened power check stays open when the gate opens through a user clearance** (`complete_power_checks` runs only in `record_measurement`, `:702-703`). Case: 5.10 V at C12.1, new confirmation, "VBUS" 0.01 V, `clear_residual_point C12.1`: gate open, `next_step` still `power_check`.
- N36 (low): `*GROUND*` also matches BACKGROUND and FOREGROUND (`bench_points.py:25`): "BACKGROUND_LED" is refused as a ground net. The name is refused, so the gate is not weakened.
- Low: naming a capture after a second bulk clear closes the gate again (errs on the safe side, `:604`); cosmetic texts for unknown points (`:602`, `:609`, `:811`, `:872`).

#### Local 7-segment decoder (`docs/briefs/sevenseg.md`)

73 sevenseg tests pass; with the multimeter, meter frame, and meter decimal tests: 191 passed; with the bench tests: 134 passed. The reviewer compared `HEAD` (`git archive` to a scratch folder) with the working tree on the same fake frames and fake vision answers, in off and compare mode, with the state in a temporary directory.

| Rule | Status | Evidence | Case / note |
|---|---|---|---|
| 1. Off mode: no local code runs, results unchanged | pass, with a schema note | The only call site is `server.py:869-871` (checks `LocalDecoderMode.COMPARE`); default `off` (`config.py:111`); a bad env value gives a validation error. The imports at `server.py:133-134` only define things (the state dir stayed empty). Test `test_sevenseg_compare.py:279` | `multimeter_read` and `bench_measure` equal `HEAD` except two new keys, `"local_reading": null` and `"local_agrees": null`, in the text JSON and in `structured_content`. The output schema has 2 new optional properties and 2 new `$defs`; the `required` list does not change (`multimeter.py:446-447`). Additive. |
| 2. Compare mode never changes the vision result, the confidence, the status, the bench gate, or `bench_measure` | pass | `server.py:868-871` (the gate call runs before `compare_local`), `sevenseg/compare.py:204` (`model_copy` of the two local fields only), `bench_state.py:840-857` (only vision fields), page `app.js:712` (only vision fields) | Field by field, off vs compare (without the 2 local fields, ids, and times): no difference. Cases: vision 5.10 V vs local 51.0 V (confirmed; same gate and notice); vision 0.51 vs local 51.0; vision frames 5.10/51.0 (disputed); vision confidence 0.3; vision kΩ vs local V; `bench_measure`. The vision requests and the bench state are the same too. |
| 3. Privacy and dataset | **fail (checked)** | Dataset in `<state>/sevenseg/dataset` (`sevenseg/profile.py:124-126`), at most 500 (`sevenseg/constants.py:86`), oldest removed by UUID v7 name (`sevenseg/dataset.py:315-319`); test `test_sevenseg_compare.py:180`. No image files in `git status`; the tests draw all images. | `sevenseg/compare.py:147-148` saves every frame, also when its size does not match the profile (the crop changed or was cleared). Case: a 1280x720 frame with a 420x300 profile: status `unreadable`, and the full 1280x720 JPEG was saved. With no `--webcam-crop`, up to 500 full webcam frames (they can show people) stay on disk. This disagrees with `sevenseg/dataset.py:3` ("the webcam crop only") and the AGENTS.md privacy rule. Fix: save only when the frame size equals the profile size and a crop is set. |
| 4. Failure isolation | pass | `sevenseg/compare.py:195-200` (any exception: the result unchanged), `:135-141` (no profile); tests `test_sevenseg_compare.py:151`, `:164` | Broken profile JSON, missing profile, a profile with a missing segment (KeyError), a decoder `RuntimeError`: the vision result is unchanged every time. |
| 5. Performance | pass | `sevenseg/compare.py:196` and `sevenseg/stability.py:297` run in a thread | 6-18 ms per frame; 13-33 ms for 2 frames in compare mode. |
| 6. Tests | pass, gaps | `test_sevenseg_decode.py` (digits, points, sign, OL, symbols, blur, noise, glare, JPEG q50, perspective), profile round trip (`sevenseg/calibrate.py:81`), toggle through the tool (`sevenseg/compare.py:261`) | No tool-level test with a local result that disagrees in the risky direction (local 51.0 vs vision 5.10); no test of `bench_measure` in compare mode or of an unchanged bench state. The reviewer's runs cover these cases, and they pass. |
| CLI (`debug-devices-sevenseg`) | pass | `sevenseg/cli.py`: files only at the given paths or in the state dir; no network code in `sevenseg/` | `calibrate` wrote only `profile.json` and `profile.annotated.png`; `evaluate` works on an empty and a filled dataset. |

New findings (decoder):

- **N37 (privacy, medium, checked): full webcam frames can go into the local dataset** (rule 3 above).
- N38 (low): the monitor log line says "agrees" when `local_agrees` is false (`sevenseg/compare.py:107-110`, `:140`). Case: no profile gives "local: unreadable (no_profile), agrees, 0 ms" with `local_agrees: false`.
- N39 (low): `sevenseg/dataset.py:304-319` writes the `.jpg` before the `.json`, and the prune finds entries by `*.json` only. If the JSON write fails, the `.jpg` stays and the 500 limit never removes it.
- N40 (low): `LcdLayout` (`sevenseg/profile.py:88-103`) does not check for 7 segments or for the number of decimal points; such a profile passes validation, then fails in `decode.py:312`; `local_reading` is then null with no log line, the same as off mode.
- N41 (low): `reading_key` (`sevenseg/stability.py:238-243`) does not include the mode, so an AC/DC change between frames is not "unstable"; one unreadable frame of 2 gives "unstable", not "unreadable" (`:257-258`).
- Low: the CLI prints raw tracebacks for a bad profile, a calibration error, or a missing file (`sevenseg/cli.py:49-92`); `stability.sample()` has no live caller (`:289`, also open in the dd-meter report); `docs/reports/dd-meter.md` open item 6 is out of date (AGENTS.md and `mcp/README.md` now describe the decoder).

#### Server, page, and Android (N27-N30, N21-N23, N11 text, crash fix)

`uv run pytest -q` on 12 test files of this area: 105 passed. The N27 and N28 cases ran in memory. The Android status comes from the code and its tests (the Gradle run in 1. passes).

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N27 | fixed | `pointer.py:94`, `:102-103`, `:168-170`, `:206-211` (no arrow; message "at the centre of the view"), `pointing.py:356`, `:408-413`; tests `test_qa_round10.py:45-67` | The round 10 case: no exception, no box, no arrow, the message. Cosmetic: the message text contains the QA id "(N27)" (`pointer.py:28`). |
| N28 | fixed | `pointer.py:93-94`; test `test_qa_round10.py:90` | Band `(200, 400, 600, 0)`, part 0.5 cm left of the centre: "~0.5 cm" at 180°. |
| N29 | fixed | `ui/monitor.py:654-659` (read-modify-write inside the lock), `ui/routes/settings.py:49`; tests `test_qa_round10.py:104`, `:117` | – |
| N30 | fixed for restarts, small gaps | `phone_screen.py:166`, `:267-275` (the selection is checked before each restart), `ui/setup.py:33-35`, `devices.py:109-111`; tests `test_qa_round10.py:134`, `:170` | (1) A running session is not stopped (the check is only at the loop top, `:268`): the secondary keeps streaming phone A until that stream ends, while its phone tools go to phone B. No new adb command goes to A in that time. (2) A small gap: the check runs before `detect_version` (`:302`) and the push (`:305`). adb re-audit: no other adb call site changed in this round. |
| N21 | fixed | `A/ApiServer.kt:32-55` (`ServerHost`: start and stop on one daemon thread, in call order), `A/MainActivity.kt:144`, `:206`; test `AT/ApiServerTest.kt:920` | – |
| N22 | fixed | `A/CameraController.kt:758` (rotation), `:719` (zoom), `:728` (torch), `:798-803`; test `AT/WaitsTest.kt:78` (helper only) | No test for zoom and torch. Side effect: see N43. |
| N23 | fixed | `A/CameraController.kt:750-756` (`Log.w` with tag `DebugCamera`), `A/Waits.kt:36`; test `AT/WaitsTest.kt:62` | The `null` of case (c) below writes no log line. |
| N11 text | partly | `docs/phone-api.md:81` matches the app (`ROTATION_LAYOUT_WAIT_MILLIS = 500`, 16 ms steps, `A/Constants.kt:95-96`); S22: the responses have the new region | `docs/phone-api.md:101` is unchanged: it allows `null` only "while the safe area is not measured yet (the window has no size)". Still `null` with a sized window: (b) a `GET /v1/status` during a relayout (a physical turn in auto mode, or during the rotation wait; `A/OverlayLayout.kt:480`, `A/CameraController.kt:526-529`); (c) a `POST /v1/rotation` while a relayout is already pending (the request does not wait, `A/Waits.kt:25-37`). |
| Crash fix (fast Back and restart) | fixed, low gaps | The crash: Android calls `onCreate` of the new activity before `onDestroy` of the old one, so the new server bound port 8765 while the old one still listened (`BindException` on a background thread, process crash). Fix: `ServerHost.start` stops the running server first (`A/ApiServer.kt:36-40`); `stop` acts only for the current server (`:42-47`), so a late `onDestroy` does nothing; one server per process (`:51`); Main never blocks; `singleTask` in the manifest. Tests `AT/ApiServerTest.kt:913`, `:954`, `:976` | (1) An exception in `startEngine` or `stopEngine` is not caught on the host thread, so Android kills the process (case: another app holds 127.0.0.1:8765). A try/catch with `Log.e` is missing. (2) The port reuse depends on the platform default of `SO_REUSEADDR`; Ktor CIO sets it only with `reuseAddress = true` (default false). It passed 6 of 6 on the device (dd-android report); set it explicitly. |

New findings (server, page, Android):

- N42 (low): `A/ApiServer.kt:94-95` rethrows every `CancellationException`, also one that does not come from a stop or a client cancel (for example a cancelled `await()`). Then there is no `Log.e`, and Ktor sends its default 500, not the contract `internal_error` JSON. Rethrow only when the call is no longer active.
- N43 (low): with N22, a change is applied before the 503. Case: `{"step": "in"}`, the app pauses during the zoom, the response is 503, and a retry after the resume zooms in again. `docs/phone-api.md:104` does not say that a 503 can follow an applied change.
- N44 (low): `except ValueError` in `pointing.py:356` and `:410` also catches pydantic `ValidationError` and numpy `LinAlgError` (subclasses). A programming error then becomes a warning on every frame, not a visible failure.
- Low: after `onDestroy`, the old server answers for up to the stop grace period (100-500 ms): the camera endpoints give 503 and health 200, so no harm. Clients can get a refused connection for about 0.6 s during a restart. No test covers the `setRotation` lambdas of the real controller (`A/CameraController.kt:748-758`).

### 4. Real phone

- Selected serial (`~/.local/state/debug-devices/ui-settings.json`, `adb_serial`): the Wi-Fi serial of the Samsung S22 (`SM_S901B`). `adb devices -l` listed only this device, in state `device`. I used the adb binary of the running adb server (`android-tools-37.0.0`).
- Separate forward `tcp:18793 tcp:8765` on the selected serial only. Health 200, status 200 (foreground, unlocked). The `app_start_id` is new since round 10 (the new APK).
- `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18793 --strict`: 29/29 (turned pixels, EXIF absent or 1, headers, `preview_region` and `overlay_region` rules).
- Rotation responses (N11): `{"degrees": 90}`, `{"degrees": 0}`, `{"auto": true}` each have the `overlay_region` of the new orientation, in 23-47 ms.
- After the run: `{"in_sensor_zoom": true}` again (the saved setting), rotation auto. 3 s later, every field (except `focus`) equalled the first status.
- I removed the forward. The MCP server forward (18765) and its scrcpy forward were there before my run and did not change. No command went to another device.
- Not tested on the phone: the crash fix (fast Back and restart) and `--after-start` / `--expect starting-race`, because they need an app restart or user input on the phone.

### Summary

- Tests: all pass (pytest 1045, boardview 13, Android 177, fake self-check 13 modes, contract 30/30 on the fake and 29/29 on the S22, MCP stdio 15/15). prek passes and leaves the tree unchanged (313 file hashes and the git status equal before and after).
- Fixed: N13 rest, N24, N25, N26, N16, N17, N21, N22, N23, N27, N28, N29, the crash fix (with two low gaps), and N30 for restarts.
- Local 7-segment decoder: off mode runs no local code and adds only two `null` fields (an additive schema change); compare mode never changes the vision result, the status, the confidence, the bench gate, or `bench_measure` (field-by-field comparison). Failure isolation and performance pass.
- Not fully fixed or new:
  - **N37 (privacy, medium, checked): the compare dataset can keep up to 500 full webcam frames** when the frame does not match the profile or no crop is set (`sevenseg/compare.py:147-148`).
  - N11 contract text (`docs/phone-api.md:101`): the `null` cases with a sized window (b) and (c).
  - N31 (checked): `bench-state.json.lock` is untracked in the repository root and not in `.gitignore`: `git add -A` would commit it.
  - Safety, low: N32 (a user-confirmed diode mode hides 0.5-3.0 V DC), N33 (an unsafe reading is lost after a 5 s lock wait).
  - Low: N30 gaps, N34-N36, N38-N44.

### What to do next

1. N37: save a dataset frame only when its size equals the profile size and a crop is set.
2. N31: add `bench-state.json.lock` to `.gitignore` (before the next `git add -A`).
3. N33: do not drop an unsafe reading when the lock wait times out (retry, or keep it in memory and save later); N32: count a DC V reading from the model even when the user mode is diode.
4. The N11 text in `docs/phone-api.md:101`, and the crash-fix gaps (catch start and stop errors; `reuseAddress = true`).
5. When the user allows it: the fast Back and restart test, `--after-start`, and `--expect starting-race` on the S22.

## Round 12: check

Date: 2026-09-28. Scope: `docs/briefs/qa-round11-followup.md` (all sections); dd-android-2 round 11 (`ServerHost` start and stop errors, bind check, `reuseAddress`); N42; N45 (a snapshot during a camera change: the new rule in `docs/phone-api.md` after "The app runs zoom and torch changes one at a time", `SNAPSHOT_READY_WAIT`, `Services.snapshot_when_ready`); dd-meter N40, N41, and the CLI errors. N45 was first called N44; the N44 of round 11 (`pointing.py`) is a different item. The working tree at the last commit ("feat: local 7-segment meter decoder (compare only) and QA fixes") with the uncommitted changes. All dd agents had stopped editing (dd-ally-tutor changes no repository files). I did not change product code or my scripts, and I did not commit.

### 1. Tests and hooks

| Command | Result |
|---|---|
| `uv run pytest -q -p no:cacheprovider` | 1077 passed, 1 skipped |
| `nix develop --command boardview/tests/run.sh` | 13 passed, 1 skipped |
| `nix develop --command prek run --all-files`, first on a copy (new files added in the copy only) | All 15 hooks pass; no file changed in the copy |
| The same in the real tree, with `sha256sum` of every file (315 files) and `git status --porcelain` before and after | All 15 hooks pass; hashes and git status unchanged. `bench-state.json.lock` is no longer in `git status` (N31). |
| `(cd android && nix develop .. --command ./gradlew --no-daemon assembleDebug testDebugUnitTest)` | BUILD SUCCESSFUL (`UP-TO-DATE`). Forced `testDebugUnitTest --rerun`: BUILD SUCCESSFUL, 187 tests in 16 suites, 0 failures. |

### 2. QA scripts

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | PASSED in 13 modes |
| `python3 scripts/fake_phone.py --port 18892`, then `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18892 --strict --after-start` | 30/30 |
| `uv run python scripts/qa_mcp_stdio.py --skip-webcam --scratch <dir>` | 15/15, with `fire-tv` |

The fake phone does not model N45 (a camera change never takes time in the fake), and `qa_contract.py` has no N45 check. I checked N45 by hand on the phone (section 4).

### 3. Re-check

Three read-only agents re-checked every item against the code and the contract text, and ran the cases in memory (temporary state dirs, synthetic images only). I checked the main findings in the code myself ("checked"). All changes are uncommitted. Paths are in `mcp/debug_devices_mcp/` unless they start with another directory; `A/` and `AT/` are the Android source and test directories. Round 11 used N44 for `pointing.py`; the snapshot item of this round is N45, and the new findings start at N46.

#### Privacy, decoder, and bench (N37, N40, N41, CLI errors, N31, N32, N33)

`uv run pytest -q mcp/tests/test_sevenseg_compare.py mcp/tests/test_sevenseg_calibrate.py mcp/tests/test_sevenseg_decode.py mcp/tests/test_bench_state.py`: 206 passed. The reviewer used a temporary state dir and synthetic images only.

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N37 (privacy) | fixed | `sevenseg/compare.py:119-121`, `:187-206`, `sevenseg/dataset.py:124-139`, `sevenseg/decode.py:387-403`, `server.py:885-889`; tests `test_sevenseg_compare.py:196-468` | A 1280x720 noise frame with a 420x300 profile and a crop: only 2 `.json` files, no image, a mismatch note. A matching crop: `.json` and `.lcd.png` of the warped LCD (440x200). No crop: `.json` only, with a note. The server prunes only `<state>/sevenseg/dataset` (`sevenseg/compare.py:209-216`). See N53 for the CLI. |
| N40 | fixed | `sevenseg/profile.py:105-116`, `:160-168`, `sevenseg/compare.py:162-168`; tests `test_sevenseg_compare.py:292-316`, `test_sevenseg_calibrate.py:131` | A missing segment and a wrong number of points give a clear error; in compare mode `no_profile` and the log line "not compared". |
| N41 | fixed | `sevenseg/stability.py:19-28`, `:48-58`; tests `test_sevenseg_compare.py:83-99` | DC then AC 5.10 V: `unstable`. One unreadable frame of 2: `unreadable`. |
| CLI errors | fixed | `sevenseg/cli.py:37-50`, `:185-190`; test `test_sevenseg_calibrate.py:131` | 19 bad inputs: one line on stderr, exit 1, no traceback, no profile file. |
| N31 | fixed | `.gitignore:24`; `git check-ignore -v bench-state.json.lock` gives `.gitignore:24` | – |
| N32 | partly | `bench_state.py:74`, `:283-314`, `:350-361`, `server.py:1263-1266`; tests `test_bench_state.py:1165-1189` | The brief case is fixed: user diode mode, model DC V "2.50 V" in 2 frames: an unsafe point and the note "... it counts as a voltage (fail safe)". Gaps (safety, low): (a) only the first frame's model mode counts (`bench_state.py:356`, `meter_frames.py:134`, `:253`): user diode, frame 1 model diode "0.62 V", frame 2 model DC V "0.62 V": no point, no notice. (b) With an unreadable unit, `bench_state.py:330` checks the user mode first: user diode, model DC V, no unit, "5.10" in 2 frames: `uncertain`, no point, no notice (without the user mode, a point is added). (c) Cosmetic: for a model AC V reading, the note says "DC V symbols" (`:308`). |
| N33 | fixed, one limit | `bench_state.py:437-441`, `:450-508`, `:537-541`, `:570-576`, `:844-862`; tests `test_bench_state.py:1221`, `:1247` | Another process held the lock for 7 s: the result said "NOT SAVED YET", `bench_state` showed the point as unsaved, and a background retry saved it at about 10.5 s. Limit: the kept reading is in memory only; nothing saves it at process exit, so it is lost if the server stops or reloads before a retry succeeds. |

New findings (decoder and bench):

- **N53 (low-medium, data loss, checked): `debug-devices-sevenseg evaluate --dataset <folder>` deletes files in any folder.** `Dataset.prune` (`sevenseg/dataset.py:146-160`) deletes every `*.jpg`, every `*.json` after the newest 500 by name, and every `*.lcd.png` without an entry, and `evaluate` runs it first (`sevenseg/cli.py:147-152`). Case: a folder with `holiday1.jpg` to `holiday3.jpg`: "removed 3 webcam frames of an older version", the files are gone, exit 0. A folder with 503 `.json` files: 3 are deleted. Fix: delete only files with a UUID v7 entry name, or prune only the default state dataset folder.
- N54 (low): a dataset write error (for example a full disk) raises inside `LocalMeter.compare` (`sevenseg/compare.py:177-178`), and `compare_local` then replaces a valid local reading with "the local decoder failed" (`:234-240`). The vision result does not change.
- Very low: `crop_set` is read after the capture (`server.py:889`); the saved image is still only the LCD area.
- Info: the git-ignored `bench-state.json` in the repository root got a new `updated_at` during the check; 4 `debug-devices-mcp` processes run. The review used only temporary stores.

#### Android (ServerHost, bind check, reuseAddress, N42, N45 app side)

The status comes from the code and its tests (the Gradle run in 1. passes) and the phone run in section 4.

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| ServerHost start and stop errors | fixed | `A/ApiServer.kt:74-85` (`tryStart` catches an `Exception`, logs, tries again every 2 s, `A/Constants.kt:15`), `:87-94` (`stopEngine` catches and logs), `:68-72` (a stop ends the tries); tests `AT/ApiServerTest.kt:1109`, `:1139`, `:1174` | "503 until the server runs" cannot work: no socket is bound, so a client gets a refused connection. The contract (`docs/phone-api.md:73`) does not say this. The level is `Log.w`. See N47. |
| Bind check | fixed | `A/ApiServer.kt:141-153` (a socket with `SO_REUSEADDR` binds and closes before Ktor starts; a new engine for each try) | Another process can take the port between the check and the Ktor bind (known, in the dd-android report). |
| `reuseAddress = true` | fixed | `A/ApiServer.kt:116-128` (the CIO configuration; the flag reaches the server socket in Ktor 3.6.0) | – |
| N42 | fixed | `A/ApiServer.kt:173-179` (rethrow only when the call is no longer active; else `Log.e` and 500 `internal_error` JSON); tests `AT/ApiServerTest.kt:972`, `:984` | – |
| N45 app side | fixed, with N46 | `A/ControlGate.kt:40-50` (the snapshot uses the lock of `control` and `start`; waits at most `SNAPSHOT_READY_WAIT_MILLIS` = 5000, `A/Constants.kt:152`; then 503 "camera change still running", `:189`), `A/CameraController.kt:767`; the rebind, in-sensor zoom, start state, zoom, and torch run under the gate (`:454-461`, `:495`, `:510`, `:711`, `:723`); the JPEG turn is outside the gate (`:797`); tests `AT/ControlGateTest.kt:85-136`, `AT/ApiServerTest.kt:623`, `:648`. S22: 8 of 8 snapshots during a change gave 200 (section 4). | No test of the real `CameraController.capture`. (a) A total rebind failure (both tries throw, `:238-258`) leaves the old camera object: later snapshots give 500 `capture_failed` and `/v1/status` stays 200, against "a new bind never makes a snapshot fail" (older than N45, rare). (b) A pause during a rebind: `restoreState` waits for OPEN with no limit (`:409`) and keeps the lock; snapshots get 503 "camera change still running" after 5 s, not the immediate 503 "not active"; other changes wait with no limit (the MCP HTTP timeout of 10 s ends first). (c) A pause during `takePicture` gives 500 `capture_failed`, not 503 (`capture` does not check the foreground again, unlike the N22 paths). |

New findings (Android):

- **N46 (checked; low probability, high effect): the snapshot wait can leak the camera lock.** `A/ControlGate.kt:42` runs `lock.lock()` inside `withTimeoutOrNull`. The kotlinx documentation says that the timeout can fire right before the block returns; then the lock is taken, `withTimeoutOrNull` returns `null`, the 503 is thrown, and nothing unlocks. After that, every camera change hangs with no limit and every snapshot gets 503, until the activity is created again. The window is very small. Fix: set a flag after `lock.lock()` inside the block and unlock when the flag is set but the result is `null` (or use an owner token with `holdsLock`). No test covers it.
- N47 (low): `ServerHost` does not catch an `Error` (for example `LinkageError` or `ExceptionInInitializerError` from Ktor): the executor wraps each task in a Future, so the error disappears with no log and no retry, and the server does not run (`A/ApiServer.kt:61-94`). Catch `Throwable`.
- N48 (low, text): `docs/phone-api.md:75` lists "the start state" among the changes that a snapshot waits for, but the code gives 503 at once while the start state runs (`A/ControlGate.kt:41`; `docs/phone-api.md:72` agrees with the code). The gate also covers focus, overlay, visibility, flips, and rotation (`A/CameraController.kt:531`, `:546`, `:636`, `:732`, `:743`), which the text does not list. "At most 5 s" is only the gate wait: a second snapshot first waits for the capture lock (`:763`) with no limit.
- N49 (low): with in-sensor zoom `on`, a rebind (bind, restore, a 4 s session check, `A/Constants.kt:86`, and maybe a fallback bind) takes more than 5 s, so a snapshot during it always gets 503 and the MCP retries. The S22 is `unsupported`, so the phone run did not reach this path.
- Cosmetic: the code and tests still call this item N44 (`A/ControlGate.kt:36`, `A/CameraController.kt:766`, `A/Constants.kt:151`, `AT/ControlGateTest.kt`, `AT/ApiServerTest.kt`, `server.py:632`, `mcp/tests/test_snapshot_retry.py:1`); the KDoc at `A/CameraController.kt:66` is out of date. Older than N45: a failed restore leaves the in-sensor zoom state and the label observers stale (`:210-212`, `:840`).

#### MCP server and page (N45 client side, the webcam port lookup)

`uv run pytest -q mcp/tests/test_snapshot_retry.py mcp/tests/test_remote_ports.py`: 9 passed. The reviewer ran the cases in memory with a private `XDG_RUNTIME_DIR` and `XDG_STATE_HOME`.

| Id | Status | Evidence | Remaining / case |
|---|---|---|---|
| N45 retry (`snapshot_when_ready`) | fixed | `server.py:630-641`; the only HTTP snapshot call is `server.py:637`; tests `test_snapshot_retry.py:43-61` | In memory: 503 two times, then 200: the still after 3 requests. |
| N45: which tools | fixed | `phone_snapshot` (`server.py:1072`, `:803`, `:652`), `bench_measure` (`:1325`), `multimeter_read` with the phone (`:783`, `:867`). The board tools take no photo. | Only `phone_snapshot` has a test. In memory, `bench_measure` and `multimeter_read` (phone, 2 frames) also pass after two 503. |
| N45: which 503 | partly (as the contract says) | `server.py:639` retries every 503 `camera_not_ready` (the contract, `docs/phone-api.md:75`). The status read before the snapshot (`server.py:649`) fails at once for an app in the background. | When the app leaves the foreground after the status read, the retry runs up to `app_start_timeout` (20 s). The error then has only the last message ("Camera is not active"); it does not say that the server retried, and gives no hint (bring the app to the front, `phone_connect`). |
| N45: timeout, poll, cancel | partly | `server.py:639-641`; a cancel stops the requests | The deadline is checked only after a response: the last request can start after the deadline and take the app's 5 s wait (worst case about 20 + 0.5 + 5 s). `wait_until_ready` uses a hard `asyncio.timeout` (`server.py:898`). |
| N45: turn and capture id after a retry | fixed | `server.py:653-658` (the headers of the successful still), `:822`, `:869`, `ui/monitor.py:437-446` | In memory: header 90 or 270 after two 503 gives turn 90 or 270. No test. |
| Webcam port lookup | fixed (webcam) | `remote_webcam.py:71-74`, `:82` (identity: app name and not this process), `:86-105`, `ui/forward.py:36-73` (`$XDG_RUNTIME_DIR/debug-devices`, the existing `ingest-<port>.token` names, mode 0600), `ui/monitor.py:815` (removed at stop); tests `test_remote_ports.py:63-121` | A stale token file after a crash: one request to a closed port, then skipped. A stale port that accepts but never answers makes each lookup wait up to 20 s (`webcam_timeout`). No test for stale files. |

New findings (MCP and page):

- **N50 (medium, checked): the primary-page lookup now also uses the other page ports.** `ui/setup.py:74-88` gives the webcam `RemoteMonitor` (with `other_ports`) to the monitor as `shared`, and `Monitor._find_primary` and `_other_monitor_runs` use it (`ui/monitor.py:762-776`); `ensure_page` acts on the result (`:740-746`).
  - Case 1 (in memory): a server with `--ui-port 40111` runs while the user's monitor is on 18766. It finds 18766 as the primary, so it serves no page, but its forwarder asks only 40111 and finds no primary: its tool calls show on no page. `monitor_open` and `bench_start` return the user's page URL and can open it (`ui/tools.py:92-93`, `:115-116`).
  - Case 2 (in memory; it touches the user's cockpit): the user's server on 18766 starts its page lazily while another debug-devices page runs on another port (a test server, `scripts/record_demo.py`, or a primary on a free port). It takes that page as the primary and serves no page on 18766, and its calls go nowhere.
  - This contradicts `mcp/README.md:266`. Fix: the monitor gets its own `RemoteMonitor` for its own port only (as `ui/setup.py:122`, `:126`), and a test.
- **N51 (privacy, low): the user's server can now take frames from any debug-devices monitor on a page port, with that monitor's crop** (`remote_webcam.py:157-159`, `ui/monitor.py:116`). Case: a test server with a temporary state dir and no crop owns `/dev/video0`; the user's `multimeter_read` then sends the whole webcam frame to OpenRouter. Before this change, the user's server failed with "busy".
- N52 (low): with the retry, the `bench_measure` photo can come about 25 s after the meter frames. The result gives only `gap_seconds` (`server.py:1333`), and `BENCH_MEASURE_NOTE` (`:1296`) still says that the value and the photo "belong together"; no limit or warning.
- Cosmetic: `RemoteMonitor.base_url` keeps the last port that it found after that monitor is gone (`remote_webcam.py:94-95`); the dd-ui report and `mcp/README.md:259` say that only the webcam sharing uses the other ports (see N50).

### 4. Real phone

- Selected serial (`~/.local/state/debug-devices/ui-settings.json`, `adb_serial`): the Wi-Fi serial of the Samsung S22 (`SM_S901B`). `adb devices -l` listed only this device, in state `device`. I used the adb binary of the running adb server (`android-tools-37.0.0`).
- Separate forward `tcp:18794 tcp:8765` on the selected serial only. Health 200, status 200 (foreground, unlocked). The `app_start_id` is new since round 11 (the new APK).
- `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18794 --strict`: 29/29.
- N45 by hand: 8 times, a `POST /v1/camera` in the background and `GET /v1/snapshot` 20 ms later (bodies: `in_sensor_zoom` false/true two times each, `af_mode` macro/continuous, and both fields together two times):

  | # | POST body | POST | Snapshot |
  |---|---|---|---|
  | 1 | `{"in_sensor_zoom": false}` | 200, 0.05 s | 200, 1.91 s |
  | 2 | `{"in_sensor_zoom": true}` | 200 (`unsupported`), 1.15 s | 200, 3.23 s |
  | 3 | `{"in_sensor_zoom": false}` | 200, 0.94 s | 200, 3.78 s |
  | 4 | `{"in_sensor_zoom": true}` | 200 (`unsupported`), 2.20 s | 200, 1.91 s |
  | 5 | `{"af_mode": "macro"}` | 200 (stays `continuous`: no macro mode), 0.07 s | 200, 1.84 s |
  | 6 | `{"af_mode": "continuous"}` | 200, 1.32 s | 200, 1.95 s |
  | 7 | `{"in_sensor_zoom": false, "af_mode": "macro"}` | 200, 1.03 s | 200, 3.08 s |
  | 8 | `{"in_sensor_zoom": true, "af_mode": "continuous"}` | 200 (`unsupported`), 2.23 s | 200, 2.00 s |

  Every snapshot returned 200; none returned 500. The times show that the snapshot and the change run one at a time (a snapshot during a rebind took 3.1-3.8 s; a change after a running snapshot took 2.2 s). No 503 occurred, because every change ended within the 5 s `SNAPSHOT_READY_WAIT`. The 503 path was not reached on the phone.
- The last request put back the saved settings (`in_sensor_zoom` true, which gives `unsupported`; `af_mode` continuous). 3 s later, every field (except `focus`) equalled the first status.
- I removed the forward. Only the MCP server forward (18765) remains. No command went to another device.
- Not tested on the phone: a `ServerHost` start error (it needs another app on port 8765), the fast Back and restart, `--after-start`, and `--expect starting-race`.

### Summary

- Tests: all pass (pytest 1077, boardview 13, Android 187, fake self-check 13 modes, contract 30/30 on the fake and 29/29 on the S22, MCP stdio 15/15). prek passes and leaves the tree unchanged (315 file hashes and the git status equal before and after).
- Fixed: N37 (privacy), N40, N41, the CLI errors, N31, N33 (with a limit), the `ServerHost` errors, the bind check, `reuseAddress`, N42, N45 on the app and the client side (S22: 8 of 8 snapshots during a camera change gave 200), and the webcam port lookup.
- Not fully fixed: N32 (two gaps: a later frame's DC V and an unreadable unit), the N45 retry texts and soft deadline, and the stale-port-file wait.
- New:
  - **N50 (medium, checked): the primary-page lookup also uses other page ports**, so the user's server can serve no page, and a test server's calls go nowhere (`ui/setup.py:74-88`, `ui/monitor.py:762-776`).
  - **N46 (checked, low probability, high effect): the snapshot wait can leak the camera lock** (`A/ControlGate.kt:42`).
  - **N53 (checked): `debug-devices-sevenseg evaluate --dataset` deletes `*.jpg` files in any folder.**
  - N51 (privacy, low): frames of another monitor, with its crop (or none), can go to OpenRouter.
  - Low: N47, N48, N49, N52, N54.

### What to do next

1. N50: the monitor uses a `RemoteMonitor` for its own port only; add a test.
2. N46: release the lock when the timeout fires after `lock.lock()`; add a test.
3. N53: prune only UUID v7 entry files, or only in the default state dataset folder.
4. N32 gaps (a) and (b), N51 (take remote frames only from the monitor on the default port, or require a crop), N33 limit (save at exit).
5. Contract text: N48 (the snapshot and the start state; the list of gated changes), and whether a server that cannot bind is "refused" (not 503).
6. On a phone with in-sensor zoom: the N45 503 path (N49).
