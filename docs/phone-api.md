# Phone camera HTTP API (contract)

The Android app (`android/`) and the MCP server (`mcp/`) share this contract.
Change this file first, then change both sides.

## Transport

- The app runs an HTTP/1.1 server on the phone.
- The server binds to `127.0.0.1` only, port `8765`.
- The MCP server reaches it through ADB: `adb -s <serial> forward tcp:<local> tcp:8765`.
- All bodies are JSON (`application/json`), except the snapshot (`image/jpeg`).
- The camera is the back camera. The app keeps the screen on while it runs.

## Endpoints

| Method | Path | Request body | Response |
|---|---|---|---|
| GET | `/v1/health` | none | `{"ok": true, "app_version": "0.1.0"}` |
| GET | `/v1/status` | none | `CameraStatus` |
| POST | `/v1/zoom` | `{"ratio": 2.5}` or `{"step": "in" \| "out"}` | `CameraStatus` |
| POST | `/v1/torch` | `{"enabled": true}` | `CameraStatus` |
| GET | `/v1/snapshot` | none | `image/jpeg` bytes of one full still capture |

`CameraStatus`:

```json
{
  "zoom_ratio": 1.0,
  "min_zoom_ratio": 1.0,
  "max_zoom_ratio": 8.0,
  "torch_enabled": false,
  "has_flash_unit": true
}
```

Rules:

- `step: "in"` multiplies the ratio by `ZOOM_STEP_FACTOR = 1.5`, `"out"` divides it. The app clamps the result to `[min, max]`.
- An explicit `ratio` outside `[min, max]` is clamped, not refused.
- `torch` on a phone without a flash unit returns 409 with an `ApiError`.
- A zoom body with both `ratio` and `step`, or with neither, returns 400 `bad_request`.
- A known path with a wrong method (for example `GET /v1/zoom`) returns 405 `method_not_allowed`.
- `/v1/health` returns 200 while the camera is not bound yet. The other camera endpoints return 503 `camera_not_ready` until then.
- After an app start, the torch is off and the zoom is at `min_zoom_ratio`.
- `/v1/snapshot` does not fire the flash. The torch state after a snapshot is the same as before it.

`ApiError` (every non-2xx response):

```json
{"error": "camera_not_ready", "message": "Camera is not bound yet"}
```

Error codes: `camera_not_ready` (503), `no_flash_unit` (409), `bad_request` (400), `not_found` (404), `method_not_allowed` (405), `capture_failed` (500).

## App launch

- Package: `dev.jayson.debugdevices.camera`
- Activity: `.MainActivity`
- Start from the PC: `adb -s <serial> shell am start -n dev.jayson.debugdevices.camera/.MainActivity`
