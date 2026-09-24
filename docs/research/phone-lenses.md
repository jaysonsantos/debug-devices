# Phone lenses: is zoom the right way to use the other cameras?

Date of the check: 2026-09-24. Author: dd-research (Claude Code).

Phone: adb serial `7fad170e`, model `2510ERA8BG` (Redmi Note 15 Pro+ 5G, global), product `flourite_eea`, Android API 36, HyperOS `OS3.0.307.0.WPREUXM`.

I used only read-only commands on this one serial: `getprop`, `dumpsys media.camera`, `pm list features`, `pm list libraries`, `dumpsys package dev.jayson.debugdevices.camera`. I did not open a camera, start an app, or send input. At the time of the check, our app (`dev.jayson.debugdevices.camera`) had camera ID 0 open.

## Summary

- The phone has **two rear cameras**: a 200 MP main camera and an 8 MP ultrawide. It has **no telephoto and no macro camera**.
- Third-party apps see **only two cameras**: ID `0` (rear main) and ID `1` (front). The ultrawide (ID `2`), the logical rear multi-camera (ID `3`), and two vendor system cameras (IDs `4`, `5`) are hidden.
- Our app opens ID `0`. Its zoom range 1.0-10.0 is **digital zoom on the main camera only**. No lens switch occurs, and no lens switch is possible for our app.
- Thus, zoom is the correct and the only control that we have. But our zoom is only a crop of the 12.5 MP binned image. It does not add detail.
- For PCB work, move the phone close (10-12 cm, the minimum focus distance is about 10 cm). That gives 2x more detail than 2x zoom at 20 cm.
- Two options can add real value: (A) set the autofocus to the **MACRO** mode for close work, and (B) try the vendor **in-sensor zoom** of the 200 MP sensor. That can give real detail up to 4x. Neither needs a new `/v1/lens` endpoint.

## 1. Physical cameras

### Evidence

`dumpsys media.camera`, service part:

```text
Number of camera devices: 6
Number of normal camera devices: 2
Number of public camera devices visible to API1: 2
    Device 0 maps to "0"
    Device 1 maps to "1"
Active Camera Clients:
(Camera ID: 0, ... Client Package Name: dev.jayson.debugdevices.camera, ...)
```

`getprop`:

- `persist.vendor.camera.mi.module.info = back_main=0xD1;back_ultra=0x37;`
- `persist.vendor.camera.mi.module.infoext = front_main=0xFC;`
- The sensor module files (`persist.vendor.camera.modulename{0,1,2}.info`, with each character shifted by one) decode to:
  - `com.qti.sensormodule.flourite_ofilm_s5khpe_wide_i` (rear main, Samsung ISOCELL HP-series 200 MP)
  - `com.qti.sensormodule.flourite_ofilm_ov32d40_front_i` (front)
  - `com.qti.sensormodule.flourite_aac_ov08f10_ultra_i` (rear ultrawide, OmniVision OV08F10)

The module list has no telephoto sensor and no macro sensor.

### Static data per camera ID (HAL `vendor_qti/<id>`)

| ID | Visible to apps | Facing | Role | Focal length | Equivalent (35 mm) | Aperture | Sensor size | Output array | Min focus distance | AF modes | Zoom ratio range |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | **yes** | back | main (200 MP, 4-to-1 binned output) | 6.07 mm | 23 mm | f/1.7 | 9.14 x 6.85 mm (1/1.4") | 4080 x 3060 (12.5 MP) | **10 diopters = 10 cm** (APPROXIMATE) | OFF, AUTO, **MACRO**, CONTINUOUS_VIDEO, CONTINUOUS_PICTURE | 1.0-10.0 |
| 1 | yes | front | front | 2.58 mm | - | f/2.2 | 4.00 x 3.00 mm | 3264 x 2448 | fixed focus | OFF | 1.0-10.0 |
| 2 | **no** | back | ultrawide | 1.67 mm | 16 mm | f/2.2 | 3.66 x 2.74 mm | 3264 x 2448 (8 MP) | fixed focus (hyperfocal 1.77 diopters) | OFF | 1.0-10.0 |
| 3 | **no** | back | logical multi-camera, physical IDs `2` and `0` | 6.07 mm | 23 mm | f/1.7 | as ID 0 | 4080 x 3060 | 10 cm | as ID 0 | **1.0-10.0** |
| 4, 5 | **no** | external | vendor system cameras (`SYSTEM_CAMERA` capability) | - | - | - | - | 12000 x 9000 | - | - | 0.1-100.0 |

More facts for ID 0:

- `org.codeaurora.qcamera3.quadra_cfa.is_qcfa_sensor = 1`, `qcfa_dimension = 16320 x 12240` (the full 200 MP array).
- `android.request.availableCapabilities` has no `ULTRA_HIGH_RESOLUTION_SENSOR`. The largest JPEG stream for apps is 4080 x 3060. Apps do not get the 200 MP mode through the public API.
- `android.scaler.availableMaxDigitalZoom = 10.0`.
- The API1 parameters give `horizontal-view-angle: 73.9461`. That agrees with 6.07 mm on a 9.14 mm wide sensor.

Public specification (for comparison): 200 MP main, f/1.7, 23 mm, 1/1.4", optical image stabilization. 8 MP ultrawide, f/2.2, 15 mm, 120 degrees. No telephoto, no macro camera. Sources: [GSMchoice](https://www.gsmchoice.com/en/catalogue/redmi/note-15-proplus-5g/), [xiaomitime](https://xiaomitime.com/smartphones/redmi-note-15-pro-2/). The HAL data agrees.

## 2. Does zoom switch lenses?

**Not in our app.**

- `CameraController.kt` binds `CameraSelector.DEFAULT_BACK_CAMERA`. CameraX takes the first back camera in the list that the system gives to the app. That list has only `0`. The dump confirms that our app has camera ID `0` open.
- ID `0` is one physical camera, not a logical multi-camera. Its `CONTROL_ZOOM_RATIO_RANGE` is 1.0-10.0, and `availableMaxDigitalZoom` is 10.0. `setZoomRatio` only crops. It never selects the ultrawide.
- The dump of our session shows `android.control.zoomRatio = 3.3`, `android.scaler.cropRegion = [0 0 4080 3060]`, and the vendor session parameter `org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom = 0`. Thus the 3.3x image is a crop of the 12.5 MP binned frame, scaled up.

Why no zoom below 1.0 (no ultrawide):

1. The only lens switch in the HAL is the logical camera ID `3` (physical `2` + `0`). But its zoom range is also 1.0-10.0. It does not go below 1.0 through the public zoom API.
2. ID `3` and ID `2` are **hidden from third-party apps**: "Number of normal camera devices: 2". This is the vendor (Xiaomi/HyperOS) configuration. It is not a CameraX setting. No `vendor.camera.aux.packagelist` property is set (on some Xiaomi builds, that property lets named apps see the auxiliary cameras).
3. The Xiaomi camera app uses a vendor path: the tag `com.xiaomi.camera.videosat.zoomRange = [0.6 10.0]` (SAT = the vendor's multi-camera zoom) gives it 0.6x.

## 3. Is a macro lens available?

**No. The phone has no macro camera.**

- The sensor module list has only `back_main`, `back_ultra`, and `front`.
- Xiaomi "macro" on this phone is a feature of the main camera: `xiaomi.capabilities.macro = 1` and `xiaomi.capabilities.macro_zoom_feature = 1` on ID 0, and `xiaomi.MacroMode` is a vendor request tag. It is a close-focus plus crop mode of the main camera in the Xiaomi camera app.
- Other ways that I checked:
  - A separate camera ID: none for macro.
  - Camera2 `setPhysicalCameraId`: only on a logical camera. ID `0` is not logical, and the logical ID `3` is hidden.
  - CameraX Extensions: the phone has the vendor library (`pm list libraries`: `androidx.camera.extensions.impl`, `camerax-vendor-extensions.jar`). The CameraX extension modes are AUTO, BOKEH, HDR, NIGHT, and FACE_RETOUCH. None is macro or lens selection.
- What our app can use: the standard AF mode `CONTROL_AF_MODE_MACRO` (value 2) on ID 0. It is in `android.control.afAvailableModes = [0 1 2 3 4]`.

## 4. What should our app do?

| Option | Effort | Risk | Gain for PCB inspection |
|---|---|---|---|
| Keep zoom only (no change) | 0 | none | The zoom is a crop. It helps to frame a part, but it adds no detail. |
| CameraX `CameraSelector` for a specific camera ID | 0.5 day | The only other rear camera (ID 2) is hidden. `DEFAULT_BACK_CAMERA` already gives ID 0. | **None** on this phone |
| Camera2 interop `setPhysicalCameraId` | 1-2 days | Needs a visible logical camera. ID 3 is hidden. | **None** on this phone |
| New endpoint `POST /v1/lens` (contract change) | 1-2 days + contract | Nothing to switch to on this phone | **None** on this phone. On a phone with a visible telephoto, it would help. |
| **A. AF MACRO mode for close work** (recommended) | 0.5 day | Low. A standard Camera2 key through `Camera2Interop.Extender.setCaptureRequestOption(CONTROL_AF_MODE, MACRO)`. Also add a tap-to-focus at the center (`FocusMeteringAction`). | Faster and more reliable focus at 10-15 cm. That is where the detail is. |
| **B. Vendor in-sensor zoom** (try it) | 1-2 days, with a test on this phone | Medium. A vendor session parameter (`org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom`, int32). The HAL can ignore it for third-party apps, or it can change the session start. It must be behind a setting with a fallback. | If it works: at 2x-4x, the crop comes from the full 16320 x 12240 array. This gives up to 4x more real detail than now. This is the only "tele" that the phone has. |

**Recommendation:**

1. **Option A first.** Keep `/v1/zoom` as it is. Add AF MACRO mode and a center focus trigger. No contract change is necessary. Optional: add `"focus_mode"` to `CameraStatus` later.
2. **Option B as an experiment.** Set the vendor session parameter with Camera2 interop, then compare 4080 x 3060 snapshots at 2x and 4x with it on and off (the same board, the same distance). Keep it only if the images show more detail. The zoom API stays the same. Only the image quality at zoom > 1 changes.

Do not add `POST /v1/lens` for this phone. Add it only if a later target phone shows a telephoto or macro camera in its visible camera list.

## 5. Close focus against zoom

Numbers for ID 0 (thin-lens estimate, f = 6.07 mm, sensor width 9.14 mm, output width 4080 px; the distance is from the lens, and the minimum focus distance is "APPROXIMATE"):

| Distance | Field of view (4:3) | Detail at 1x | Upper limit with in-sensor zoom (full 200 MP array) |
|---|---|---|---|
| 10 cm (minimum focus) | 141 x 106 mm | 29 px/mm (35 µm per pixel) | 115 px/mm |
| 12 cm | 172 x 129 mm | 24 px/mm (42 µm) | 95 px/mm |
| 15 cm | 217 x 163 mm | 19 px/mm (53 µm) | 75 px/mm |
| 20 cm | 292 x 219 mm | 14 px/mm (72 µm) | 56 px/mm |
| 30 cm | 443 x 332 mm | 9 px/mm (108 µm) | 37 px/mm |

Comparison (same framing, about 145 mm wide):

- 10 cm at 1x: 29 px/mm.
- 20 cm at 2x (our crop zoom): 14 px/mm. **Half the detail.**
- 30 cm at 3x (our crop zoom): 9 px/mm. **One third of the detail.**

Thus, with the current app: **move close first, then use zoom only to frame.** Crop zoom never adds detail. At 10 cm and 4x crop zoom, the field is 35 mm wide, but the detail stays at 29 px/mm.

What this means for parts (at 10-12 cm, 1x, 24-29 px/mm):

- 0402 resistor (1.0 x 0.5 mm): about 24-29 x 12-14 px. Easy to find.
- 0201 (0.6 x 0.3 mm): about 15-17 x 7-9 px. Visible, but no detail.
- IC marking with 0.4 mm character height: about 10-12 px high. Readable only in good light and sharp focus.
- The 200 MP numbers are an upper limit. The 0.56 µm pixels, the remosaic, and the lens give less. A real gain of 2x is a fair expectation if Option B works.

Practical rules for this phone:

- Hold the phone at **10-12 cm**. Closer than 10 cm, the main camera cannot focus. The ultrawide cannot help: it is fixed-focus, sharp only from about 28 cm, hidden from apps, and has only 8 MP.
- Use the torch at this distance. The phone body shades the board.
- Use AF MACRO or a center focus trigger after each move (Option A).
- Use zoom 1.5-3x only to frame the area of interest in the snapshot, not to get detail.

## Sources

- `adb -s 7fad170e shell dumpsys media.camera` (81,082 lines; the facts above come from the service part, the HAL static information of IDs 0-5, and the dynamic information of device 0).
- `adb -s 7fad170e shell getprop` (camera module properties).
- `adb -s 7fad170e shell pm list features` and `pm list libraries`.
- `android/app/src/main/java/dev/jayson/debugdevices/camera/CameraController.kt` (`CameraSelector.DEFAULT_BACK_CAMERA`, `setZoomRatio`).
- Public specification: [GSMchoice, Redmi Note 15 Pro+ 5G 2510ERA8BG](https://www.gsmchoice.com/en/catalogue/redmi/note-15-proplus-5g/), [xiaomitime](https://xiaomitime.com/smartphones/redmi-note-15-pro-2/).
