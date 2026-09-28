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
| POST | `/v1/camera` | `{"in_sensor_zoom": true}` and/or `{"af_mode": "macro"}` | `CameraStatus` |
| POST | `/v1/focus` | `{"screen_x": 0.4, "screen_y": 0.6}` or `{"snapshot_x": 0.4, "snapshot_y": 0.6}` | `CameraStatus` |
| POST | `/v1/overlay` | `{"boxes": [{"snapshot_x": 0.42, "snapshot_y": 0.31, "width": 0.05, "height": 0.04, "label": "U730"}]}` or `{"boxes": []}` | `CameraStatus` |
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
  "in_sensor_zoom": "off",
  "overlay_boxes": 0,
  "overlay_arrows": 0,
  "af_mode": "continuous",
  "app_start_id": "0192f3a4-5b6c-7d8e-9f00-112233445566",
  "preview_region": {"snapshot_x": 0.2, "snapshot_y": 0.0, "width": 0.6, "height": 1.0},
  "overlay_visible": true,
  "overlay_region": {"snapshot_x": 0.2, "snapshot_y": 0.04, "width": 0.6, "height": 0.9}
}
```

Rules:

- `step: "in"` multiplies the ratio by `ZOOM_STEP_FACTOR = 1.5`, `"out"` divides it. The app clamps the result to `[min, max]`.
- An explicit `ratio` outside `[min, max]` is clamped, not refused.
- `torch` on a phone without a flash unit returns 409 with an `ApiError`.
- A zoom body with both `ratio` and `step`, or with neither, returns 400 `bad_request`.
- A known path with a wrong method (for example `GET /v1/zoom`) returns 405 `method_not_allowed`.
- `/v1/health` returns 200 while the camera is not bound yet. The other camera endpoints return 503 `camera_not_ready` until the camera is bound AND the start state (torch off, zoom at the start zoom) is set.
- The HTTP server needs a few seconds after `am start`. The client retries `/v1/health`, then `/v1/status` while it gets 503, until its start timeout ends.
- The app runs zoom and torch changes one at a time. A request never cancels another request.
- `ratio` must be a JSON number. A string (also `"2"`) returns 400 `bad_request`.
- After an app start, the torch is off and the zoom is 1.0 when 1.0 is inside `[min_zoom_ratio, max_zoom_ratio]`, else `min_zoom_ratio`. (A phone with an ultrawide lens can have a minimum below 1.0; 1.0 is the main camera.)
- `rotation_degrees` is the rotation of the next snapshot (0, 90, 180, 270). With `rotation_locked: false`, the app follows the physical orientation of the phone. When the phone lies flat (no angle), the app keeps the last value.
- The still and the screen: the activity is locked to portrait, so the phone screen (and the screen stream) shows the camera image in the natural portrait orientation. The still of `/v1/snapshot` is that same camera image turned clockwise by `(360 - rotation_degrees) % 360` (CameraX `targetRotation`; the app writes this turn into the pixels, see the next rule). A client that shows the screen turned clockwise by V turns the still clockwise by `(V + rotation_degrees) % 360` to show the same picture. This rule describes the current app; it is not a new behavior.
- The app turns the pixels of the `/v1/snapshot` JPEG: the EXIF `Orientation` tag is 1 or absent, and clients do not read it. When the phone writes an EXIF turn, the app decodes the JPEG, turns the pixels, and encodes it again (other EXIF tags can be lost). When that fails, the app returns 500 `capture_failed` with the message "could not turn the still"; it never returns a still with an EXIF turn. `optics.output_width_px` is the width of the still before this turn (in the sensor orientation).
- The `/v1/snapshot` response has the headers `X-Rotation-Degrees` (the rotation that the app used for this still) and `X-App-Start-Id`. Clients use these headers for the still, not a status that they read before the snapshot (the phone can turn in between).
- `POST /v1/rotation {"degrees": N}` locks the snapshot rotation to N. `{"auto": true}` goes back to the physical orientation. Other values, or both fields, or neither, return 400 `bad_request`. After an app start, the rotation is auto.
- `POST /v1/preview` mirrors only the camera preview on the phone screen: `flip_horizontal` left-right, `flip_vertical` upside down. The status label and the other on-screen text stay readable (not mirrored). Both fields are required booleans; a missing field, another type, or an unknown field returns 400 `bad_request`. It does not change `/v1/snapshot`: the snapshot stays in the true orientation, and the MCP server applies its own turn and flips to the snapshots. After an app start, both preview flips are false; the MCP server sends them again after `phone_connect` and after an app start.
- `focus` comes from the latest preview capture result of the back camera. `distance_diopters` is `LENS_FOCUS_DISTANCE` (1/m; 0 means infinity), `null` until the first result or when the lens has fixed focus. `state` is the autofocus state: `focused`, `scanning`, `unfocused`, or `unknown`. `calibration` is `LENS_INFO_FOCUS_DISTANCE_CALIBRATION`: `uncalibrated`, `approximate`, or `calibrated` (with `uncalibrated`, the distance is not in real units: clients must not show cm). `min_distance_diopters` is `LENS_INFO_MINIMUM_FOCUS_DISTANCE` (0 = fixed focus). The `focus` object can be `null` before the camera is bound.
- `optics` describes the camera of `/v1/snapshot`: the first focal length, the physical sensor width (`SENSOR_INFO_PHYSICAL_SIZE`), and the snapshot width in pixels before rotation. Clients compute the distance in cm (100 / diopters) and the detail in px/mm (`output_width_px * focal_length_mm / (sensor_width_mm * distance_mm)`, thin-lens estimate). Zoom does not change this detail value: zoom crops.
- `POST /v1/camera {"in_sensor_zoom": true|false}` turns the vendor in-sensor zoom on or off. The body field is a required boolean (400 `bad_request` otherwise). The app binds the camera again: the preview stops for about 1 s, and the zoom and the torch stay as they were. `CameraStatus.in_sensor_zoom` is `off`, `on`, `unsupported` (the phone has no such vendor mode; the request still returns 200 with this value), or `fallback` (the vendor session failed; the app runs in the normal mode). After an app start, it is `off`.
- `POST /v1/camera` also takes `af_mode`: `continuous` (default) or `macro` (the camera's close-range autofocus mode, for work near the minimum focus distance). The body needs at least one of `in_sensor_zoom` and `af_mode`; an unknown field or value returns 400 `bad_request`. `CameraStatus.af_mode` is the current mode; after an app start it is `continuous`. A phone without the macro mode returns 200 and `af_mode` stays `continuous`. `/v1/focus` works in both modes.
- With `in_sensor_zoom` `on`, a zoom at 2x or more can give real extra detail (a sensor crop at full density, not optics). The detail estimate in `optics` does not include this gain.
- `POST /v1/focus` focuses and meters (autofocus and auto exposure) on one point. Give exactly one pair, each value a number in [0, 1] (400 `bad_request` otherwise):
  - `screen_x`, `screen_y`: a point on the phone screen as the screen stream shows it (the display in its natural portrait orientation, 0,0 = top left). A point outside the camera preview returns 400 `bad_request` with the message "outside the preview". The app takes the preview position on the screen and the preview flips into account.
  - `snapshot_x`, `snapshot_y`: a point on the image that `/v1/snapshot` returns now (true orientation, before any turn or flip that the MCP server applies; 0,0 = top left). The app takes the snapshot rotation into account.
- The focus lock ends after 5 s (`FOCUS_HOLD`), then the camera goes back to continuous autofocus. A new `/v1/focus`, a zoom change, or an in-sensor zoom change ends it earlier. The response comes at once; `focus.state` in later statuses shows `scanning`, then `focused` or `unfocused`.
- `POST /v1/overlay` draws highlight boxes over the camera preview on the phone screen (so they also show in the screen stream). Each box is a rectangle on the true-orientation `/v1/snapshot` image, normalized to [0, 1] (`snapshot_x`, `snapshot_y` = top left corner; `width`, `height` > 0; the box must be inside the image), with a `label` of at most 32 characters and an optional `tag` of 1-3 characters. At most 8 boxes (400 `bad_request` otherwise). The app draws the boxes, tags, and a legend with the labels by the rules in `docs/overlay-layout.md`, not mirrored, and keeps the boxes aligned when the zoom, the rotation, or the preview flips change (zoom crops around the center, so the app scales the boxes around the center by the zoom change since the call). `{"boxes": []}` removes all boxes. The boxes also go away after 10 minutes (`OVERLAY_TTL`) and at an app start. The overlay never goes into `/v1/snapshot` images.
- The overlay body can also have `arrows` (optional, at most 4): `{"angle_deg": 45.0, "label": "J4 ~4 cm"}`. The angle is a direction on the true-orientation snapshot image (0 = right, 90 = down, 180 = left, 270 = up; any number is taken modulo 360). The app draws each arrow at the edge of the camera preview, pointing in that direction after the same rotation and flip mapping as the boxes, in green, with its label (unmirrored, upright for the viewer). An arrow means: the target is outside the view in that direction. `{"boxes": [], "arrows": []}` removes everything; a body without `arrows` removes the arrows. The TTL and the start rule are the same as for the boxes.
- Overlay body rules: every body that is not a `visible` body needs `boxes` (use `[]` to send only arrows); `arrows` is optional. An arrow can have an optional `tag` with the same rule as a box tag. A tag matches `^[A-Za-z0-9]{1,3}$` (ASCII letters and digits); another tag returns 400 `bad_request`. "Inside the image" means `snapshot_x >= 0`, `snapshot_y >= 0` (strict), and `snapshot_x + width <= 1.0001`, `snapshot_y + height <= 1.0001` (a tolerance only at the right and bottom edges, for rounding).
- The app runs the layout of `docs/overlay-layout.md` on all boxes and arrows first, so tags and colours come from the full list. Then it draws only the boxes that are inside `overlay_region` (a box that is partly inside is drawn whole; a box that is fully outside is not drawn).
- Clients (the MCP server, the page, the tracker) forget their copy of the boxes and arrows at the same times as the app: after `OVERLAY_TTL` (10 minutes) from the call, and when `app_start_id` changes.
- `CameraStatus.overlay_boxes` is the number of boxes that the app keeps now, visible or hidden (0 when none).
- `CameraStatus.overlay_arrows` is the number of arrows that the app keeps now, visible or hidden (0 when none).
- `POST /v1/overlay {"visible": false}` hides the boxes and arrows on the phone screen without removing them; `{"visible": true}` shows them again. A body with only `visible` does not change the boxes or arrows. A body with `boxes` or `arrows` does not change the visibility. `CameraStatus.overlay_visible` is the current value; after an app start it is `true`. The TTL still runs while the overlay is hidden. A body with `visible` together with `boxes` or `arrows` returns 400 `bad_request`.
- `CameraStatus.preview_region` is the part of the current `/v1/snapshot` image (true orientation, normalized) that the camera preview on the phone screen shows now. The preview fills the screen and can cut off the sides or the top and bottom of the still. It takes the rotation and the screen size into account. Clients use it to tell whether a box or a point is visible on the phone. Zoom and the preview flips do not change it (zoom crops the still and the preview alike, and the crop is centred, so a mirror shows the same region). It is `null` before the camera is bound or while the preview has no size.
- `CameraStatus.overlay_region` is the part of the current `/v1/snapshot` image (true orientation, normalized) where the app draws highlight boxes: `preview_region` without the system bars, display cutouts, and the app's status label (the safe area of `docs/overlay-layout.md`). A box outside it is not drawn (the legend still lists it). Clients use `overlay_region`, not `preview_region`, to tell the user whether a box shows on the phone. It is `null` when `preview_region` is `null`, and also while the safe area is not measured yet (the window has no size); then clients use `preview_region`. A measured safe area with no room (for example when the status label covers it) gives a region with `width` 0 or `height` 0, not `null`, placed at the centre of `preview_region` (so directions from it stay meaningful): nothing shows on the phone, and clients must not fall back to `preview_region` then. Unlike `preview_region`, it changes with the preview flips and the rotation, because the system bars are not symmetric (for example, a vertical flip moves the region). Clients read it again after a flip or rotation change, not only at start.
- `CameraStatus.app_start_id` is a UUID v7 that the app makes once at each start of the camera activity (a new camera session with the start state; one process can have more than one). It does not change while that activity runs. Clients use it to find a restart: send the stored settings (preview flips, in-sensor zoom, af_mode, overlay visibility) again only when this id changes, or when the user or an agent changes a setting. The preview flips also go to the phone when the effective image turn changes (a Screen view change, or a phone turn in Auto). A status value that differs from a client's stored setting is not a reason to send it again: another client may have changed it.
- Labels of boxes and arrows are drawn upright for the viewer in every phone orientation (also when the phone is sideways).
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
