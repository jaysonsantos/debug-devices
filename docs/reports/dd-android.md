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

### Round: in-sensor zoom experiment

Brief: option B of `docs/research/phone-lenses.md`. Result: **no detail gain. The HAL accepts the vendor parameter from our app, but it does not switch to in-sensor zoom.** The code stays in the app, off by default.

Code (no contract change):

- `InSensorZoom.kt` (pure): `InSensorZoomState` (`OFF`, `ON`, `UNSUPPORTED`, `FALLBACK`), `SessionEvent`, `VendorKeyPresence`, `InSensorZoomLogic` (intent extra, key presence in the session and request key lists, plan, session stability, fallback).
- `CameraController.bind(owner, previewView, inSensorZoom)`:
  1. Finds the back camera ID (`Camera2CameraInfo`) and reads `availableSessionKeys` and `availableCaptureRequestKeys` from `CameraManager`. It takes the framework key object with the vendor name.
  2. `OFF` or `UNSUPPORTED`: binds as before. `ON`: sets the key with `Camera2Interop.Extender.setCaptureRequestOption` on the Preview and ImageCapture builders.
  3. `ON`: watches the new session for 4 s (`SESSION_CHECK_MILLIS`). Stable = the preview streamed and no `CameraState` error. When the session is not stable, the state is `FALLBACK`, and the app binds again without the key.
  4. Logs `In-sensor zoom: requested=..., sessionKey=..., requestKey=..., state=...` (tag `DebugCamera`, level I), and the fallback with level W.
- `ImageCapture` is built again on each bind (the parameter is on the builder). It keeps the current snapshot rotation.
- `MainActivity`: off at process start. `am start ... --ez in_sensor_zoom true|false` sets it (`onCreate` and `onNewIntent`), and a change binds the camera again. Without the extra, the current value stays. The rebind runs the start state again (zoom at min, torch off).
- Constants in `Constants.InSensorZoom`. ktlint passes.
- New unit tests: `InSensorZoomLogicTest` (7). 70 tests in total, all pass (`nix develop .. --command ./gradlew --no-daemon assembleDebug testDebugUnitTest`).

Two bugs found on the phone and fixed:

1. **Crash at start** (first install of this round, 12:39). `imageCapture` was built in the constructor before the `rotation` field existed, so `buildImageCapture` read a null `RotationState` (`NullPointerException` in `CameraController.<init>`). Fix: declare `rotation` before `imageCapture`. The JVM unit tests do not construct `CameraController`, so they did not catch it. The app was down for about 2 minutes. Note: my `adb logcat -c` also cleared the crash buffer, so the crash counts below start at 12:41.
2. **The parameter never reached the HAL** (first "on" run). CameraX (CameraPipe back end, log tag `CXCP`) logged for each request: `Failed to set [org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom: 1] on CaptureRequest.Builder` with `java.lang.IllegalArgumentException: Not an array: class java.lang.Integer` (from `MarshalQueryableArray`). The framework gives this vendor key the type `int[]`, not `int`. CameraX only logs the error, so the session streamed and the app said `state=ON`, but `dumpsys` showed `EnableInsensorZoom = 0`. Fix: `CaptureRequest.Key<IntArray>` with the value `intArrayOf(1)`. After the fix: 0 `CXCP` set failures, and `dumpsys` shows 1. I did not use the images of that run.

Device test on `7fad170e` (APK installed at 12:45, phone in landscape on a stand, fixed scene: a red laptop main board at about 25-30 cm; flag on first, then off, back to back; each snapshot 2.5 s after the zoom change):

`dumpsys media.camera`, camera 0, "Last request sent" and "Latest received frame":

| Flag | Zoom | `EnableInsensorZoom` | `control.zoomRatio` | `scaler.cropRegion` | `inSensorZoom.InSensorZoomState` | `inSensorZoom.SensorSwitched` | `xiaomi.superResolution.inSensorZoomState` |
|---|---|---|---|---|---|---|---|
| on | 1x | 1 | 1.0 | 0 0 4080 3060 | 0 | 0 | 0 |
| on | 2x | 1 | 2.0 | 0 0 4080 3060 | 0 | 0 | 0 |
| on | 4x | 1 | 4.0 | 0 0 4080 3060 | 0 | 0 | 0 |
| off | 1x / 2x / 4x | 0 | 1.0 / 2.0 / 4.0 | 0 0 4080 3060 | 0 | 0 | 0 |

- The streams stay the same with the flag on and off: preview 1600 x 1200, still capture 4080 x 3060 (JPEG). The operation mode is `NORMAL`.
- The HAL result tags for in-sensor zoom stay 0 at 2x and 4x. The sensor never switches to the full-resolution mode.

Sharpness, 800 x 800 center crop of each 4080 x 3060 snapshot (variance of the Laplacian, and the mean squared gradient):

| Zoom | Off: Laplacian var / gradient | On: Laplacian var / gradient |
|---|---|---|
| 1x | 419.3 / 242.7 | 392.4 / 228.1 |
| 2x | 55.0 / 129.7 | 58.6 / 138.4 |
| 4x | 7.2 / 41.3 | 7.8 / 44.9 |

- The differences are 5-8%, in both directions. That is normal frame-to-frame noise (focus, a small shift of the framing). A real in-sensor zoom would give much more at 4x.
- I looked at the 2x and 4x crops side by side (Read tool). The detail is the same: the "000" markings of the resistors, the pad edges, and the soft upscaled edges at 4x look equal.
- Images and dumps: only in my scratch directory (`.../scratchpad/isz/`). Not committed, not sent to any service.

Stability: the session with the flag on streamed and gave snapshots (about 0.84 s each). There was no camera error, no fallback, and no crash after 12:41. At the end, zoom is 1x, the torch is off, and the flag is off (`state=OFF`). No Gradle daemon runs (all builds used `--no-daemon`).

Decision: **keep the code, off by default. Do not propose a contract change.** It has no effect when off, and the key type fix is useful for any later vendor key. Next test that is still possible (not done): the HAL can start in-sensor zoom only on `SCALER_CROP_REGION` zoom, or with the Xiaomi feature tag `com.xiaomi.camera.supportedfeatures.insensorzoom`. CameraX always uses `CONTROL_ZOOM_RATIO` on this phone, so that test needs a Camera2 request option for the crop region. If nobody plans that test, remove the code.

#### Rerun at the closer position

Run on 2026-09-25 at 19:44, the same way (flag on, then off, back to back; zoom 1x, 2x, 4x; `dumpsys` and a snapshot 2.5 s after each zoom change). No new build: the APK from 12:45. The app had zoom 5.6 from another client before the run.

- Position: the phone lies almost flat and looks down at the board (accelerometer `2.59, 0.20, 9.45`). The whole laptop board and much of the table are in the frame.
- **Distance: about 25-30 cm, not 10-12 cm.** Evidence: `android.lens.focusDistance` = 3.41 diopters, that is 29 cm (the calibration is "APPROXIMATE"). The board fills about half of the frame width (at 29 cm, the field is about 430 mm wide). The mounting-hole rings are about 60 px wide in the 4080 px image: about 11 px/mm, between the research values for 20 cm (14 px/mm) and 30 cm (9 px/mm).
- **Focus at 1x: sharp.** `afState` = `PASSIVE_FOCUSED`. In the 1x center crop, the board silkscreen text and the mounting-hole labels are easy to read.

| Flag | Zoom | `EnableInsensorZoom` | `cropRegion` | `InSensorZoomState` | `SensorSwitched` | `xiaomi.superResolution.inSensorZoomState` |
|---|---|---|---|---|---|---|
| on | 1x / 2x / 4x | 1 | 0 0 4080 3060 | 0 | 0 | 0 |
| off | 1x / 2x / 4x | 0 | 0 0 4080 3060 | 0 | 0 | 0 |

Sharpness, the same 800 x 800 center crop (variance of the Laplacian / mean squared gradient):

| Zoom | Off | On |
|---|---|---|
| 1x | 771.2 / 315.7 | 872.4 / 335.7 |
| 2x | 56.2 / 106.2 | 52.0 / 100.4 |
| 4x | 4.6 / 20.1 | 5.2 / 22.7 |

- The differences are again small and go both ways (on is higher at 1x and 4x, lower at 2x). At 1x the flag cannot change anything, so the 13% there shows the noise level of this measure.
- The 4x crops side by side look the same: the same mounting-hole label is readable in both, and the part edges have the same softness.
- 0 camera errors, 0 `CXCP` set failures, 0 crashes.
- End state: zoom 1x, torch off, flag off (`state=OFF`). No build, so no Gradle daemon. Images and dumps only in my scratch directory (`.../scratchpad/isz2/`). The full frame also shows part of a person at the edge, so these images must stay local.

**Result: the same as the first run.** The HAL gets `EnableInsensorZoom = 1` but never switches, so there is no detail gain at 2x or 4x. The decision stays: keep the code off by default, no contract change. To get more detail, move the phone to 10-12 cm (the research advice). The phone is not at that distance now.


### Round: preview flip on the phone screen

Contract: `POST /v1/preview {"flip_horizontal", "flip_vertical"}` and `CameraStatus.preview_flip_horizontal` / `preview_flip_vertical` (changed by the orchestrator).

Code:

- `PreviewFlip.kt`: `PreviewFlip` (the state, `NONE` after an app start) and the pure `PreviewFlipLogic`. `scale(flip, rotation)` gives the `PreviewView` scale factors. The flips are in the viewer's upright frame, like the snapshot. The activity stays in portrait, so when the phone is sideways (rotation 90/270) the axes swap: flip H mirrors the screen Y axis.
- `PreviewRequest`: both fields required, `StrictBooleanSerializer`. `ApiJson` refuses unknown fields. So a missing field, another type, `null`, an unknown field, or broken JSON gives 400 `bad_request`.
- `CameraController` holds the state (one place, main thread) and reports it in `CameraStatus`. `setPreviewFlip` goes through `ControlGate` (503 before the start state and in the background).
- `MainActivity`: `applyPreviewFlip` sets `scaleX` / `scaleY` on the `PreviewView` only. The label is in the sibling overlay, so it is never mirrored. It runs on a flip change and on a rotation change. The label shows ` · flip H` and ` · flip V`.
- **Bug found on the phone and fixed:** the first build set the scale, and the label showed `flip H`, but the preview did not change. `PreviewView` uses a `SurfaceView` by default (PERFORMANCE mode), and a `SurfaceView` ignores the view scale. Fix: `previewView.implementationMode = COMPATIBLE` (a `TextureView`).
- `/v1/snapshot` is not changed: `ImageCapture` does not see the view scale.
- New unit tests: `PreviewFlipLogicTest` (4) and `ApiServerTest` +3 (set and read both fields while the snapshot bytes stay the same, 9 bad bodies, 503 before the start state and 405 on GET). 77 tests in total, all pass. ktlint passes. Builds used `--no-daemon`.

Check on `7fad170e` (APK installed at about 19:50, phone flat, rotation 0):

| Request | Result |
|---|---|
| `GET /v1/status` after start | 200, both preview flips false |
| `{"flip_horizontal":true,"flip_vertical":false}` | 200, `preview_flip_horizontal` true |
| `{}`, only `flip_horizontal`, `"true"` as a string, an extra field `mirror` | 400 `bad_request` |
| `GET /v1/preview` | 405 `method_not_allowed` |

Screenshots (`adb -s 7fad170e exec-out screencap -p`, only in my scratch directory):

- None / H / V / H+V: the preview is mirrored left-right, upside down, and both ways. The logo on the board reads backwards in the H image. The label is readable in each image: `zoom 1.00x · torch off · flip H · listening on 127.0.0.1:8765`, `... · flip V ...`, `... · flip H · flip V ...`.
- Rotation locked to 90 with flip H: the label turns sideways and stays readable, and the preview is mirrored along the screen Y axis (left-right for a viewer who holds the phone sideways). Then `{"auto": true}` again.
- Snapshots with flip H and without a flip: the same orientation.
- Another client restarted the app during my first screenshot series (the PID changed from 32453 to 500 at 19:50:32, no crash). I did not use those images. The PID stayed 500 for the final series. No FATAL entry in the crash buffer.

End state: zoom 1x, torch off, both flips false, rotation auto, in-sensor zoom off.


### Round: live focus distance and optics

Contract: `CameraStatus.focus {distance_diopters, state, calibration, min_distance_diopters}` and `CameraStatus.optics {focal_length_mm, sensor_width_mm, output_width_px}` (changed by the orchestrator).

Code:

- `Focus.kt`: the models (`FocusInfo`, `FocusState`, `FocusCalibration`, `Optics`) and the pure `FocusLogic`. AF state map: `FOCUSED_LOCKED`, `PASSIVE_FOCUSED` -> `focused`; `ACTIVE_SCAN`, `PASSIVE_SCAN` -> `scanning`; `NOT_FOCUSED_LOCKED`, `PASSIVE_UNFOCUSED` -> `unfocused`; other or none -> `unknown`. A missing calibration is `uncalibrated` (the safe value). `focus` is null before the bind. The distance is null before the first result and with fixed focus (minimum distance 0). The label distance in cm is null when uncalibrated, unknown, or infinity (0).
- `CameraController`: one `CameraCaptureSession.CaptureCallback` instance on the Preview (`Camera2Interop.Extender.setSessionCaptureCallback`). It reads `LENS_FOCUS_DISTANCE` and `CONTROL_AF_STATE` and makes a new `FocusSample` (small, immutable, `@Volatile`) only when a value changes. It never logs. After each bind it reads `LENS_INFO_FOCUS_DISTANCE_CALIBRATION`, `LENS_INFO_MINIMUM_FOCUS_DISTANCE`, `LENS_INFO_AVAILABLE_FOCAL_LENGTHS[0]`, and `SENSOR_INFO_PHYSICAL_SIZE.width` (`Camera2CameraInfo`). The snapshot width comes from `ImageCapture.resolutionInfo` (the long side = the width before rotation).
- `ApiJson` no longer has `explicitNulls = false`, so the JSON has `"focus": null` and `"distance_diopters": null` as the contract says. The request models give their optional fields a null default, so the requests decode as before (the old tests pass).
- `MainActivity`: the label shows ` · ≈ NN cm`, and a coroutine reads it again every second while the app is visible (`repeatOnLifecycle(STARTED)`).
- New unit tests: `FocusLogicTest` (6) and `ApiServerTest` +1 (the full JSON shape, `"focus":null`, `"distance_diopters":null`). 84 tests in total, all pass. ktlint passes. Builds used `--no-daemon`.

Check on `7fad170e` (APK installed at 20:06, the phone at the same position as the in-sensor zoom rerun):

- `GET /v1/status`: `focus` = `{"distance_diopters": 3.2467532, "state": "focused", "calibration": "approximate", "min_distance_diopters": 10.0}`, `optics` = `{"focal_length_mm": 6.07, "sensor_width_mm": 9.1392, "output_width_px": 4080}`.
- Distance: 100 / 3.247 = **31 cm**. Detail: 4080 x 6.07 / (9.139 x 308) = **8.8 px/mm**. The minimum focus distance is 10 cm (10 diopters). So a move to 10-12 cm gives about 3x more detail.
- Six reads 1.5 s apart without a change: the same values (the phone and the scene did not move).
- Refocus: a zoom change to 3x gave one `scanning` read and then `focused` at 3.4965 (29 cm). Back to 1x: `scanning`, then `focused` at 3.3003 (30 cm). A torch on/off did not start a new focus scan.
- Phone label (screenshot, only in my scratch directory): `zoom 1.00x · torch off · ≈ 30 cm · listening on 127.0.0.1:8765`.
- No FATAL entry in the crash buffer, no `CXCP` errors, the same PID for the whole check.

End state: zoom 1x, torch off, both flips false.


### Round: vendor session mode 0x9005 (real in-sensor zoom)

Brief: open our session with the vendor operation mode `CUSTOM (36869)` = `0x9005`, like the Xiaomi camera app (`docs/research/xiaomi-app-zoom.md`). Result: **it works at 2x**. With two more settings, our session gets the same sensor state as the Xiaomi app at 2x, and the 2x snapshot shows more real detail. At 4x there is no extra gain yet.

Way chosen: **CameraX 1.7.0-alpha03** (the only 1.7 release; no rc or stable yet). It is the smallest change: CameraX still runs zoom, torch, preview flip, focus data, rotation, and the snapshot. The session is bound as a `SessionConfig(preview, imageCapture)`, because the session type is a session-level option. A plain Camera2 path would replace the whole capture pipeline.

What was necessary (each step checked with `dumpsys media.camera` on `7fad170e`):

1. `SessionConfig.Builder(...).camera2Interop { setSessionType(0x9005) }` alone: the session stayed `NORMAL (0)`. Cause in the 1.7.0-alpha03 sources: the interop writes `camera2.cameraCaptureSession.sessionType`, `SessionConfig.Builder.build()` reads `camerax.core.useCase.sessionType`, and `SupportedSurfaceCombination` gives each stream spec `SESSION_TYPE_REGULAR` anyway. This looks like a CameraX bug.
2. Workaround (library-internal API, `@SuppressLint("RestrictedApi")`): the Preview gets its own default session config (the CameraX default template `TEMPLATE_PREVIEW`) with `Camera2ImplConfig.SESSION_TYPE_OPTION = 0x9005`. `UseCaseCameraConfig` reads that option first. Result: `Operation mode: CUSTOM (36869)`. A second write to `UseCaseConfig.OPTION_SESSION_TYPE` is also in the code. It did not help alone. I did not test the first workaround without it.
3. In the vendor mode with `EnableInsensorZoom = 1`, the HAL still reported `InSensorZoomState = 0` at 2x. Only the base sensor mode changed (`sensorModeCache[0]` 6 instead of 2). A request diff against the Xiaomi app's 2x dump (dd-research scratch) showed the session key `xiaomi.app.module`: 163 in the Xiaomi app, not set by us (static default 65535). With `xiaomi.app.module = 163` as a session parameter and a request option, the switch happens.

Code:

- `InSensorZoomLogic`: `plan` (off, on, unsupported: key missing or Android < 9), `sessionType` (`0x9005` only for `ON`), `vendorParameters` (`EnableInsensorZoom = 1`, `xiaomi.app.module = 163` only for `ON`), `afterBindFailure` (`ON` -> `FALLBACK`). Constants in `Constants.InSensorZoom`.
- `CameraController`: vendor int32 keys come from the camera's key lists (type `int[]`). With `ON`, it binds the `SessionConfig` with the session type and the session parameters. An `IllegalArgumentException` or `IllegalStateException` from the bind, or a session without preview frames in 4 s, gives `FALLBACK`: bind again in NORMAL mode without vendor settings. The log line has `sessionType=0x9005|NORMAL` and the state.
- CameraX 1.6.2 -> 1.7.0-alpha03 for all camera artifacts. It deprecates `Camera2Interop.Extender` and `Camera2CameraInfo` (warnings only, still used).
- New unit tests (`InSensorZoomLogicTest`): plan with Android support, vendor parameters, session type, bind fallback. 87 tests in total, all pass. ktlint passes. Builds used `--no-daemon`.

Device test on `7fad170e` (20:40-20:43, the phone on the stand at about 28 cm, the same scene; flag on 1x, 2x, 4x, then off 1x, 2x, 4x, back to back; rotation locked to 0 for the six snapshots):

| Flag | Zoom | Operation mode | `EnableInsensorZoom` | `InSensorZoomState` | `rawCropRegion` | `sensorModeMask` / `sensorModeCache[0]` |
|---|---|---|---|---|---|---|
| on | 1x | CUSTOM (36869) | 1 | (no frame block in this dump) | - | - |
| on | 2x | CUSTOM (36869) | 1 | **2** | **1028 776 2024 1508** (half field) | **48 / 7** |
| on | 4x | CUSTOM (36869) | 1 | 2 | 1028 776 2024 1508 (still the half field) | 48 / 7 |
| off | 1x / 2x / 4x | NORMAL (0) | 0 | 0 | not reported | 0 / 2 |
| Xiaomi app (research) | 2x | CUSTOM (36869) | 1 | 2 | 1028 776 2024 1508 | 48 / 7 |
| Xiaomi app (research) | 4x | CUSTOM (36869), session restarted | 1 | 2 | 1530 1152 1020 756 (quarter) | 96 / 22 |

- At 2x our state is the same as the Xiaomi app's 2x. At 4x we stay in the 2x sensor mode (half-field raw crop, then a digital 2x). The Xiaomi app restarted its session to get the quarter mode.
- Streams in the vendor mode: preview 1600 x 1200 with format `0x22` (PRIVATE; NORMAL: `0x7fa30c06`), JPEG 4080 x 3060. The HAL accepted the JPEG `ImageCapture`, so no YUV path was needed. `cropRegion` stays `0 0 4080 3060`.
- The preview works (screenshot at 2x: normal image, label `zoom 2.00x · torch off · ≈ 28 cm`). Focus data flows (`focused`, 3.56 diopters). Snapshots take 0.7-1.2 s. No fallback, no camera error, no crash. The same PID for the whole run.

Sharpness, 800 x 800 centre crop (Laplacian variance / mean squared gradient):

| Zoom | Off (NORMAL) | On (0x9005) |
|---|---|---|
| 1x | 575.9 / 268.8 | 807.5 / 348.8 |
| 2x | 77.9 / 140.0 | 220.9 / 152.2 |
| 4x | 11.0 / 54.2 | 14.7 / 45.4 |

- The vendor mode also changes the processing: at 1x (no in-sensor crop) the Laplacian variance is 1.4x higher. At 2x it is 2.8x higher, so about 2x comes from the sensor crop.
- Visual check (Read tool, native pixels, 2x nearest-neighbour): at 2x the mounting-hole label has thinner and crisper strokes, and the ring and pad edges are sharper, with the flag on. At 4x both images are soft and about equal.
- Compared with the Xiaomi photos (research): the Xiaomi 2x against a crop zoom from its own 1x gave Laplacian ratios of about 47x, because the Xiaomi still pipeline sharpens and denoises strongly (different scene time and framing). Our on/off ratio at 2x is 2.8x on the plain CameraX JPEG. The physical gain is the same kind (a half-field crop at full density). The rest is processing.

Other observations:

- Each `adb install` stopped our app. While the Xiaomi camera app ran in the background, it opened camera 0 for a few seconds and lost it again to our `am start` (camera events log, 20:28, 20:31, 20:33). I did not close it. Later the user stopped all apps.
- A first "on" run at 20:35 failed (HTTP 000), because the user stopped our app at 20:35:57. The final runs are from 20:40-20:43.
- Images and dumps only in my scratch directory (`.../scratchpad/v9005/`).

End state: zoom 1x, torch off, flag off (`sessionType=NORMAL, state=OFF`), rotation auto, both flips false.

Proposal (no contract change made):

1. **Keep it opt-in for now**, but make it settable through the API instead of an intent extra: for example `POST /v1/camera {"in_sensor_zoom": true}` and `CameraStatus.in_sensor_zoom` (`off`, `on`, `unsupported`, `fallback`). Reasons not to turn it on by default yet: an alpha CameraX, two library-internal workarounds, Xiaomi-specific tags, a different processing at every zoom level, and one test session only.
2. **Where it helps:** 2x (a real half-field crop). From 2x to 4x it gives the 2x sensor crop plus a smaller digital crop, so it is still better than NORMAL, but less at 4x. Below 2x there is no in-sensor crop.
3. **Next step that is worth it:** the quarter-field mode at 4x. The Xiaomi app restarted the session for it. Test: rebind the vendor session while the zoom is already 4x (keep the zoom over the rebind instead of the start state), and check for `rawCropRegion` `1530 1152 1020 756` and mask 96.


### Round: 4x quarter-field mode (part 1)

Brief: get the Xiaomi app's quarter-field mode at 4x (`rawCropRegion 1530 1152 1020 756`, `sensorModeMask 96`) by a rebind at the 4x boundary. Result: **the quarter-field mode is not reachable with our streams, and a rebind at 4x makes it worse. What works: keep the half-field mode from 2x up, also at 4x and 6x.** The automatic rebind is removed again.

What I found on `7fad170e` (vendor session on, `dumpsys media.camera`, complete frame dumps only):

1. I implemented the rebind at the 4x boundary (zoom bands, a 600 ms debounce, through `ControlGate`, keeping zoom and torch). A session that is created at 4x or 6x gives `InSensorZoomState = 0`, the full raw crop `0 4 4080 3052`, and mask 0: no in-sensor zoom at all. Rebinds took 4.8-8.4 s.
2. Probes in one session: zoom 3.9 -> half field (state 2, mask 48). A session created at 1x, then 1x -> 2x -> 4x -> 6x: the half field stays at 4x and 6x. A direct jump 1x -> 5x: state 1, mask 0 (not entered). 1x -> 3x -> 5x: half field at 5x.
3. Rule: **the HAL enters the half-field mode only when the zoom lands in [2x, 4x). After that it keeps the mode above 4x.** The quarter mode (mask 96) never appeared in our session, at any zoom and after any rebind. The Xiaomi app uses 1440 x 1080 YUV streams and a vendor still pipeline, not a 4080 x 3060 JPEG stream. That is the likely condition for the quarter mode. Our snapshot would lose resolution with that stream set, so I did not try it.

Code (final):

- The automatic zoom-band rebind (`Debouncer`, zoom bands, their tests) is **removed**, because it only made 4x worse.
- `InSensorZoomLogic.zoomPath(state, from, to)`: in an `ON` session, a jump from below 2x to 4x or more goes through `ENTRY_RATIO = 3x`, with `ENTRY_SETTLE_MILLIS = 300` between the steps. `updateZoom` and the rebind restore use it. Constants in `Constants.InSensorZoom`.
- Bug found and fixed on the phone: the restore after a rebind read the old state (OFF), so it jumped 1x -> 4x directly. The restore now gets the state of the new session.

Device results (session on, 21:11-21:16):

| Case | `InSensorZoomState` | `rawCropRegion` | mask |
|---|---|---|---|
| On, 1x | 0 or 1 | `0 4 4080 3052` | 0 |
| On, 2x | 2 | `1028 776 2024 1508` (half) | 48 |
| On, 4x (through 2x or 3x) | 2 | half | 48 |
| On, 6x | 2 | half | 48 |
| On, jump 1x -> 5x (with the zoom path) | 2 | half | 48 |
| Turned on at 4x and at 6x (rebind with the zoom path) | 2 | half | 48 |
| Off (NORMAL), 4x | 0 | - | 0 |
| Xiaomi app, 4x (research) | 2 | `1530 1152 1020 756` (quarter) | 96 |

Sharpness, 800 x 800 centre crop (Laplacian variance / mean squared gradient): on 1x 530.2 / 253.8, on 2x 226.3 / 155.5, on 4x 15.5 / 46.7, on 6x 5.0 / 23.4, off 4x 10.4 / 51.6. At 4x, on against off: 1.5x Laplacian variance, about the same gradient. Visual check (native pixels): at 4x the pads are narrower and better separated with in-sensor zoom on. A small, real gain (a 2x digital enlargement of the full-density half field instead of a 4x enlargement of the binned image). It does not match the Xiaomi 4x.

Preview gap and time of a rebind (log `Camera open again, zoom and torch set`): 0.8-1.4 s until the camera is open again with zoom and torch set. Turning in-sensor zoom on takes about 5 s in total, because the app checks the vendor session for 4 s (`SESSION_CHECK_MILLIS`). Turning it off takes about 0.9 s.

### Round: API switch for in-sensor zoom (part 2)

Contract: `POST /v1/camera {"in_sensor_zoom": bool}` and `CameraStatus.in_sensor_zoom` (`off`, `on`, `unsupported`, `fallback`), changed by the orchestrator.

Code:

- `CameraSettingsRequest` with a required strict boolean. `ApiJson` refuses unknown fields.
- `CameraController.inSensorZoomRequested` is the one place of the request (off after an app start). `setInSensorZoom` goes through `ControlGate`. `needsReconfigure`: a new value binds again, and `true` again after a `fallback` tries the vendor session once more. The same value does nothing. `reconfigure` saves zoom and torch, binds again, and sets them back (with the zoom path). `unsupported` (no vendor keys, or Android < 9) returns 200. A failed vendor session gives `fallback` and the NORMAL mode.
- `InSensorZoomState` has the API words. `CameraStatus.in_sensor_zoom` shows the state of the current session.
- `MainActivity`: the intent extra stays as an adb helper and goes through `setInSensorZoom` (keeps zoom and torch). The label observes the zoom and torch of the new `CameraInfo` after a rebind, and removes the old observers.
- New unit tests: `ApiServerTest` +4 (on and off keep zoom and torch, `unsupported` is 200, 6 bad bodies, 503 before the start state and 405 on GET), `InSensorZoomLogicTest` (zoom path, reconfigure rule, state words). 94 tests in total, all pass. ktlint passes. Builds used `--no-daemon`.

Checks on `7fad170e` with curl (local port 18765):

| Request | Result |
|---|---|
| `GET /v1/status` after start | `"in_sensor_zoom":"off"` |
| `{"in_sensor_zoom":true}` at 1x / 4x / 6x | 200, `on`, 4.9-5.4 s, zoom kept |
| `{"in_sensor_zoom":false}` at 6x with torch on | 200, `off`, 0.89 s, zoom 6.0 and torch on kept |
| `{"in_sensor_zoom":false}` again | 200, 0.02 s (no rebind) |
| `{}`, `"true"`, `1`, `null`, an unknown field | 400 `bad_request` |
| `GET /v1/camera` | 405 `method_not_allowed` |

- `unsupported` and `fallback` cannot happen on this phone (it has the keys, and the session works). The unit tests cover them.
- The crash buffer has 2 FATAL entries from 20:43, both from another app (`com.plexapp.android`). None from our app.
- The camera was free during the tests: only our app was a client. A `com.android.camera` process ran in the background but did not open the camera.

End state: zoom 1x, torch off, in-sensor zoom off, rotation auto, both flips false. Images and dumps only in my scratch directory (`.../scratchpad/q4x/`).

Proposal: keep in-sensor zoom off by default. The MCP can turn it on for work at 2x and more. The detail gain is real at 2x, and small at 4x and more. For more detail at 4x the quarter mode needs the Xiaomi stream set (YUV 1440 x 1080), which costs snapshot resolution. I do not recommend that now.


### Round: tap to focus (`POST /v1/focus`)

Contract: `POST /v1/focus` with exactly one pair, `screen_x`/`screen_y` or `snapshot_x`/`snapshot_y` (changed by the orchestrator).

Code:

- `FocusTap.kt` (pure): `FocusRequest` (strict floats), `FocusTapLogic.target` (exactly one complete pair, finite values in [0, 1], else 400), `screenToPreview` (display point -> preview-view point: the preview offset on the display, "outside the preview" -> null, and the view mirroring undone), `snapshotToSurface` (turns a snapshot point back by the snapshot rotation 0/90/180/270).
- `CameraController.focusAt` (through `ControlGate`): a screen point goes through `PreviewView.meteringPointFactory` (it knows the preview crop and the sensor orientation). A snapshot point goes through `SurfaceOrientedMeteringPointFactory(1, 1, imageCapture)` with `getSensorRotationDegrees(targetRotation)`. `FocusMeteringAction` with `FLAG_AF | FLAG_AE` and `setAutoCancelDuration(5 s)`. The response comes at once. A new focus replaces the action. A zoom change calls `cancelFocusAndMetering()` while a hold is active. A rebind (in-sensor zoom) ends it with the old session.
- The preview position comes from the (never mirrored) parent's screen location plus the layout offset, because `getLocationOnScreen` of a mirrored view gives the mirrored corner. The display size is the full display (`maximumWindowMetrics`), like the screen stream.
- Optional focus ring: a white 72 dp ring at the screen point for 0.8 s. It sits outside the preview view, so it is never mirrored, and it shows in the screen stream.
- New unit tests: `FocusTapLogicTest` (7: pairs, range, mapping, flips, offset and bounds, all four rotations with known points) and `ApiServerTest` +3 (screen and snapshot targets, 9 bad bodies plus "outside the preview", 503 and 405). 103 tests in total, all pass. ktlint passes. Builds used `--no-daemon`.

Check on `7fad170e` (APK installed at about 21:5x; the phone now at about 11 cm, focus distance 9.3 diopters; board with a raised heat pipe and shield):

- **The mapping is exact** (AF/AE regions in "Last request sent", active array 4080 x 3060, sensor orientation 90):
  - Snapshot (0.92, 0.80): expected region centre (3264, 245), sent `[2958 15 3570 473]` = centre (3264, 244). Snapshot (0.10, 0.10): expected (408, 2754), sent centre (408, 2754).
  - Screen points: the same x axis. On the other axis the preview crop shows: the display (1280 x 2772) shows only the middle 62% of the 3:4 image width, so screen 0.92 -> image 0.76 and screen 0.10 -> image 0.25. The regions match this.
  - Flip H at screen (0.08, 0.80) and flip V at screen (0.92, 0.20) give the same region as the unflipped screen (0.92, 0.80): `[2958 509 3570 967]`.
- **Focus follows the tap:** each tap gives `scanning` for 0.6-0.9 s, then `focused`. The distances changed between points: 8.06-8.55 diopters for board points, 7.63 on the right side (seen through flip H), against 9.35 with continuous AF. After the 5 s hold the camera went back to continuous AF (9.35). The board is almost flat at this distance, so the near/far differences are small (2-7 mm), and the direction was not always as expected (the heat pipe gave 8.06, the table 8.20).
- **Exposure metering on this HAL is weak or inverted:** metering on the white table gave a brighter snapshot (mean 92.1) than on the black shield (67.6); the snapshot-point and flipped pairs gave almost the same brightness. The regions are right, so the HAL uses the AE region little. `FLAG_AE` stays (contract), but do not expect a strong exposure change from a tap.
- **"outside the preview" does not happen on this phone:** the preview view fills the whole display (also under the status and navigation bars), so (0.5, 0.99) and (0.5, 0.005) return 200. The rule is covered by unit tests for other layouts.
- The focus ring shows at the tap point (screenshot).
- No crash of our app. Images and dumps only in my scratch directory (`.../scratchpad/ftap/`).
- During the test another client changed the zoom (2.25x) and turned in-sensor zoom **on** (it was off before). I set the zoom back to 1x. I left in-sensor zoom on, because a person probably turned it on.

End state: zoom 1x, torch off, flips false, in-sensor zoom `on` (changed by another client during the test, see above).


### Round: highlight boxes (`POST /v1/overlay`)

Contract: `POST /v1/overlay {"boxes": [{snapshot_x, snapshot_y, width, height, label}]}` and `CameraStatus.overlay_boxes` (changed by the orchestrator).

Code:

- `Overlay.kt` (pure): `OverlayRequest`/`OverlayBox` (strict floats, a required string label), `OverlayLogic.validate` (at most 8 boxes, finite values, x and y >= 0, width and height > 0, the box inside the image with a 1e-4 float tolerance, label <= 32 characters), `rescale` (zoom around the centre by zoom now / zoom at the call), `surfaceToImage` (the inverse of the focus mapping), `snapshotToView` (snapshot rotation back to the surface, surface to the upright preview, the PreviewView FILL crop or FIT letterbox, then the preview flips), `boxToView` (null when no part is in the view).
- `OverlayView`: a view with the preview's bounds, above the preview and below the label layer. It is never mirrored. It draws a green 3 dp border and the label (12 sp, white on a dark background, above the box or inside its top edge).
- `CameraController`: the boxes, the zoom at the call, and the 10-minute timer (`Handler`) in one place, through `ControlGate`. `overlayRects` builds the geometry from the camera: zoom now, snapshot rotation, preview rotation (`getSensorRotationDegrees(ROTATION_0)`), the ImageCapture resolution, and the preview scale type and flips. The view redraws on a zoom change, a rotation change, a flip change, and an overlay change. An app start clears the boxes (in memory only).
- New unit tests: `OverlayLogicTest` (7: straight mapping, inverse rotation, landscape snapshot on the portrait preview, flips, zoom rescale, hidden when outside, FILL crop and FIT letterbox on the phone's 1280 x 2772 view) and `ApiServerTest` +3 (set and clear, 12 bad bodies plus the 32-character and 8-box limits, 503 and 405). 113 tests in total, all pass. ktlint passes. Builds used `--no-daemon`.

Check on `7fad170e` (the board moved since the last round; a new scene at about 11 cm):

- Two boxes from a 1x snapshot: "BAT1" around the battery connector (x 0.43-0.55, y 0.455-0.505) and "QR" around a QR label (x 0.65-0.82, y 0.505-0.57). `overlay_boxes` 2.
- Screenshot at 1x: "BAT1" is around the connector. "QR" is around the label at the right edge. The preview shows only the middle 62% of the image width, so it is cut off there.
- Zoom 1x -> 2x: "BAT1" stays around the connector, now twice as large. The QR label moved almost out of the screen; only a thin strip of its box is at the right edge. Correct.
- Flip H at 2x: the preview is mirrored, the box stays around the mirrored connector, and the labels are readable.
- `{"boxes": []}`: `overlay_boxes` 0, no boxes on the screen.
- A snapshot taken while the boxes were on the screen has no box, and it is in the true orientation.
- No crash. Screenshots only in my scratch directory (`.../scratchpad/ovl/`).
- Not done: a sideways (landscape) phone. The mapping handles it (unit test), but the label text is always drawn for portrait, so it is turned for a viewer who holds the phone sideways. The status label turns; the box labels do not.

End state: no boxes, zoom 1x, torch off, flips false. In-sensor zoom was `on` before the test (set by a person). My reinstall reset it to `off`, so I turned it `on` again.

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
