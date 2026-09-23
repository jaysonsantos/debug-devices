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
- When the activity is not in the foreground (resumed), the camera endpoints return `503 camera_not_ready`.
- The activity is `singleTask`, so `am start` does not open a second server on the same port.
- All values with a meaning are in `Constants.kt`.
