# Report: dd-android

## What I did

- Made the Gradle project in `android/`: Kotlin, Gradle Kotlin DSL, version catalog (`gradle/libs.versions.toml`), Gradle wrapper 9.7.1 (made with the nix `gradle`).
- Versions: AGP 9.4.1 (built-in Kotlin), Kotlin 2.4.20, CameraX 1.6.2, Ktor 3.6.0 (server CIO), kotlinx.serialization 1.11.0, coroutines 1.11.0. minSdk 26, compileSdk and targetSdk 36.
- `Constants.kt`: one object with all values (host, port, paths, `STEP_FACTOR = 1.5f`, messages).
- `Models.kt`: `CameraStatus`, `ZoomRequest`, `TorchRequest`, `HealthResponse`, `ApiError`, `ErrorCode` (wire name + HTTP status), `ApiException`.
- `ZoomLogic.kt`: pure zoom rules (step, clamp, request check). No Android types.
- `CameraPort.kt`: interface between the HTTP API and the camera.
- `ApiServer.kt`: Ktor routes for the five endpoints, and StatusPages that return an `ApiError` for every non-2xx response.
- `CameraController.kt`: CameraX back camera (preview + still capture), zoom, torch, JPEG capture with EXIF. Captures run one at a time (mutex).
- `MainActivity.kt`: full-screen `PreviewView`, screen kept on, camera permission request, label with zoom ratio, torch state and `listening on 127.0.0.1:8765`.
- `android/README.md`: build, install, start, API check.

### Why the activity lifecycle and not a foreground service

Android gives camera access only to a visible app or to a camera-type foreground service. The app keeps the screen on, so the activity is enough.
A service adds a notification and more permissions for no gain. The server runs from `onCreate` to `onDestroy`.
The activity is `singleTask` and handles configuration changes itself, so a second server never tries to bind the same port.

## What works

Build and unit tests (27 tests: `ZoomLogicTest`, `ApiServerTest` with a fake camera), from `android/`:

```sh
nix develop .. --command ./gradlew assembleDebug testDebugUnitTest
# BUILD SUCCESSFUL
```

On the phone `7fad170e` (Xiaomi 2510ERA8BG, API 36):

```sh
adb -s 7fad170e install -r app/build/outputs/apk/debug/app-debug.apk   # Success (second try, see below)
adb -s 7fad170e shell pm grant dev.jayson.debugdevices.camera android.permission.CAMERA
adb -s 7fad170e shell am start -n dev.jayson.debugdevices.camera/.MainActivity
adb -s 7fad170e forward tcp:18765 tcp:8765
```

| Request | Result |
|---|---|
| `GET /v1/health` | 200 `{"ok":true,"app_version":"0.1.0"}` |
| `GET /v1/status` | 200 `{"zoom_ratio":1.0,"min_zoom_ratio":1.0,"max_zoom_ratio":10.0,"torch_enabled":false,"has_flash_unit":true}` |
| `POST /v1/zoom {"step":"in"}` twice, then `"out"` | 200, zoom 1.5, 2.25, 1.5 |
| `POST /v1/zoom {"ratio":100}` / `{"ratio":0.01}` | 200, clamped to 10.0 / 1.0 |
| `POST /v1/torch {"enabled":true}` then `false` | 200, `torch_enabled` true then false (torch turned on) |
| `POST /v1/zoom {"step":"up"}` | 400 `bad_request` |
| `POST /v1/zoom` without `Content-Type` | 400 `bad_request` |
| `GET /v1/nope` | 404 `not_found` |
| `DELETE /v1/status` | 405 with `bad_request` body |
| `GET /v1/snapshot` | 200 `image/jpeg`, 1.88 MB, 3060x4080, about 1.2 s, real scene (not black) |

The Fire TV devices were not touched. Only `-s 7fad170e` was used.

## Open items and notes

1. The first `adb install` failed with `INSTALL_FAILED_USER_RESTRICTED` (Xiaomi "Install via USB" check). The second try succeeded, probably after a person accepted the prompt on the phone. The README tells how to fix it.
2. Local port 8765 on the PC is in use by another process (`python`, pid 1117704, listens on 127.0.0.1:8765, answers `{"error": "auth required"}` 401). `adb forward tcp:8765 tcp:8765` fails with `Address already in use`. dd-mcp: use a different local port by default (for example 18765), or pick a free port. The forward `tcp:18765 -> tcp:8765` on 7fad170e is still active.
3. compileSdk is 36 because `flake.nix` has only `android-36`. Latest stable is 37. Because of this, `androidx.core` is pinned to 1.18.0 (1.19.0 needs compileSdk 37). Proposal for dd-research: add platform 37 to the flake, then raise compileSdk/targetSdk to 37 and `androidx.core` to 1.19.0.
4. Proposals for `docs/phone-api.md` (not changed):
   - A wrong method returns 405 with `bad_request` in the body. The contract has no code for it. Add `method_not_allowed` (405), or accept this.
   - The contract has no code for an unexpected server error. The app uses `camera_not_ready` (503) for unknown exceptions. Add `internal_error` (500)?
   - When the activity is stopped (screen off, app in background), camera endpoints return 503 `camera_not_ready` with the message `Camera is not active`. Document this.
   - Zoom ratios are 32-bit floats. A step can give values such as `3.375` or `1.6500001` in JSON.
5. The vision model change (`openai/gpt-6-luna`) does not touch `android/`.
6. ktlint is in the flake but I did not add a lint task or a prek hook for Kotlin. That belongs to the repository setup.
