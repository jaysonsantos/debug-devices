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
adb -s <serial> shell am start -n dev.jayson.debugdevices.camera/.MainActivity
```

On Xiaomi phones (MIUI, HyperOS), turn on "Install via USB" in Developer options first.
Without it, `adb install` fails with `INSTALL_FAILED_USER_RESTRICTED`.

If you do not grant the permission with `pm grant`, the app asks for it on the screen at start.

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
curl -s -o snapshot.jpg localhost:8765/v1/snapshot
```

## Behavior

- The server runs from `onCreate` to `onDestroy` of `MainActivity`. Android gives camera access only to a visible app
  or to a camera foreground service. The app keeps the screen on, so the activity is enough.
- After a start, the camera endpoints return `503 camera_not_ready` until the start state is set (zoom at min, torch off).
  `/v1/health` returns 200 before that. Retry `/v1/status` while it returns 503.
- Zoom and torch changes run one at a time (`ControlGate`), so a request never cancels another request.
- An unexpected error returns `500 internal_error`. Read the stack trace with `adb -s <serial> logcat -s DebugCamera:E`.
- The activity stays in portrait, so the preview never restarts. An `OrientationEventListener` sets the snapshot
  rotation (`ImageCapture.targetRotation`) and turns the on-screen label (inside the system bar and cutout insets) to the physical orientation. It uses 4 buckets
  with 15 degrees of hysteresis (`OrientationLogic`). When the phone lies flat, Android reports no orientation, and the
  app keeps the last rotation. `adb -s <serial> logcat -s DebugCamera:I` shows each rotation change.
- `POST /v1/rotation {"degrees": 0|90|180|270}` locks the snapshot rotation (`RotationState`); `{"auto": true}` goes
  back to the sensor. `CameraStatus` has `rotation_degrees` and `rotation_locked`. After a start, the rotation is auto.
- Experiment, off by default: vendor in-sensor zoom of the 200 MP sensor. Turn it on or off with
  `adb -s <serial> shell am start -n dev.jayson.debugdevices.camera/.MainActivity --ez in_sensor_zoom true|false`.
  On: the session opens with the vendor operation mode `0x9005` and the session parameters
  `EnableInsensorZoom = 1` and `xiaomi.app.module = 163`, like the Xiaomi camera app. At 2x the sensor then reads a
  half-field crop at full density (`InSensorZoomState = 2`). The app binds again in NORMAL mode when the vendor
  session fails. It needs CameraX 1.7.0-alpha03 and two library-internal workarounds (see `CameraController.kt`).
  `logcat -s DebugCamera:I` shows `In-sensor zoom: ..., sessionType=0x9005, state=ON`.
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
- When the activity is not in the foreground (resumed), the camera endpoints return `503 camera_not_ready`.
- The activity is `singleTask`, so `am start` does not open a second server on the same port.
- All values with a meaning are in `Constants.kt`.
