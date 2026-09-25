# Xiaomi camera app zoom compared with our app

Date: 2026-09-25, 20:11-20:17. Author: dd-research (Claude Code).

Phone: adb serial `7fad170e` only. The user opened the Xiaomi camera app (`com.android.camera/.Camera`) and allowed input to it for this task. The phone stayed still and pointed at the laptop board (the same board as in `docs/reports/dd-android.md`). The lens focus distance at 1x was 3.41 diopters (about 29 cm).

What I did:

- I read the zoom buttons with `uiautomator dump` and tapped them with `input tap`.
- For levels between the buttons, I used `input swipe` on the zoom buttons. A sideways swipe opens the zoom dial.
- After each level: `screencap`, `dumpsys media.camera`, and one photo with the shutter button.
- I did not change settings, delete files, or send input to other apps or devices. At the end, I set the app to 1x.

## Summary

- **Lens switch:** only at **0.6x**. There the Xiaomi app opens **camera ID 2** (the ultrawide, 1.67 mm). That camera is hidden from third-party apps. From 1x to 14.6x it uses **camera ID 0** (the main camera). It never opens the logical camera 3. The phone has **no telephoto**.
- **In-sensor zoom:** **yes, at 2x and 4x.** On camera 0 the HAL reports `InSensorZoomState = 2` and a new sensor mode. The RAW crop is a half (2x) or a quarter (4x) of the field. That is a crop from the full 200 MP array, not an upscaled crop. Above 4x, the RAW crop stays at the 4x region, and the rest is digital zoom.
- **Detail:** on the same scene, the Xiaomi 2x and 4x photos show much more detail than a crop zoom from 1x, which is what our app does. At 4x, the label next to a mounting hole is readable in the Xiaomi photo. It is not readable in the crop zoom. At 10x there is almost no more gain.
- **Why our app does not get it:** our session has operation mode `NORMAL (0)`. The Xiaomi app uses the vendor operation mode `CUSTOM (36869)` = `0x9005`. Both set `EnableInsensorZoom = 1`, but only the vendor mode switches the sensor.
- **Advice:** "distance gives detail; zoom does not" stays correct **for our app**. For the phone itself, the Xiaomi camera app gets real detail up to about 4x. Section 5 gives two paths for our app.

## 1. Zoom controls in the Xiaomi app

`uiautomator dump` (Photo mode, portrait UI, screen 1280 x 2772):

| Control | content-desc | Bounds |
|---|---|---|
| 0.6x button | `0.6X zoom` | `[323,1880][513,2071]` |
| 1x button | `1.0X zoom` | `[513,1880][640,2071]` |
| 2x button | `2.0X zoom` | `[640,1880][767,2071]` |
| 4x button | `4.0X zoom` | `[767,1880][957,2071]` |
| Shutter | `Shutter button` | `[491,2263][788,2560]` |

- A sideways swipe on the buttons opens a dial. From the 4x button, a 562 px swipe to the left gave 14.6x. Swipes of 120 px to the right gave 12.0x, 9.9x, and 7.6x. A 122 px swipe to the left then gave exactly 10.0x.
- The largest value that I saw was 14.6x. The HAL tag `com.xiaomi.scaler.availableCaptureMaxZoomRatio` says 30x.

## 2. Camera state per level (`dumpsys media.camera`)

"Frame" values come from "Latest received frame". Session values come from "Stream configuration".

| Level | Camera ID open | Focal length | `control.zoomRatio` | `scaler.rawCropRegion` (left, top, width, height) | `InSensorZoomState` | `xiaomi.superResolution.inSensorZoomState` | `com.xiaomi.SensorMode.sensorModeMask` / `sensorModeCache[0]` | `sensor.pixelMode` |
|---|---|---|---|---|---|---|---|---|
| 0.6x | **2** (ultrawide) | 1.67 mm | 1.0 (on camera 2) | - | 0 | 0 | 0 / 0 | DEFAULT |
| 1x | 0 (main) | 6.07 mm | 1.0 | `0 4 4080 3052` (full field) | 0 | 0 | 0 / 6 | DEFAULT |
| 2x | 0 | 6.07 mm | 2.0 | `1028 776 2024 1508` (**half field**) | **2** | **2** | **48 / 7** | DEFAULT |
| 4x | 0 (session restarted) | 6.07 mm | 4.0 | `1530 1152 1020 756` (**quarter field**) | **2** | **2** | **96 / 22** | DEFAULT |
| 10x | 0 | 6.07 mm | 10.0 | `1530 1152 1020 756` (same as 4x) | 2 | 2 | 96 / 22 | DEFAULT |
| 14.6x | 0 | 6.07 mm | 14.6 | `1530 1152 1020 756` (same as 4x) | 2 | - | 96 / - | DEFAULT |

At all levels:

- `EnableInsensorZoom = 1`, `org.quic.camera.inSensorZoom.SensorSwitched = 0`, `xiaomi.superResolution.enabled = 0`.
- `android.scaler.cropRegion` is the full active array (`0 0 4080 3060`, or `0 0 3264 2448` on camera 2).
- The session operation mode is `CUSTOM (36869)`. The streams are one 1440 x 1080 preview and two 1440 x 1080 YUV readers. The still comes from the vendor pipeline, not from a JPEG stream.
- Only one client was active: `com.android.camera`. Our app was not running.

What the raw crop means (with the RAW crop region documentation in `docs/research/zoom-docs-claude.md`): at 2x the RAW frame covers half the field width. At 4x it covers a quarter. With the sensor mode change and `InSensorZoomState = 2`, the sensor reads the centre at full density (unbinned) and does not upscale a binned image. A 200 MP array is 4x the binned width (16320 / 4080), so 4x is the limit of this method. That agrees with the unchanged RAW crop above 4x.

Compare our app (`dd-android.md`, in-sensor zoom rounds): operation mode `NORMAL (0)`, `EnableInsensorZoom = 1` or `0`, and `InSensorZoomState = 0` at 1x, 2x, and 4x.

## 3. Photos

New files on the phone. **Delete them on the phone if you do not want them.** I did not delete any file:

| Level | File on the phone (`/sdcard/DCIM/Camera/`) | Size in pixels | EXIF focal length (35 mm equivalent) | ISO | Exposure |
|---|---|---|---|---|---|
| 0.6x | `IMG_20260925_201324.jpg` | 2448 x 3264 | 1.67 mm (15 mm) | 800 | 1/30 s |
| 1x | `IMG_20260925_201426.jpg` | 3060 x 4080 | 6.07 mm (23 mm) | 1000 | 1/25 s |
| 2x | `IMG_20260925_201450.jpg` | 3060 x 4080 | 6.07 mm (46 mm) | 1600 | 1/33 s |
| 4x | `IMG_20260925_201519.jpg` | 3060 x 4080 | 6.07 mm (92 mm) | 1250 | 1/33 s |
| 10x | `IMG_20260925_201704.jpg` | 3060 x 4080 | 6.07 mm (230 mm) | 2000 | 1/33 s |

- EXIF `Model`: `REDMI Note 15 Pro+ 5G` (our app writes `2510ERA8BG`). No EXIF digital zoom tag.
- The Xiaomi app gives the same output size as our app (12.5 MP) at every level of the main camera. It writes the zoom only as the 35 mm equivalent focal length.
- Copies, screenshots, and dumps are only in my scratch directory (`.../scratchpad/xz/`). The frames show the bench, and one older frame shows part of a person. They are not in the repository.

## 4. Detail compared with our app

### A direct comparison is not possible

Our last snapshots (`isz2`, 19:44, `dd-android.md`) show another view: the phone was moved after them (checked by eye on a side-by-side overview). The Xiaomi photos are also portrait, and ours are landscape. The Xiaomi app was open, and the brief says not to close it, so I did not take new snapshots with our app.

### Same-scene test

Our app's zoom is a crop of the 1x frame: the zoom-ratio crop with no sensor change (`dd-android.md`, `zoom-docs-claude.md`). So I made "our 2x/4x/10x" from the Xiaomi 1x photo: a centre crop of 1/z, scaled back to 3060 x 4080 (Lanczos). I compared it with the Xiaomi photo at the same level. I used the same measure as the earlier rounds: an 800 x 800 centre crop, the variance of the Laplacian, the mean squared gradient, and a new high-frequency share (spectral energy above 25% of the Nyquist frequency).

| Level | Crop zoom from 1x (our method): Laplacian var / gradient / HF share | Xiaomi app: Laplacian var / gradient / HF share | Xiaomi / crop |
|---|---|---|---|
| 2x | 27.4 / 55.0 / 0.021 | 1278.3 / 455.0 / 0.308 | 47x / 8.3x / 14.5x |
| 4x | 3.1 / 9.7 / 0.008 | 144.4 / 76.2 / 0.093 | 46x / 7.8x / 11.8x |
| 10x | 1.2 / 0.9 / 0.017 | 3.8 / 2.4 / 0.018 | 3.2x / 2.5x / 1.0x |

The ratios are too large to take as numbers. The Xiaomi pipeline sharpens and denoises the image, and the framing is not exactly the same (the correlation of the two crops is 0.66 at 2x and 0.40 at 4x). What counts is the visual check (Read tool, the same crops side by side):

- **1x (Xiaomi, native pixels):** in focus. the board silkscreen text is readable. The small label next to the mounting hole is not.
- **2x:** the crop zoom is soft. The via rings run together, and the hole label is a blur. The Xiaomi 2x shows separate via rings, sharp silkscreen, and the readable hole label.
- **4x:** the crop zoom shows only soft shapes, and the label is not readable. The Xiaomi 4x shows the hole label clearly, the knurled edge of the mounting hole, and the solder of single vias.
- **10x:** both are soft. The Xiaomi image has a little more edge detail and processing artefacts. Above 4x there is no real gain.

Conclusion: at the same distance (about 29 cm), the Xiaomi app's 2x and 4x give **real extra detail**, up to about 4x the linear resolution of a crop zoom. Our app cannot get this today.

## 5. What this means for us

### Answers

| Question | Answer |
|---|---|
| Lens switch below 1x? | Yes. At 0.6x, camera ID 2 (the ultrawide), which is hidden from third-party apps. |
| A telephoto? | No. All levels from 1x up use camera 0. |
| In-sensor zoom at 2x? | Yes (`InSensorZoomState = 2`, half-field RAW crop, new sensor mode). Also at 4x (quarter field). Not above 4x. |
| Gain over our app at 2x and 4x? | Large and visible: labels and via rings that the crop zoom blurs are readable. The metric ratios are 8-47x, but they are inflated by the sharpening. The physical limit is 2x (at 2x) and 4x (at 4x) in linear detail. |

### The advice "distance gives detail; zoom does not"

- **For our app: still correct.** Our zoom is a crop, and the measurements did not change.
- **For the phone: not correct.** The phone's own camera app gets real detail up to 4x without moving the phone.
- Proposed wording for rule 6 (replaces the proposal in `zoom-docs-claude.md`):

  > Phone zoom in our camera app is a digital crop on the current phone: it frames the area but adds no detail. When a marking or a small part is too small to read, ask the user to move the phone closer (near the minimum focus distance, about 10-12 cm), then use zoom only to frame. With our app, distance gives detail and zoom does not.

### Paths for our app (at most two)

1. **Recommended experiment: vendor session type `0x9005`.**
   - Open the session with the operation mode that the Xiaomi app uses (`SessionConfiguration` with session type `0x9005`), keep `EnableInsensorZoom = 1`, and then check `InSensorZoomState` and the RAW crop at 2x and 4x.
   - CameraX 1.6.2 cannot set a session type. CameraX 1.7 adds `SessionConfigurationInterop.setSessionType(int)` (API 28; `camera-camera2` API file `1.7.0-rc01`). Plain Camera2 can pass the value in `SessionConfiguration`.
   - Android documents only regular and high-speed session types. A vendor value is not documented, and the vendor mode can need more Xiaomi tags, give other streams, or fail.
   - Effort: 1-2 days with a device test and a fallback to `NORMAL`. The gain, if it works: up to 4x real detail at the same distance, with no contract change.
2. **Official, but heavy: the Xiaomi HyperOS Camera Engine SDK** (see `zoom-docs-claude.md`). It gives the ultrawide and the SAT zoom modes officially. It needs an enterprise developer account and Xiaomi's authorization, and this phone is not in its device list. I do not recommend it for this project now.

Not a path: telling the agent to use photos from the Xiaomi app. The evidence rules allow device data only through the MCP tools.

## Evidence files (scratch only, not in the repository)

`.../scratchpad/xz/`: `ui0.xml` and `ui_*.xml` (UI dumps), `screen_*.png` (screenshots), `mc_*.txt` (`dumpsys media.camera` per level), `xiaomi_*_IMG_*.jpg` (the pulled photos), `pair_sim_*x.png` (crop zoom against the Xiaomi image), `extract.py`, `level.sh`, `shoot.sh`, `cmp.py`.
