# QA test plan

This plan tests the phone app (`android/`), the MCP server (`mcp/`), and the contract between them (`docs/phone-api.md`).

## Safety

- The PC can have other ADB devices. Two Amazon Fire TV devices (model `AFTR`) are on the network. They are not test targets.
- Do not install, start, or send input to a device that is not the test phone.
- The target phone is adb serial `0a1b2c3d` (model `2510ERA8BG`). Use only `adb -s 0a1b2c3d`.
- Always set `DEBUG_DEVICES_ADB_SERIAL=0a1b2c3d` (or `--adb-serial 0a1b2c3d`) when the real adb is in use.
- For tests without a phone, use `scripts/fake_adb.py`. It never calls the real adb.
- Do not put a real `OPENROUTER_API_KEY` in a test. Tests mock the OpenRouter API.

## Tools

| File | Purpose |
|---|---|
| `scripts/fake_phone.py` | HTTP server that implements `docs/phone-api.md` with state in memory. Standard library only. |
| `scripts/fake_adb.py` | Fake `adb` with one device (`fake-phone-0001`). It accepts `devices`, `forward`, and `shell am start`. |
| `scripts/qa_contract.py` | Contract checks against a base URL: the fake phone or the real phone through `adb forward`. |
| `scripts/qa_mcp_stdio.py` | MCP tool tests over stdio with the fake phone, the fake adb, and a mock OpenRouter. Run it with `uv run`. |
| `scripts/multimeter_capture.sh` | Captures webcam frames and writes a CSV for the multimeter accuracy check. |

`fake_phone.py` modes:

| Flag | Result |
|---|---|
| none | Zoom `[1.0, 8.0]`, flash unit, 64x48 test JPEG |
| `--min-zoom X --max-zoom Y` | Other zoom range |
| `--no-flash` | `POST /v1/torch` returns 409 `no_flash_unit` |
| `--not-ready` | All camera endpoints return 503 `camera_not_ready`. `/v1/health` returns 200. |
| `--capture-fails` | `GET /v1/snapshot` returns 500 `capture_failed` |
| `--background` | The app is in the background: all camera endpoints return 503 `camera_not_ready` |
| `--physical-rotation N` | The phone orientation (0, 90, 180, 270). The auto rotation follows it. |
| `--internal-error` | All camera endpoints return 500 `internal_error` |
| `--start-delay S` | For `S` seconds, the camera endpoints return 503 `camera_not_ready`. Then the start state applies. |
| `--snapshot FILE` | Serve `FILE` as the snapshot, for example a real webcam frame |
| `--self-check` | Start the server in each mode and run `qa_contract.py` against it |

## 1. Contract tests

### 1.1 Self-check of the tools

Run:

```sh
python3 scripts/fake_phone.py --self-check
```

Expected result: `self-check PASSED`. This proves that the checks and the fake agree with the contract.

### 1.2 Checks in `qa_contract.py`

| Check | What it checks |
|---|---|
| `health` | 200, `ok` is `true`, `app_version` is a string that is not empty |
| `status` | 200, `application/json`, exactly the 5 `CameraStatus` keys with the correct types, `0 < min <= zoom <= max` |
| `zoom_ratio` | `ratio` = min, middle, max gives that ratio. `GET /v1/status` shows the same value. |
| `zoom_clamp` | `ratio` above max gives max. `ratio` below min, 0, or negative gives min. |
| `zoom_step` | `in` from min gives `min * 1.5` (clamped). `out` divides by 1.5. `out` at min stays at min. |
| `zoom_step_to_max` | Repeated `in` follows `x * 1.5` to max. `in` at max stays at max. |
| `zoom_bad_request` | Bad bodies give 400 `bad_request`, and the zoom does not change |
| `torch` | With a flash unit: on, then off, and `GET /v1/status` agrees. Without one: 409 `no_flash_unit`. |
| `torch_bad_request` | Missing or non-bool `enabled` gives 400 `bad_request` |
| `snapshot` | 200, `image/jpeg`, SOI and EOI markers, a SOF segment. It prints the width and height. |
| `snapshot_keeps_torch` | With a flash unit: torch on, snapshot, torch still on. Then the same with the torch off. |
| `rotation` | Lock 0, 90, 180, 270: each gives that `rotation_degrees` with `rotation_locked: true`, and `GET /v1/status` agrees. `{"auto": true}` gives `rotation_locked: false`. |
| `rotation_bad_request` | Bad rotation bodies give 400 `bad_request`: 45, `"90"`, `true`, 90.5, `auto: false`, `auto: "true"`, both fields, neither |
| `post_needs_json_content_type` | A POST to zoom, torch, or rotation without `Content-Type: application/json` (none, or `text/plain`) gives 400, and nothing changes |
| `snapshot_rotation` | Lock each rotation and take a snapshot. The displayed size (pixel size turned by the EXIF orientation) of 90 and 270 is the size of 0 and 180 with width and height swapped. |
| `concurrent_zoom` | 8 zoom requests at the same time. Each gets 200 with its own ratio. No request cancels another. |
| `method_not_allowed` | A known path with a wrong method gives 405 `method_not_allowed`: `GET /v1/zoom`, `GET /v1/torch`, `POST /v1/status`, `POST /v1/health`, `POST /v1/snapshot` |
| `not_found` | Unknown path gives 404 `not_found` for `GET` and `POST` |

Every error response must be an `ApiError`: exactly the keys `error` and `message`, a known code, and the HTTP status for that code.

The script restores the zoom ratio and the torch state that it found at the start.

`--strict` adds checks for cases that the contract does not state:

- A zoom body with `"ratio": 1e400` (overflow) or `"step": "IN"` (wrong case) gives 400 `bad_request`.
- `PUT /v1/zoom` and `DELETE /v1/status` give 405 `method_not_allowed`.
- Rotation bodies `-90`, `360`, `"degrees": null`, and `"auto": null` give 400 `bad_request`.
- `POST /v1/focus` on the screen edges (`screen_x` 0.5, `screen_y` 0 and 1): 200 when the preview covers the edge, else 400 `bad_request` with a message that contains "outside the preview". The app layout decides which one, so the script notes a 200.

`--expect starting` is for a run right after `am start`. It polls `/v1/status`. Every answer must be 503 `camera_not_ready` until the first 200. The first 200 must show the start state. Then the normal checks run.

`--expect background` is for the app in the background (press HOME on the phone). Health gives 200, and every camera endpoint gives 503 `camera_not_ready`.

The start state now also has `rotation_locked: false` (auto). `--after-start` and `--expect starting` check it.

`--expect starting-race` is also for a run right after `am start`. It sends `POST /v1/zoom {"ratio": 3}` until it gets 200. Every earlier answer must be 503 `camera_not_ready`. Then the app must answer health and status for 3 s. A connection error in this time means a crash. This is the check for bug 1 of round 1.

`--after-start` adds one check before the others: the torch is off and the zoom is at min. Use it only right after an app start.

`--expect not-ready` checks a camera that is not bound: health 200, and 503 `camera_not_ready` on the other endpoints.

### 1.3 Contract test of the real phone

1. Connect the phone with USB. The target phone is serial `0a1b2c3d`, model `2510ERA8BG`.
2. Set `SERIAL=0a1b2c3d`. Run `adb -s "$SERIAL" shell getprop ro.product.model` and make sure that it prints `2510ERA8BG`.
3. Install the app: `adb -s "$SERIAL" install -r android/app/build/outputs/apk/debug/app-debug.apk`.
4. Start the app: `adb -s "$SERIAL" shell am start -n dev.jayson.debugdevices.camera/.MainActivity`.
5. Forward the port: `adb -s "$SERIAL" forward tcp:18765 tcp:8765`.
6. Wait about 5 s. The app sets the start state (zoom at min, torch off) after the camera opens.
7. Run: `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict --after-start`.
   Alternative without the wait: run `am start`, then at once `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict --expect starting`.
   For the start race: run `am start`, then at once `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict --expect starting-race`.
8. Record the output in `docs/reports/dd-qa.md`.

Expected result: all checks pass. The torch check turns the torch on and then off.

### 1.4 Contract tests on the app side (`./gradlew`)

The Android unit tests must cover the same rules without a device:

- Zoom step math: `in` multiplies by `ZOOM_STEP_FACTOR = 1.5`, `out` divides, the result is clamped.
- Explicit ratio clamp.
- Request parsing: each bad body in `BAD_ZOOM_BODIES` and `BAD_TORCH_BODIES` of `qa_contract.py` gives `bad_request`.
- JSON names of `CameraStatus` and `ApiError` match the contract (snake case).
- Each error code maps to its HTTP status.
- Unknown path gives 404 `not_found`.

Run:

```sh
cd android && nix develop .. --command ./gradlew :app:testDebugUnitTest --rerun
```

### 1.5 Contract tests on the MCP side (`pytest`)

The MCP unit tests must cover:

- `CameraStatus` and `ApiError` parse from the contract examples.
- A non-2xx response with an `ApiError` body becomes a clear error for the tool caller. The error must include the code.
- A non-2xx response with a body that is not JSON also gives a clear error.
- The snapshot response is returned as JPEG bytes.

Run: `uv run pytest` and `uv run ruff check`.

## 2. MCP tool tests over stdio

### 2.1 Set-up with the fake phone

The MCP forwards `tcp:<local_forward_port>` to the phone. The default local port is `18765`. With `fake_adb.py`, the forward does nothing. Thus the fake phone must listen on the local forward port.

```sh
python3 scripts/fake_phone.py --port 18765 --snapshot /tmp/dd-qa/frame_1080p.jpg &
export DEBUG_DEVICES_ADB_PATH="$PWD/scripts/fake_adb.py"
export FAKE_ADB_LOG=/tmp/dd-qa/fake_adb.log
```

### 2.2 Automatic run

```sh
uv run python scripts/qa_mcp_stdio.py --snapshot /tmp/dd-qa/frame_1080p.jpg --real-adb-several-devices
```

The script starts the MCP with `--no-ui` (no monitor window, no scrcpy, no shared webcam stream). The webcam must be free: another MCP process with its UI on holds `/dev/video0`. The script starts the fake phone, the mock OpenRouter, and one MCP server process for each group of cases. It replaces `OPENROUTER_API_KEY` with a dummy value. It never sends a request to OpenRouter. `--real-adb-several-devices` runs `phone_connect` with the real adb and no serial. Only `adb devices -l` runs, and the server must refuse.

### 2.3 Manual driver

Use the Python `mcp` client (`mcp.client.stdio.stdio_client`) through `uv run`. The MCP Inspector CLI is an alternative:

```sh
npx @modelcontextprotocol/inspector --cli uv run debug-devices-mcp --method tools/list
npx @modelcontextprotocol/inspector --cli uv run debug-devices-mcp --method tools/call --tool-name <tool>
```

### 2.4 Cases

| Case | Set-up | Expected result |
|---|---|---|
| List tools | none | Each tool has a name, a description, and an input schema |
| Status | fake phone default | The tool returns the 5 `CameraStatus` values |
| Zoom in, zoom out | fake phone default | The ratio changes by 1.5, and the clamp applies at min and max |
| Zoom ratio | ratio 100 | The tool returns max, not an error |
| Torch on, off | fake phone default | `torch_enabled` changes |
| Torch without flash | `--no-flash` | A tool error with `no_flash_unit`. The server does not crash. |
| Snapshot | `--snapshot <frame>` | Image content with `mimeType` `image/jpeg` |
| Camera not ready | `--not-ready` | A tool error with `camera_not_ready` |
| Capture fails | `--capture-fails` | A tool error with `capture_failed` |
| Phone down | fake phone stopped | A tool error that says that the phone is not reachable. The server does not crash. |
| ADB forward | any | `FAKE_ADB_LOG` shows `forward tcp:18765 tcp:8765` with the serial `fake-phone-0001` |
| Several ADB devices | real adb, no serial set | A tool error that asks for `--adb-serial`. No command goes to a device. |
| Webcam frame | `/dev/video0` | Image content, 1920x1080 JPEG |
| Webcam busy or missing | `--webcam /dev/video9` | A tool error with the device path |
| Multimeter, no key | `OPENROUTER_API_KEY` empty | A tool error that names the variable. No network call. |
| Multimeter, mocked API | mock OpenRouter base URL | The tool returns value, unit, mode, and range from the mock |
| Multimeter, request shape | mock OpenRouter base URL | The mock gets `model` = `openai/gpt-6-luna` (the default), one image as a JPEG data URL, and `response_format` of type `json_schema` with `strict` set to `true` |
| Multimeter, other model | `--vision-model <id>` | The mock gets that model id |
| Multimeter, bad model output | mock returns text that is not JSON | A tool error. The server does not crash. |

For the mocked API, set `--openrouter-base-url` to a local HTTP server. The mock returns a fixed chat completion.

### 2.5 Stdout hygiene

The stdio transport uses stdout for JSON-RPC. Logs must go to stderr. Run the server and send `tools/list`. Every stdout line must be JSON.

## 3. Manual phone tests

Do these steps on the real phone after the contract test passes.

1. Start the app. Check that the preview shows the back camera and that the screen stays on.
2. Call zoom `in` 3 times. Check that the preview zooms each time.
3. Call zoom `out` until the ratio is at min. Check that the preview is at full width.
4. Turn the torch on. Check that the LED is on. Turn it off. Check that the LED is off.
5. Take a snapshot. Open the JPEG. Check that it is sharp, that it has the full sensor resolution, and that it has the correct orientation.
6. Take a snapshot with zoom at 4x. Check that the JPEG shows the zoomed view.
7. Put the app in the background. Call status. Record the result: 503 `camera_not_ready` or a working camera.
8. Lock the screen, then unlock it. Call status and snapshot again.
9. Turn off the camera permission. Start the app. Check that the app shows the problem and that the API returns 503.
10. Kill the app. Start it again with `am start`. Check that the torch is off and the zoom is at min.
11. Check that the server does not accept a connection on the Wi-Fi address of the phone: `curl http://<phone-ip>:8765/v1/health` must fail.
12. Take 10 snapshots in a row. Record the time of each. Check that no snapshot fails.

## 4. Multimeter accuracy check

### 4.1 Set-up

- Put the multimeter in front of the webcam. The display must fill a large part of the frame, and it must face the camera.
- Avoid glare on the LCD. Use even light.
- Webcam formats that work (`v4l2-ctl -d /dev/video0 --list-formats-ext`): MJPEG up to 1920x1080 at 30 fps, YUYV 1920x1080 at 5 fps only. Use MJPEG 1920x1080.

### 4.2 Procedure

1. Run `scripts/multimeter_capture.sh /tmp/dd-qa/accuracy 20`.
2. Before each capture, set a different state on the meter. Use this mix:
   - 5 DC voltage values (for example a 1.5 V cell, a 9 V cell, a USB 5 V line, a bench supply at 3.3 V and 12 V).
   - 3 AC voltage values, if a safe source is available.
   - 4 resistance values (for example 100 Ω, 4.7 kΩ, 1 MΩ, and an open circuit with `OL`).
   - 2 continuity or diode values.
   - 2 current values in mA, if a safe circuit is available.
   - 2 values with a changing reading.
   - 2 frames with the meter off or partly out of the frame.
3. A human reads the display in each frame and fills `readings.csv`: value, unit, mode, range.
4. Run the multimeter tool on each frame with the default model `openai/gpt-6-luna`. Record the model output next to the human values.
5. If a target fails, run the same frames with one other vision model. Record both results.

### 4.3 Metrics and targets

| Metric | Target |
|---|---|
| Exact value match (all digits, sign, decimal point) | 18 of 20 or more |
| Unit match (V, mV, A, mA, Ω, kΩ, MΩ, Hz, F) | 20 of 20 |
| Mode match (DC V, AC V, Ω, continuity, diode, A) | 19 of 20 or more |
| `OL` and blank display reported as such, not as a number | all |
| Frames with a bad view give low confidence or an error, not a wrong number | all |
| Median time per reading | record it |

A wrong unit or a wrong decimal point is a severe failure. It gives a wrong value by a factor of 1000.

## 5. Reports

Write the results and the bugs in `docs/reports/dd-qa.md`. For each bug, give the file and line, the steps, the expected result, and the actual result.
