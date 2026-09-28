# Samsung Galaxy S22 lenses: what our app gets, and how to use it for PCB work

Date of the check: 2026-09-27. Author: dd-android (Claude Code).

Phone: Samsung Galaxy S22 (SM-S901B), Android 16 (API 36), over Wi-Fi adb. Our app 0.1.0 had the camera open. I used only read-only `dumpsys media.camera` and the app API (`/v1/zoom`, `/v1/snapshot`, `/v1/status`, `/v1/camera`). Images and dumps are only in a local scratch directory.

## Summary

- Our app opens camera `0`. It is a **logical multi-camera** of three physical back cameras: `"2"` (ultrawide), `"5"` (wide), and `"6"` (telephoto). The app zoom range is 0.6-10.
- **The zoom switches lenses by itself.** 0.6x uses the ultrawide. 1x to 5x use the wide, with a crop. The telephoto came in only once, at 10x. A second pass at 5-10x stayed on the wide. The HAL chooses the lens by itself, and at a short subject distance it mostly keeps the wide.
- **3x is not the telephoto** on this phone: it is a crop of the wide. The telephoto has about 2.9x the wide's focal length, but the HAL does not use it at 3x.
- **The telephoto cannot focus closer than 50 cm.** The wide focuses from 10 cm. The ultrawide has fixed focus. So for close PCB work only the wide counts.
- **Recommendation:** hold the phone at 10-12 cm, use zoom 1x to frame the area, and zoom 2-3x only to frame a part. Do not go below 1x (the ultrawide cannot focus near a board), and do not expect the telephoto at close range.

## 1. Cameras

`dumpsys media.camera`: 4 camera devices, 3 normal ones. Apps see `0` (back, logical), `1` and `3` (front). Camera `0`:

- `android.request.availableCapabilities` has `LOGICAL_MULTI_CAMERA`.
- `android.logicalMultiCamera.physicalIds`: `"5"`, `"6"`, `"2"`.
- `android.control.zoomRatioRange`: 0.6-10.
- `android.control.afAvailableModes`: OFF, AUTO, CONTINUOUS_VIDEO, CONTINUOUS_PICTURE. **No MACRO.**
- No Qualcomm in-sensor zoom keys.

Physical cameras (from "Physical camera N characteristics" of camera 0, and the HAL static data):

| Physical | Role | Focal length | Aperture | Sensor size | Output pixels | Minimum focus distance |
|---|---|---|---|---|---|---|
| `"2"` | ultrawide | 2.2 mm | - | 5.64 x 4.23 mm | 4032 x 3024 | fixed focus (0 diopters, uncalibrated) |
| `"5"` | wide (main) | 5.4 mm | f/1.8 | 8.16 x 6.12 mm | 4080 x 3060 | 10 diopters = **10 cm** (calibrated) |
| `"6"` | telephoto | 7.0 mm | f/2.4 | 3.65 x 2.74 mm | 3648 x 2736 | 2 diopters = **50 cm** (calibrated) |

The telephoto's field of view is about 2.9x narrower than the wide's field ((7.0 / 3.65) / (5.4 / 8.16)).

The phone also has hidden logical cameras (for example `20`, `21`) that apps cannot open.

## 2. Zoom levels in our app

"Latest received frame" of `dumpsys media.camera`, the phone at close range (the wide focus distance at its 10 cm limit):

| Zoom | `activePhysicalId` | `lens.focalLength` | `scaler.cropRegion` | `lens.focusDistance` |
|---|---|---|---|---|
| 0.6x | `"2"` (ultrawide) | 2.2 mm | `0 0 4080 3060` | 0 (fixed) |
| 1x | `"5"` (wide) | 5.4 mm | `0 0 4080 3060` | 10.0 |
| 2x | `"5"` | 5.4 mm | `1020 765 2040 1530` | 10.0 |
| 3x | `"5"` | 5.4 mm | `1360 1020 1360 1020` | 10.0 |
| 5x | `"5"` | 5.4 mm | `1632 1224 816 612` | 10.0 |
| 10x (first pass) | `"6"` (telephoto) | 7.0 mm | `1836 1377 408 306` | 5.2 |
| 6x-10x (second pass) | `"5"` | 5.4 mm | - | 10.0 or -1 |

- All snapshots are 4080 x 3060 at every level.
- The lens switch depends on the scene: the HAL can move to the telephoto at high zoom, but not reliably at close range. At 10x on the telephoto the focus distance was 5.2 diopters (19 cm), closer than its 50 cm minimum, so that image cannot be sharp at close range.

## 3. Detail at 3x

The same scene, 800 x 800 centre crop, the variance of the Laplacian / mean squared gradient. "1x crop" = the centre of the 1x snapshot, enlarged to the same size (Lanczos), that is a pure digital zoom:

| Zoom | Real snapshot | 1x crop enlarged |
|---|---|---|
| 2x | 109.5 / 171.1 | 107.0 / 186.9 |
| 3x | 47.3 / 102.5 | 29.4 / 106.8 |

- 2x: the same detail as a digital crop.
- 3x: the real snapshot looks crisper (a component marking and the pad edges are sharper; the Laplacian variance is 1.6x higher, the gradient is the same). It still comes from the wide (5.4 mm), so this is not optical. It is image processing, or a sensor readout at a higher density; the dump does not show which.

## 4. Detail by distance (thin-lens estimate)

Detail in px/mm = output width x focal length / (sensor width x distance). Zoom does not add optical detail: it crops.

| Distance | Wide (`"5"`) | Telephoto (`"6"`) | Ultrawide (`"2"`) |
|---|---|---|---|
| 10 cm | 27 px/mm | cannot focus | not sharp (fixed focus) |
| 12 cm | 22 px/mm | cannot focus | not sharp |
| 15 cm | 18 px/mm | cannot focus | not sharp |
| 20 cm | 13 px/mm | cannot focus | 8 px/mm |
| 30 cm | 9 px/mm | cannot focus | 5 px/mm |
| 50 cm | 5 px/mm | 14 px/mm | 3 px/mm |

- The telephoto is better than the wide only from 50 cm on. At that distance it gives 14 px/mm, and the wide at 10-12 cm gives about twice that.
- In our app the lens choice is the HAL's, not the app's. The app has no lens switch.

## 5. Recommendation for close PCB work on the S22

1. **Distance first:** hold the phone at 10-12 cm. `CameraStatus.focus.distance_diopters` must be a little under 10 (for example 8-9.5). A value of exactly 10.0 means that the wide is at its limit: the board can be too close, so move the phone back a little.
2. **Zoom 1x** to see the area. **Zoom 2-3x** to frame one part or read a marking; 3x looks a little crisper than a digital crop. **Above 5x** there is no gain at close range, and the HAL can switch to the telephoto, which cannot focus there.
3. **Never below 1x** for board work: 0.6x is the ultrawide with fixed focus.
4. `in_sensor_zoom` returns `unsupported` and `af_mode: macro` stays `continuous` on this phone (no vendor keys, no MACRO mode). Both are correct and need no action.
5. Use tap to focus (`/v1/focus`) on the part after each move.

## Sources

- `dumpsys media.camera` on the S22 (service part, "Physical camera N characteristics" of camera 0, HAL static information, "Latest received frame" at each zoom level).
- Snapshots from `/v1/snapshot` at 0.6x, 1x, 2x, 3x, 5x, and 10x (scratch only).
- `docs/research/phone-lenses.md` (the thin-lens estimate and the method, for the earlier phone).
