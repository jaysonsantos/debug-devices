# Report: dd-android

## What I did

- Made the Gradle project in `android/`: Kotlin, Gradle Kotlin DSL, version catalog (`gradle/libs.versions.toml`), Gradle wrapper 9.7.1 (made with the nix `gradle`).
- Versions: AGP 9.4.1 (built-in Kotlin), Kotlin 2.4.20, CameraX 1.6.2, Ktor 3.6.0 (server CIO), kotlinx.serialization 1.11.0, coroutines 1.11.0. minSdk 26, compileSdk and targetSdk 37, build-tools 37.0.0, androidx.core 1.19.0.
- `Constants.kt`: one object with all values (host, port, paths, `STEP_FACTOR = 1.5f`, messages).
- `Models.kt`: `CameraStatus`, `ZoomRequest`, `TorchRequest`, `HealthResponse`, `ApiError`, `ErrorCode` (wire name + HTTP status), `ApiException`.
- `ZoomLogic.kt`: pure zoom rules (step, clamp, request check). No Android types.
- `CameraPort.kt`: interface between the HTTP API and the camera.
- `ApiServer.kt`: Ktor routes for the five endpoints, and StatusPages that return an `ApiError` for every non-2xx response.
- `CameraController.kt`: CameraX back camera (preview + still capture), zoom, torch, JPEG capture with EXIF. Captures run one at a time (mutex).
- `MainActivity.kt`: full-screen `PreviewView`, screen kept on, camera permission request, label with zoom ratio, torch state and `listening on 127.0.0.1:8765`.
- `android/README.md`: build, install, start, API check.

### Round 2 (contract update)

- New error code `method_not_allowed` (405). A wrong method on a known path returns it.
- A zoom body with both `ratio` and `step`, or with neither, returns 400 `bad_request` (unit test for `{}` added).
- `/v1/health` returns 200 while the camera is not bound. The camera endpoints return 503 (unit test added).
- Start state: `CameraController.applyStartState` waits until the camera is open (`CameraState.Type.OPEN`), then sets zoom to `min_zoom_ratio` (`ZoomLogic.startRatio`) and torch off (`Constants.Start.TORCH_ENABLED`). CameraX cancels zoom and torch calls on a camera that is not open, so the wait is necessary.
- Snapshot: `ImageCapture` uses `FLASH_MODE_OFF` and the route does not touch the torch (unit test added).
- New dependency: `androidx.lifecycle:lifecycle-livedata-ktx` (for `LiveData.asFlow`).
- SDK: the flake now has `android-37.0` and build-tools 37.0.0. compileSdk and targetSdk are 37, `buildToolsVersion = "37.0.0"`, androidx.core 1.19.0.

### Why the activity lifecycle and not a foreground service

Android gives camera access only to a visible app or to a camera-type foreground service. The app keeps the screen on, so the activity is enough.
A service adds a notification and more permissions for no gain. The server runs from `onCreate` to `onDestroy`.
The activity is `singleTask` and handles configuration changes itself, so a second server never tries to bind the same port.

### Round 3 (dd-qa bugs)

- Bug 1 (crash on zoom during the start state): new class `ControlGate` (pure Kotlin). It holds one `Mutex` and a ready flag. `status`, `zoom`, `torch`, and `snapshot` return 503 `camera_not_ready` ("Camera start state is not set yet") until the start state is set. They fail at once, without a wait. The start state runs under the same lock. The gate opens after the start state, also when it fails, so the API never stays blocked. `MainActivity` catches every error from bind and the start state and logs it with tag `DebugCamera`. Only a coroutine cancel goes through.
- Bug 2 (`{"ratio":"2"}` gave 200): `StrictFloatSerializer` and `StrictBooleanSerializer` (`StrictSerializers.kt`) refuse a JSON string, array, or object. `ratio` and `enabled` use them. `{"enabled":"true"}` also returns 400 now.
- Bug 4 (a request cancels another): zoom, torch, and the start state go through one `ControlGate` lock. A new unit test also found a second race: the route read the current zoom outside the lock, so parallel `step` requests lost updates. The fix: `CameraPort.updateZoom(target)` reads the status and sets the zoom under the lock. `ZoomLogic.validate` refuses a bad body with 400 before the lock.
- Bug 5: new error code `internal_error` (500) for every error that is not in the contract. `cameraApi(..., onUnexpected)` gets the error. `MainActivity` logs the stack trace with `Log.e(DebugCamera, ...)`.
- New unit tests: `ControlGateTest` (4: 503 before start, 503 at once during start, failed start opens the gate, 20 parallel changes run one at a time and none is cancelled). `ApiServerTest` (+8: zoom during start state, ratio as string/bool/array, number forms still work, 1e400, enabled as string, 10 parallel steps all counted, internal_error logged, contract errors not logged). `ZoomLogicTest` (+1: validate).

### Round 4 (landscape snapshots)

- The activity stays locked to portrait, so the preview does not restart when the phone turns.
- `OrientationLogic.kt` (pure): maps the `OrientationEventListener` angle to `Surface.ROTATION_*`. Four buckets: angle 0 -> `ROTATION_0`, 90 -> `ROTATION_270`, 180 -> `ROTATION_180`, 270 -> `ROTATION_90` (the CameraX mapping). Hysteresis: the rotation changes only when the angle is 60 degrees (45 + `HYSTERESIS_DEGREES` 15) or more from the center of the current bucket. `ORIENTATION_UNKNOWN` (phone flat) keeps the current rotation.
- `MainActivity`: the listener runs from `onResume` to `onPause`. A change sets `imageCapture.targetRotation` (`CameraController.setTargetRotation`), turns the overlay with the status label (see the overlay fix below), and logs `Snapshot rotation degrees: <n>` with tag `DebugCamera` (level I).
- Constants in `Constants.Orientation`. `CameraStatus` is not changed (see the contract proposal below).
- New unit tests: `OrientationLogicTest` (7): bucket centers, 359/360, unknown angle, hysteresis from portrait (59 stays, 60 switches; 301 stays, 300 switches), hysteresis from landscape, jitter around 45 degrees, surface degrees.

### Round 5 (rotation endpoint, foreground rule)

- `POST /v1/rotation`: `RotationRequest(degrees, auto)` with strict serializers (new `StrictIntSerializer`: `"90"` and `90.5` give 400). `OrientationLogic.lockedRotationFor` gives the locked `Surface.ROTATION_*` (`degrees / 90`) or null for `{"auto": true}`. Both fields, no field, other degrees, and `auto: false` give 400.
- `RotationState.kt` (pure): the sensor rotation and an optional lock. The lock wins. The sensor value stays current while locked, so `auto` goes back to how the phone is held now. Auto after an app start. A change sets `imageCapture.targetRotation` and turns the overlay. It lives in `CameraController`, on the main thread.
- `CameraStatus` has `rotation_degrees` and `rotation_locked`. `setRotation` goes through `ControlGate` (503 until the start state is set).
- Foreground rule: `activeCamera()` now needs lifecycle `RESUMED` (was `STARTED`). Status, zoom, torch, rotation, and snapshot give 503 `camera_not_ready` ("Camera is not active") in the background. `/v1/health` stays 200.
- New unit tests: `RotationStateTest` (4), `OrientationLogicTest` +2 (request to rotation, bad requests), `ApiServerTest` +3 (lock and unlock, 8 bad bodies, 503 before the start state and 405 on GET). 63 tests in total.

## What works

Build and unit tests (63 tests: `ZoomLogicTest` 16, `ControlGateTest` 4, `ApiServerTest` 29 with a fake camera, `OrientationLogicTest` 10, `RotationStateTest` 4), from `android/`:

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

Round 2 check on `7fad170e` (after the SDK 37 build). The old app was at zoom 3.0 with the torch on. Then I reinstalled, did `am force-stop` and `am start`:

| Request | Result |
|---|---|
| `GET /v1/status` after start | 200, `zoom_ratio` 1.0 (= min), `torch_enabled` false |
| `POST /v1/zoom {}` | 400 `bad_request` |
| `POST /v1/zoom {"ratio":2,"step":"in"}` | 400 `bad_request` |
| `GET /v1/zoom` | 405 `method_not_allowed` |
| `POST /v1/status` | 405 `method_not_allowed` |
| `GET /v1/nope` | 404 `not_found` |
| torch on, `GET /v1/snapshot`, `GET /v1/status` | 200 JPEG 1.40 MB in 1.27 s, then `torch_enabled` still true |
| torch off, `GET /v1/snapshot`, `GET /v1/status` | 200 JPEG 1.55 MB in 1.66 s, then `torch_enabled` still false |

The server answered about 3 s after `am start`. Before that, curl through the forward got no response (`000`). Clients must retry `/v1/health` after a start.

Round 3 check on `7fad170e` (new APK installed at about 18:40).

Bug 1 repro, 5 runs of `am force-stop`, `am start`, then `POST /v1/zoom {"ratio":3}` every 20 ms (script: scratchpad `repro.sh`):

| Run | No answer (server not up) | 503 `camera_not_ready` from try | 200 from try |
|---|---|---|---|
| 1 | tries 1-87 | 88 | 100 |
| 2 | tries 1-48 | 49 | 74 |
| 3 | tries 1-45 | 46 | 69 |
| 4 | tries 1-41 | 42 | 66 |
| 5 | tries 1-41 | 42 | 68 |

Each run ended with status `zoom_ratio` 3.0, `torch_enabled` false. The app PID stayed 16164 after the last start. `adb -s 7fad170e logcat -b crash -d` has only one FATAL entry: 18:28:44 PID 4630, the old APK crash that dd-qa found. There is no new entry (checked at 18:45 and after the checks below). `logcat -s DebugCamera:E` is empty.

Bugs 2 and 4 on the phone:

| Request | Result |
|---|---|
| `{"ratio":"2"}`, `{"ratio":"abc"}` | 400 `bad_request` |
| `{"ratio":1e400}` | 400 `bad_request` ("'ratio' must be a finite number") |
| `{"ratio":2}` | 200, zoom 2.0 |
| torch `{"enabled":"true"}` | 400 `bad_request` |
| zoom 1, then 10 parallel `step: in` + 4 parallel torch changes | 14 x 200, zoom 10.0 (max) |
| then 5 parallel `step: out` | 5 x 200, zoom 1.3168724 = 10 / 1.5^5, no lost update |

I cannot trigger `internal_error` on the phone without a bug. A unit test covers it.

Round 4 check on `7fad170e` (APK installed at 19:13):

- Accelerometer (`dumpsys sensorservice`): `0.29, -0.78, 9.77`. The phone lies flat, screen up. `OrientationEventListener` gives `ORIENTATION_UNKNOWN` in this position, so the rotation stays `ROTATION_0` and no rotation log line appears.
- `GET /v1/snapshot`: 200, 2.87 MB. `exiftool`: `Orientation: Unknown (0)`, 3060x4080. This phone (with CameraX) writes the rotation into the pixels. It does not use the EXIF tag. So for a landscape check, look at the pixel size: portrait gives 3060x4080, landscape must give 4080x3060 (or EXIF 6/8 with 3060x4080).
- Crash buffer: no FATAL entry.
- Landscape, checked at 19:29 after the user turned the phone: `logcat -s DebugCamera:I` shows `19:28:57 Snapshot rotation degrees: 90` (one change, no flips). `GET /v1/snapshot`: 200, 2.47 MB, `exiftool`: 4080x3060, `Orientation: Horizontal (normal)`. The image is a landscape top-down view of a circuit board with a red probe. App PID 26636, no FATAL entry in the crash buffer.
- At 19:29 the accelerometer read `1.26, -0.61, 9.69` (phone close to flat, pointing down). The app kept rotation 90, as designed: a flat phone gives `ORIENTATION_UNKNOWN`. This confirms the risk in the contract proposal below.
- Portrait upright, 19:33: accelerometer `-1.26, 8.32, 6.13`. Log: rotation 0. Snapshot 3060x4080, upright (monitor, multimeter, magnifier). While the user turned the phone, the log showed 0, 90, 0 at 19:33:36-39, 1-2 s apart: real movement, not jitter.
- Other landscape direction, 19:34: accelerometer `-9.69, 0.37, 1.53`. Log: `19:34:22 Snapshot rotation degrees: 270`. Snapshot 4080x3060, EXIF `Orientation: Unknown (0)`, and the scene is upright.
- The three positions give upright images: portrait (0), landscape (90), and landscape (270). No FATAL entry in the crash buffer.
- Not checked: upside-down (180). The unit tests cover its mapping.
- Overlay fix (user report: the label was cut). Cause: the label turned around its own center in the top-left corner, so in landscape most of it went off the screen. The top of the screen also has the status bar and the camera cutout. Fix: the label is inside a full-screen `overlay` that turns as one piece. When the phone is sideways, the overlay takes the safe area size with width and height swapped (`OrientationLogic.isSideways`), so the label stays in the viewer's top-left corner. A `safe_area` parent gets padding from the system bar and cutout insets. The label animation was removed. Screenshot at 19:36 (landscape, rotation 90): the full label is visible, below the status bar. New unit test `sideways rotations` (54 tests in total).

Round 5 check on `7fad170e` (APK installed at about 19:42, phone in landscape, accelerometer `7.57, 0.23, 6.16`):

| Request | Result |
|---|---|
| `GET /v1/status` after start | 200, `rotation_degrees` 90 (sensor), `rotation_locked` false; snapshot 4080x3060 |
| `{"degrees":0}` / `90` / `180` / `270` | 200, locked; snapshots 3060x4080 / 4080x3060 / 3060x4080 / 4080x3060 |
| `GET /v1/status` | `rotation_degrees` 270, `rotation_locked` true |
| `{"auto":true}` | 200, `rotation_degrees` 90 (back to the sensor), unlocked; snapshot 4080x3060 |
| `{}`, `{"degrees":90,"auto":true}`, `{"degrees":45}`, `{"degrees":"90"}`, `{"auto":false}` | 400 `bad_request` |
| `GET /v1/rotation` | 405 `method_not_allowed` |
| HOME key (app in background): health / status / rotation / snapshot | 200 / 503 / 503 / 503 (`camera_not_ready`, "Camera is not active") |
| `am start` again, `GET /v1/status` | 200 |

Crash buffer: no FATAL entry. `logcat -s DebugCamera:E`: empty.

The Fire TV devices were not touched. Only `-s 7fad170e` was used.

## Open items and notes

1. The first `adb install` failed with `INSTALL_FAILED_USER_RESTRICTED` (Xiaomi "Install via USB" check). The second try succeeded, probably after a person accepted the prompt on the phone. The README tells how to fix it.
2. Local port 8765 on the PC is in use by another process (`python`, pid 1117704, listens on 127.0.0.1:8765, answers `{"error": "auth required"}` 401). `adb forward tcp:8765 tcp:8765` fails with `Address already in use`. dd-mcp: use a different local port by default (for example 18765), or pick a free port. The forward `tcp:18765 -> tcp:8765` on 7fad170e is still active.
3. Done in round 2: compileSdk/targetSdk 37 and `androidx.core` 1.19.0.
4. Proposals for `docs/phone-api.md` (not changed):
   - Zoom ratios are 32-bit floats. A step can give values such as `3.375` or `1.6500001` in JSON.
   - A rotation lock stays when the app goes to the background and comes back. Only an app start resets it to auto. The contract does not say this.
5. The vision model change (`openai/gpt-6-luna`) does not touch `android/`.
6. ktlint is in the flake but I did not add a lint task or a prek hook for Kotlin. That belongs to the repository setup.
