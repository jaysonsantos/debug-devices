# Debug camera app

Android app that makes the phone a remote-controlled camera for a coding agent.
It shows the back camera full screen and serves the HTTP API in [`../docs/phone-api.md`](../docs/phone-api.md) on `127.0.0.1:8765`.

- Package: `dev.jayson.debugdevices.camera`
- minSdk 26, compileSdk and targetSdk 37 (the Android platform in `flake.nix`)
- CameraX (preview, still capture, zoom, torch), Ktor server with the CIO engine, kotlinx.serialization

## Build

Run from `android/` inside the dev shell (`nix develop ..` or direnv at the repository root):

```sh
./gradlew assembleDebug testDebugUnitTest
```

The APK is `app/build/outputs/apk/debug/app-debug.apk`.
The unit tests cover the zoom rules (`ZoomLogic`) and every endpoint with a fake camera (`ApiServerTest`).

## Install and start

Always give the serial of the phone. Do not install on other devices.

```sh
adb devices -l
adb -s <serial> install -r app/build/outputs/apk/debug/app-debug.apk
adb -s <serial> shell pm grant dev.jayson.debugdevices.camera android.permission.CAMERA
adb -s <serial> shell cmd notification allow_dnd dev.jayson.debugdevices.camera
adb -s <serial> shell am start -n dev.jayson.debugdevices.camera/.MainActivity
```

On Xiaomi phones (MIUI, HyperOS), turn on "Install via USB" in Developer options first.
Without it, `adb install` fails with `INSTALL_FAILED_USER_RESTRICTED`.

If you do not grant the permission with `pm grant`, the app asks for it on the screen at start.

`allow_dnd` gives the app Do Not Disturb access, so it can silence notifications while it is visible (see Behavior).
You can also give it on the phone: Settings, "Do Not Disturb access" (the search finds it), "Debug Camera".
The app does not ask for it on the screen. Without the access, notifications stay on, and the app works as before.

## Check the API

```sh
adb -s <serial> forward tcp:8765 tcp:8765
curl -s localhost:8765/v1/health
curl -s localhost:8765/v1/status
curl -s -X POST -H 'Content-Type: application/json' -d '{"step":"in"}' localhost:8765/v1/zoom
curl -s -X POST -H 'Content-Type: application/json' -d '{"enabled":true}' localhost:8765/v1/torch
curl -s -X POST -H 'Content-Type: application/json' -d '{"degrees":90}' localhost:8765/v1/rotation
curl -s -X POST -H 'Content-Type: application/json' -d '{"auto":true}' localhost:8765/v1/rotation
curl -s -X POST -H 'Content-Type: application/json' -d '{"flip_horizontal":true,"flip_vertical":false}' localhost:8765/v1/preview
curl -s -X POST -H 'Content-Type: application/json' -d '{"screen_x":0.5,"screen_y":0.5}' localhost:8765/v1/focus
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"boxes":[{"snapshot_x":0.42,"snapshot_y":0.31,"width":0.05,"height":0.04,"label":"U730"}]}' localhost:8765/v1/overlay
curl -s -o snapshot.jpg localhost:8765/v1/snapshot
```

## Behavior

- The server runs from `onCreate` to `onDestroy` of `MainActivity`. Android gives camera access only to a visible app
  or to a camera foreground service. The app keeps the screen on, so the activity is enough.
- After a start, the camera endpoints return `503 camera_not_ready` until the start state is set (zoom 1x, or the minimum when 1x is outside the range; torch off).
  `/v1/health` returns 200 before that. Retry `/v1/status` while it returns 503.
- Zoom and torch changes run one at a time (`ControlGate`), so a request never cancels another request.
- An unexpected error returns `500 internal_error`. Read the stack trace with `adb -s <serial> logcat -s DebugCamera:E`.
- The activity stays in portrait, so the preview never restarts. An `OrientationEventListener` sets the snapshot
  rotation (`ImageCapture.targetRotation`) and turns the on-screen label (inside the system bar and cutout insets) to the physical orientation. It uses 4 buckets
  with 15 degrees of hysteresis (`OrientationLogic`). When the phone lies flat, Android reports no orientation, and the
  app keeps the last rotation. `adb -s <serial> logcat -s DebugCamera:I` shows each rotation change.
- `POST /v1/rotation {"degrees": 0|90|180|270}` locks the snapshot rotation (`RotationState`); `{"auto": true}` goes
  back to the sensor. `CameraStatus` has `rotation_degrees` and `rotation_locked`. After a start, the rotation is auto.
- Vendor in-sensor zoom, off after an app start: `POST /v1/camera {"in_sensor_zoom": true|false}` (status field
  `in_sensor_zoom`: `off`, `on`, `unsupported`, `fallback`). The app binds again and keeps the zoom and the torch
  (the preview stops for about 1 s; turning it on takes about 5 s, because the app checks the new session for 4 s).
  On: the session uses the vendor operation mode `0x9005` with `EnableInsensorZoom = 1` and
  `xiaomi.app.module = 163`, like the Xiaomi camera app. From 2x on, the sensor reads a half-field crop at full
  density (`InSensorZoomState = 2`), also at 4x and more. The HAL enters this mode only when the zoom lands between
  2x and 4x, so a jump from below 2x to 4x or more goes through 3x first. The adb helper
  `am start ... --ez in_sensor_zoom true|false` still works. It needs CameraX 1.7.0-alpha03 and two
  library-internal workarounds (see `CameraController.kt`).
- `POST /v1/preview {"flip_horizontal": bool, "flip_vertical": bool}` mirrors only the on-screen camera preview
  (`PreviewView` scale). The status label stays readable and shows `flip H` / `flip V`. Snapshots do not change.
  Both fields are required. The flips are in the viewer's upright frame, so when the phone is sideways, the axes
  swap (`PreviewFlipLogic`). The preview uses `PreviewView.ImplementationMode.COMPATIBLE` (a `TextureView`),
  because a `SurfaceView` ignores the view scale. Both flips are off after an app start.
- `CameraStatus.focus` and `CameraStatus.optics`: the live focus distance (diopters, from the latest preview capture
  result), the autofocus state, the calibration, the minimum focus distance, the focal length, the sensor width, and
  the snapshot width. Distance in cm = 100 / diopters. Detail in px/mm = `output_width_px * focal_length_mm /
  (sensor_width_mm * distance_mm)`. The phone label shows `≈ NN cm` when the calibration is `approximate` or
  `calibrated`. A capture callback on the Preview keeps the values (no logging per frame).
- `POST /v1/focus` focuses and meters (AF + AE) on one point: `screen_x`/`screen_y` (the phone screen as the
  screen stream shows it) or `snapshot_x`/`snapshot_y` (the current snapshot). The lock ends after 5 s, or earlier on a
  new focus, a zoom change, or a rebind. A screen tap shows a short focus ring. The screen shows only the middle part
  of the 3:4 camera image (the preview fills the tall display), and the PreviewView metering factory accounts for it.
- `POST /v1/overlay` draws up to 8 highlight boxes over the preview by `docs/overlay-layout.md`: a dark outline
  under a coloured one (green, cyan, yellow, magenta), a tag badge next to each box (the optional `tag`, else A, B,
  ...), and a legend with the labels in the corner farthest from the boxes. `OverlayLayout` is the same pure layout
  as the server's; `OverlayLayoutTest` runs the shared vectors in `docs/overlay-layout-vectors.json`. The boxes are given on
  the snapshot. `OverlayLogic` maps them to the preview (snapshot rotation, preview crop, flips) and scales them around
  the centre when the zoom changes. They are never in `/v1/snapshot`. `{"boxes": []}` clears them; they also go away
  after 10 minutes and at an app start. Optional `arrows` (at most 4, `angle_deg` on the snapshot, 0 = right,
  90 = down) show a green arrow at the preview edge in that direction: the target is outside the view there. Labels
  of boxes and arrows are turned upright for the viewer, also when the phone is sideways. `{"visible": false}` (alone)
  hides the overlay without removing it, `{"visible": true}` shows it again; `CameraStatus.overlay_visible`.
- `POST /v1/camera {"af_mode": "continuous" | "macro"}` (with or without `in_sensor_zoom`) sets the autofocus mode.
  MACRO is the camera's close-range mode (a Camera2 request option); the lens moves on a focus trigger, so the app
  scans the centre once after the switch, and `/v1/focus` triggers it again. A phone without MACRO stays
  `continuous`. After an app start it is `continuous`.
- `CameraStatus.app_start_id` is a UUID v7 that the app makes once at each start (`AppStart`). Clients send their
  stored settings again only when it changes.
- `CameraStatus.preview_region` is the part of the current snapshot that the phone screen shows (the preview fills
  the tall screen and cuts off the sides of the 3:4 image). `OverlayLogic.previewRegion` computes it from the view
  size, the scale type, and the rotations; zoom and flips do not change it.
- When the activity is not in the foreground (resumed), the camera endpoints return `503 camera_not_ready`.
- While the app is visible (`onStart` to `onStop`), notifications are silent: no sound, no vibration, and no
  heads-up notification over the preview. This includes calls and messages. Alarms and media still play. The app
  turns its own Do Not Disturb rule ("Debug Camera quiet mode") on and off (`QuietMode`, `ZenRules`), so your Do Not
  Disturb settings do not change. Android shows the notifications again when the app leaves the screen.
  It needs Android 10 or later and the Do Not Disturb access (see Install and start). When you give the access while
  the app is visible, the rule goes on in about 1 s. `adb -s <serial> logcat -s DebugCamera:I` shows each change.
  To turn it off, disable the rule in the Do Not Disturb settings of the phone, or remove the access
  (`cmd notification disallow_dnd`).
- The rule also goes off when the app process dies while the app is visible (a crash, a kill, `am force-stop`, a new
  `adb install`). A dead process cannot turn its rule off, so `QuietModeGuard` does it: Android binds this condition
  provider service while the app has the Do Not Disturb access, and it starts the process again after the process
  dies. A process that starts with no visible activity turns the rule off (`QuietMode.reset`). On the S22
  (Android 16) this takes 1 s to 10 s. The cost: the app process stays in memory while the app has the access. It
  starts no camera and no server, because those follow the activity.
  A phone that does not let Android start the process again (a vendor autostart rule, a low-RAM device) keeps the
  rule on until the next app start. Then start the app and leave it, or turn the rule off in the Do Not Disturb
  settings.
- The activity is `singleTask`, so `am start` does not open a second server on the same port.
- All values with a meaning are in `Constants.kt`.
