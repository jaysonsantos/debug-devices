# Debug camera app

Android app that makes the phone a remote-controlled camera for a coding agent.
It shows the back camera full screen and serves the HTTP API in [`../docs/phone-api.md`](../docs/phone-api.md) on `127.0.0.1:8765`.

- Package: `dev.jayson.debugdevices.camera`
- minSdk 26, compileSdk and targetSdk 36 (the Android platform in `flake.nix`)
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
curl -s -o snapshot.jpg localhost:8765/v1/snapshot
```

## Behavior

- The server runs from `onCreate` to `onDestroy` of `MainActivity`. Android gives camera access only to a visible app
  or to a camera foreground service. The app keeps the screen on, so the activity is enough.
- When the activity is not started (for example, the screen is off), the camera endpoints return `503 camera_not_ready`.
- The activity is `singleTask`, so `am start` does not open a second server on the same port.
- All values with a meaning are in `Constants.kt`.
