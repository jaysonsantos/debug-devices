# Phone camera HTTP API (contract)

The Android app (`android/`) and the MCP server (`mcp/`) share this contract.
Change this file first, then change both sides.

## Transport

- The app runs an HTTP/1.1 server on the phone.
- The server binds to `127.0.0.1` only, port `8765`.
- The MCP server reaches it through ADB: `adb -s <serial> forward tcp:<local> tcp:8765`.
- All bodies are JSON (`application/json`), except the snapshot (`image/jpeg`).
- A POST body must have `Content-Type: application/json`. Without it, the app returns 400 `bad_request`.
- The camera is the back camera. The app keeps the screen on while it runs.

## Endpoints

| Method | Path | Request body | Response |
|---|---|---|---|
| GET | `/v1/health` | none | `{"ok": true, "app_version": "0.1.0"}` |
| GET | `/v1/status` | none | `CameraStatus` |
| POST | `/v1/zoom` | `{"ratio": 2.5}` or `{"step": "in" \| "out"}` | `CameraStatus` |
| POST | `/v1/torch` | `{"enabled": true}` | `CameraStatus` |
| POST | `/v1/rotation` | `{"degrees": 0 \| 90 \| 180 \| 270}` or `{"auto": true}` | `CameraStatus` |
| POST | `/v1/preview` | `{"flip_horizontal": true, "flip_vertical": false}` | `CameraStatus` |
| POST | `/v1/camera` | `{"in_sensor_zoom": true}` | `CameraStatus` |
| GET | `/v1/snapshot` | none | `image/jpeg` bytes of one full still capture |

`CameraStatus`:

```json
{
  "zoom_ratio": 1.0,
  "min_zoom_ratio": 1.0,
  "max_zoom_ratio": 8.0,
  "torch_enabled": false,
  "has_flash_unit": true,
  "rotation_degrees": 0,
  "rotation_locked": false,
  "preview_flip_horizontal": false,
  "preview_flip_vertical": false,
  "focus": {
    "distance_diopters": 3.41,
    "state": "focused",
    "calibration": "approximate",
    "min_distance_diopters": 10.0
  },
  "optics": {
    "focal_length_mm": 6.07,
    "sensor_width_mm": 9.14,
    "output_width_px": 4080
  },
  "in_sensor_zoom": "off"
}
```

Rules:

- `step: "in"` multiplies the ratio by `ZOOM_STEP_FACTOR = 1.5`, `"out"` divides it. The app clamps the result to `[min, max]`.
- An explicit `ratio` outside `[min, max]` is clamped, not refused.
- `torch` on a phone without a flash unit returns 409 with an `ApiError`.
- A zoom body with both `ratio` and `step`, or with neither, returns 400 `bad_request`.
- A known path with a wrong method (for example `GET /v1/zoom`) returns 405 `method_not_allowed`.
- `/v1/health` returns 200 while the camera is not bound yet. The other camera endpoints return 503 `camera_not_ready` until the camera is bound AND the start state (torch off, zoom at min) is set.
- The HTTP server needs a few seconds after `am start`. The client retries `/v1/health`, then `/v1/status` while it gets 503, until its start timeout ends.
- The app runs zoom and torch changes one at a time. A request never cancels another request.
- `ratio` must be a JSON number. A string (also `"2"`) returns 400 `bad_request`.
- After an app start, the torch is off and the zoom is at `min_zoom_ratio`.
- `rotation_degrees` is the rotation of the next snapshot (0, 90, 180, 270). With `rotation_locked: false`, the app follows the physical orientation of the phone. When the phone lies flat (no angle), the app keeps the last value.
- `POST /v1/rotation {"degrees": N}` locks the snapshot rotation to N. `{"auto": true}` goes back to the physical orientation. Other values, or both fields, or neither, return 400 `bad_request`. After an app start, the rotation is auto.
- `POST /v1/preview` mirrors only the camera preview on the phone screen: `flip_horizontal` left-right, `flip_vertical` upside down. The status label and the other on-screen text stay readable (not mirrored). Both fields are required booleans; a missing field, another type, or an unknown field returns 400 `bad_request`. It does not change `/v1/snapshot`: the snapshot stays in the true orientation, and the MCP server applies its own flips to the snapshots. After an app start, both preview flips are false; the MCP server sends them again after `phone_connect` and after an app start.
- `focus` comes from the latest preview capture result of the back camera. `distance_diopters` is `LENS_FOCUS_DISTANCE` (1/m; 0 means infinity), `null` until the first result or when the lens has fixed focus. `state` is the autofocus state: `focused`, `scanning`, `unfocused`, or `unknown`. `calibration` is `LENS_INFO_FOCUS_DISTANCE_CALIBRATION`: `uncalibrated`, `approximate`, or `calibrated` (with `uncalibrated`, the distance is not in real units: clients must not show cm). `min_distance_diopters` is `LENS_INFO_MINIMUM_FOCUS_DISTANCE` (0 = fixed focus). The `focus` object can be `null` before the camera is bound.
- `optics` describes the camera of `/v1/snapshot`: the first focal length, the physical sensor width (`SENSOR_INFO_PHYSICAL_SIZE`), and the snapshot width in pixels before rotation. Clients compute the distance in cm (100 / diopters) and the detail in px/mm (`output_width_px * focal_length_mm / (sensor_width_mm * distance_mm)`, thin-lens estimate). Zoom does not change this detail value: zoom crops.
- `POST /v1/camera {"in_sensor_zoom": true|false}` turns the vendor in-sensor zoom on or off. The body field is a required boolean (400 `bad_request` otherwise). The app binds the camera again: the preview stops for about 1 s, and the zoom and the torch stay as they were. `CameraStatus.in_sensor_zoom` is `off`, `on`, `unsupported` (the phone has no such vendor mode; the request still returns 200 with this value), or `fallback` (the vendor session failed; the app runs in the normal mode). After an app start, it is `off`.
- With `in_sensor_zoom` `on`, a zoom at 2x or more can give real extra detail (a sensor crop at full density, not optics). The detail estimate in `optics` does not include this gain.
- The camera endpoints return 503 `camera_not_ready` while the app is not in the foreground.
- `/v1/snapshot` does not fire the flash. The torch state after a snapshot is the same as before it.

`ApiError` (every non-2xx response):

```json
{"error": "camera_not_ready", "message": "Camera is not bound yet"}
```

Error codes: `camera_not_ready` (503), `no_flash_unit` (409), `bad_request` (400), `not_found` (404), `method_not_allowed` (405), `capture_failed` (500), `internal_error` (500, an unexpected error; the app logs the stack trace).

## App launch

- Package: `dev.jayson.debugdevices.camera`
- Activity: `.MainActivity`
- Start from the PC: `adb -s <serial> shell am start -n dev.jayson.debugdevices.camera/.MainActivity`
