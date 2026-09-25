# Is phone zoom only a crop? What the official documentation says

Date of the check: 2026-09-25. Author: dd-research (Claude Code).

The question: "About the crops, did you check Xiaomi or Android docs on that?" The earlier findings (`docs/research/phone-lenses.md`, and the "in-sensor zoom experiment" rounds in `docs/reports/dd-android.md`) came from the phone: `dumpsys media.camera`, crop regions, and sharpness tests. This file checks them against primary documentation.

Sources that I read:

- AOSP `platform/system/media`, `camera/docs/metadata_definitions.xml` (branch `main`, read 2026-09-25). The developer.android.com reference text of each `CaptureRequest`, `CaptureResult`, and `CameraCharacteristics` key comes from this file. The quotes below are from it.
- developer.android.com: the Camera2 reference, the Android 12 feature page, and the CameraX release notes.
- source.android.com: the multi-camera guide, and the Android 16 CDD (section 2.2.7.2).
- The androidx source (`androidx/androidx` on GitHub): the CameraX zoom code and API files.
- The Xiaomi HyperOS developer platform (`dev.mi.com`): the "Camera Engine" (相机引擎) pages.
- A new read-only `adb -s 7fad170e shell dumpsys media.camera` and `getprop`, to check specific keys.

## Summary

- **Android documentation confirms our main finding.** On one physical camera, `CONTROL_ZOOM_RATIO` and `SCALER_CROP_REGION` are digital zoom. Optical zoom occurs only when a logical multi-camera switches lenses. Our app uses camera `0`, which is one physical camera.
- **The documentation also says that the HAL can add detail at zoom by itself**, with an "in-sensor crop" or with "un-bin and remosaic" at some zoom levels. This is optional for the device, and the app cannot request it through `CONTROL_ZOOM_RATIO`. On this phone, our measurements showed no such gain. Thus "zoom never gives detail" is too strong as a general rule, but it is correct for this phone today.
- **The public full-resolution path exists** (`SENSOR_PIXEL_MODE = MAXIMUM_RESOLUTION`, Android 12, API 31). **Camera 0 on this phone does not support it.** It has no `ULTRA_HIGH_RESOLUTION_SENSOR` capability, `android.sensor.pixelMode` is not a request key, and the HAL gives no values for the maximum-resolution stream map. `CROPPED_RAW` is also not in its stream use cases.
- **CameraX** calls `CONTROL_ZOOM_RATIO` (Android 11+). It has no in-sensor-zoom API. Its only sensor pixel mode API (`OutputConfigurationInterop.addSensorPixelModeUsed`) is new in CameraX 1.7. We use 1.6.2, and the API cannot help on this phone.
- **Xiaomi documents one official path**: the HyperOS "Camera Engine" SDK. It gives third-party apps the single lenses (ultrawide, main, telephoto, macro) and SAT zoom. It needs a company account and an authorization from Xiaomi, and its device list does not name this phone. The documentation does not say anything about in-sensor zoom or third-party access through the normal Camera2 list.
- **Qualcomm**: I found no public documentation for the vendor tag `org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom`. Its name comes only from the phone's vendor tag list.
- **Proposed rule text** (section 6): keep the advice, but say "on this phone" and "unless a test shows otherwise".

## 1. Camera2 zoom: `CONTROL_ZOOM_RATIO`, `SCALER_CROP_REGION`

### `SCALER_CROP_REGION` (API 21)

Reference: [CaptureRequest#SCALER_CROP_REGION](https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#SCALER_CROP_REGION)

> The desired region of the sensor to read out for this capture.
>
> This control can be used to implement digital zoom.

> Output streams use this rectangle to produce their output, cropping to a smaller region if necessary to maintain the stream's aspect ratio, then scaling the sensor input to match the output's configured resolution.

The same entry permits a hidden in-sensor crop:

> The camera sensor output aspect ratio depends on factors such as output stream combination and android.control.aeTargetFpsRange, and shouldn't be adjusted by using this control. And the camera device will treat different camera sensor output sizes (potentially with in-sensor crop) as the same crop of android.sensor.info.activeArraySize.

> Starting from API level 30, it's strongly recommended to use android.control.zoomRatio to take advantage of better support for zoom with logical multi-camera.

### `SCALER_AVAILABLE_MAX_DIGITAL_ZOOM` (API 21)

Reference: [CameraCharacteristics#SCALER_AVAILABLE_MAX_DIGITAL_ZOOM](https://developer.android.com/reference/android/hardware/camera2/CameraCharacteristics#SCALER_AVAILABLE_MAX_DIGITAL_ZOOM)

> The maximum ratio between both active area width and crop region width, and active area height and crop region height, for android.scaler.cropRegion.

> Starting from API level 30, when using android.control.zoomRatio to zoom in or out, the application must use android.control.zoomRatioRange to query both the minimum and maximum zoom ratio.

### `CONTROL_ZOOM_RATIO` (API 30)

Reference: [CaptureRequest#CONTROL_ZOOM_RATIO](https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#CONTROL_ZOOM_RATIO)

> By using this control, the application gains a simpler way to control zoom, which can be a combination of optical and digital zoom. For example, a multi-camera system may contain more than one lens with different focal lengths, and the user can use optical zoom by switching between lenses.

> * Zooming out from a wide lens to an ultrawide lens: zoomRatio supports zoom-out whereas android.scaler.cropRegion doesn't.

The documented example says that 2x zoom is the same with either key:

> the application can achieve 2.0x zoom in one of two ways:
> * zoomRatio = 2.0, scaler.cropRegion = (0, 0, 2000, 1500)
> * zoomRatio = 1.0 (default), scaler.cropRegion = (500, 375, 1500, 1125)

This explains why our dumps show `cropRegion = 0 0 4080 3060` at 2x and 4x. With `zoomRatio`, the crop region is in the "after-zoom" coordinates. It does not show the zoom.

### `CONTROL_ZOOM_RATIO_RANGE` (API 30)

Reference: [CameraCharacteristics#CONTROL_ZOOM_RATIO_RANGE](https://developer.android.com/reference/android/hardware/camera2/CameraCharacteristics#CONTROL_ZOOM_RATIO_RANGE)

> If the camera device supports zoom-out from 1x zoom, minZoom will be less than 1.0, and setting android.control.zoomRatio to values less than 1.0 increases the camera's field of view.

### When does a logical multi-camera switch lenses?

source.android.com, [Multi-camera support](https://source.android.com/docs/core/camera/multi-camera):

> (Android 11 or higher) For a logical multi-camera device supporting optical zoom, implement the `ANDROID_CONTROL_ZOOM_RATIO` API, and use `ANDROID_SCALER_CROP_REGION` for aspect ratio cropping only.

> (Android 10 or higher) Hide physical sub-cameras from `getCameraIdList`. This reduces the number of cameras that can be directly opened by apps, eliminating the need for apps to have complex camera selection logic.

The `LOGICAL_MULTI_CAMERA` capability ([CameraMetadata](https://developer.android.com/reference/android/hardware/camera2/CameraMetadata#REQUEST_AVAILABLE_CAPABILITIES_LOGICAL_MULTI_CAMERA)):

> Starting from API level 29: Some or all physical cameras may not be independently exposed to the application, in which case the physical camera IDs will not be available in CameraManager#getCameraIdList.

What the documentation says about zoom on **one physical camera**: only the crop text applies. The crop region "can be used to implement digital zoom". `zoomRatio` becomes optical only "by switching between lenses". One physical camera has no lens to switch to. Thus, for the app, zoom on one physical camera is digital. The only exception is the optional in-sensor work of the HAL (section 2).

### Is the zoom below 1.0 required?

Only for devices that declare a media performance class. Android 16 CDD, [section 2.2.7.2](https://source.android.com/docs/compatibility/16/android-16-cdd):

> [7.5/H-1-10] MUST have min ZOOM_RATIO < 1.0 for the primary cameras if there is an ultrawide RGB camera facing the same direction.

This phone gives `ro.odm.build.media_performance_class = 0` (no class declared). Thus this rule does not apply, and a range of 1.0-10.0 on camera 0 is allowed.

## 2. In-sensor zoom and full-resolution modes

### `SENSOR_PIXEL_MODE` and `ULTRA_HIGH_RESOLUTION_SENSOR` (Android 12, API 31)

Android 12 features, [Quad bayer camera sensor support](https://developer.android.com/about/versions/12/features):

> Many Android devices today ship with ultra high-resolution camera sensors, typically with Quad or Nona Bayer patterns [...] Android 12 introduces new platform APIs that let third-party apps take full advantage of these versatile sensors.

[REQUEST_AVAILABLE_CAPABILITIES_ULTRA_HIGH_RESOLUTION_SENSOR](https://developer.android.com/reference/android/hardware/camera2/CameraMetadata#REQUEST_AVAILABLE_CAPABILITIES_ULTRA_HIGH_RESOLUTION_SENSOR):

> This camera device is capable of producing ultra high resolution images in addition to the image sizes described in the android.scaler.streamConfigurationMap. It can operate in 'default' mode and 'max resolution' mode. It generally does this by binning pixels in 'default' mode and not binning them in 'max resolution' mode.

[CaptureRequest#SENSOR_PIXEL_MODE](https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#SENSOR_PIXEL_MODE):

> When operating in SENSOR_PIXEL_MODE_DEFAULT mode, sensors would typically perform pixel binning in order to improve low light performance, noise reduction etc. However, in SENSOR_PIXEL_MODE_MAXIMUM_RESOLUTION mode, sensors typically operate in unbinned mode allowing for a larger image size.

> This key will be be present on devices supporting the REQUEST_AVAILABLE_CAPABILITIES_ULTRA_HIGH_RESOLUTION_SENSOR capability. It may also be present on devices which do not support the aforementioned capability. In that case: [...] The following keys will always be present: android.scaler.streamConfigurationMapMaximumResolution, android.sensor.info.activeArraySizeMaximumResolution, android.sensor.info.pixelArraySizeMaximumResolution, android.sensor.info.preCorrectionActiveArraySizeMaximumResolution

`REMOSAIC_REPROCESSING` (API 31) is only for devices with `ULTRA_HIGH_RESOLUTION_SENSOR`.

What the device must declare for an app to use full resolution: the capability `ULTRA_HIGH_RESOLUTION_SENSOR`, or at least the request key `android.sensor.pixelMode` and the maximum-resolution stream map with values.

### `CROPPED_RAW` and `SCALER_RAW_CROP_REGION` (API 34)

[SCALER_AVAILABLE_STREAM_USE_CASES_CROPPED_RAW](https://developer.android.com/reference/android/hardware/camera2/CameraMetadata#SCALER_AVAILABLE_STREAM_USE_CASES_CROPPED_RAW):

> Certain types of image sensors can run in binned modes in order to improve signal to noise ratio while capturing frames. However, at certain zoom levels and / or when other scene conditions are deemed fit, the camera sub-system may choose to un-bin and remosaic the sensor's output. This results in a RAW frame which is cropped in field of view and yet has the same number of pixels as full field of view RAW, thereby improving image detail.

> This stream use case may not be supported on some devices.

[CaptureResult#SCALER_RAW_CROP_REGION](https://developer.android.com/reference/android/hardware/camera2/CaptureResult#SCALER_RAW_CROP_REGION):

> The region of the sensor that corresponds to the RAW read out for this capture when the stream use case of a RAW stream is set to CROPPED_RAW.

This is the only public API text that describes "in-sensor zoom" directly. It is optional, it is for RAW streams, and "the camera sub-system may choose" when to use it. The app cannot force it.

### `CONTROL_SETTINGS_OVERRIDE` / `CONTROL_SETTINGS_OVERRIDE_ZOOM` (API 34)

This is **not** in-sensor zoom. It only makes zoom changes apply sooner. [CaptureRequest#CONTROL_SETTINGS_OVERRIDE](https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#CONTROL_SETTINGS_OVERRIDE):

> There are some CaptureRequest keys which can be applied earlier than others when controls within a CaptureRequest aren't required to take effect at the same time. One such example is zoom. Zoom can be applied at a later stage of the camera pipeline. As soon as the camera device receives the CaptureRequest, it can apply the requested zoom value onto an earlier request that's already in the pipeline, thus improves zoom latency.

### `CONTROL_ZOOM_METHOD` (HAL 3.11, Android 16)

This only tells the device which key the app uses for zoom (`AUTO` or `ZOOM_RATIO`). It has no effect on detail. Its HAL note says: "Do not use this key directly. It's for camera framework usage, and not for HAL consumption."

### Can a third-party app get more detail at 2x from a 200 MP quad-bayer sensor through public APIs?

Yes, but only when the device supports it:

1. With `SENSOR_PIXEL_MODE_MAXIMUM_RESOLUTION` (API 31): take the full-resolution still and crop it in the app. The device must declare the capability or the keys above.
2. With `CROPPED_RAW` (API 34): a RAW stream that the HAL can un-bin at some zoom levels. It is optional, and the HAL decides when.
3. Without app action: the HAL can use an "in-sensor crop" for normal zoom. This is allowed but not required, and the app cannot request it.

## 3. CameraX

### How `setZoomRatio` maps to Camera2

- CameraX release notes, `camera-camera2 1.0.0-beta11` (2020-10-14): "Supports android 11 CONTROL_ZOOM_RATIO API for zoom on android 11 or later devices which contains valid CONTROL_ZOOM_RATIO_RANGE."
- androidx source, `camera/camera-camera2/src/main/java/androidx/camera/camera2/compat/ZoomCompat.kt` (last change 2025-10-28): on API 30+ with a valid `CONTROL_ZOOM_RATIO_RANGE`, CameraX uses `AndroidRZoomCompat`. That class sets `CaptureRequest.CONTROL_ZOOM_RATIO`. On API 34+, it also sets `CONTROL_SETTINGS_OVERRIDE_ZOOM` when the camera supports it. Other devices get `CropRegionZoomCompat` (`SCALER_CROP_REGION`).
- The `CameraControl.setZoomRatio` javadoc does not say anything about optical or digital zoom. It only says: "If the ratio is smaller than ZoomState#getMinZoomRatio() or larger than ZoomState#getMaxZoomRatio(), the returned ListenableFuture will fail with IllegalArgumentException [...] It is the applications' duty to clamp the ratio."

### In-sensor zoom or high-resolution capture in CameraX

- CameraX has no in-sensor-zoom API.
- `ResolutionSelector.PREFER_HIGHER_RESOLUTION_OVER_CAPTURE_RATE` (CameraX 1.3.0-alpha06, renamed in 1.3.0-beta01) lets CameraX pick the "high resolution" output sizes (`StreamConfigurationMap.getHighResolutionOutputSizes`). These are sizes in the normal (default pixel mode) map. They are not the maximum-resolution map.
- `OutputConfigurationInterop.addSensorPixelModeUsed(int)` (API 31) is in the androidx API file `camera/camera-camera2/api/1.7.0-rc01.txt`. It is not in the 1.6.x API files. On Maven, 1.7.0 is at `1.7.0-alpha03`. We use 1.6.2.
- Thus, with CameraX 1.6.2, the only way to change the sensor mode is Camera2 interop with vendor keys. That is what the in-sensor zoom experiment did.

## 4. Xiaomi / HyperOS

### Official: HyperOS "Camera Engine" SDK (相机引擎)

- [Capability introduction (能力介绍)](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1811), updated 2025-07-15:
  > 单摄主要包括：超广角镜头、主摄镜头、长焦镜头、微距镜头等
  >
  > (Single cameras include: the ultrawide lens, the main lens, the telephoto lens, the macro lens, and others.)
  >
  > 多摄主要包括：SAT多摄、人像多摄等变焦（依次为0.5X 1X 2X 3.2X 5X 10X）
  >
  > (Multi-cameras include: SAT multi-camera, portrait multi-camera, and others; zoom, in order 0.5X 1X 2X 3.2X 5X 10X.)
- [Integration document (相机引擎技术接入文档)](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1812), updated 2024-10-18:
  - It is based on "Camera API2, Android P及以上的版本" (Camera API2, Android P and later).
  - The app selects a camera with `getCameraIdByType()` and gets SAT zoom ranges with `getSATZoomRange()`.
  - Access needs an authorization. The app owner sends the company name, app name, package name, use case, and timeline to Xiaomi through customer service.
  - The listed phones are Xiaomi MIX FOLD series, MIX 4, Xiaomi 14/13/12S/11/10 Pro, Redmi K70 Pro, K60 Pro, and POCO X6 Pro 5G. The **Redmi Note 15 Pro+ is not in the list**.
  - The pages do not mention super resolution (超分), in-sensor zoom, or the Camera2 camera list for normal apps.
- [Camera Engine overview](https://dev.mi.com/xiaomihyperos/ability/camera): the onboarding needs a Xiaomi developer account with enterprise verification.

Thus the official Xiaomi answer for the auxiliary lenses is "use our SDK, with our authorization". It is not "use the Camera2 camera list".

### Not official (label: community sources)

- [xiaomi.eu forum, Mi 11 Ultra](https://xiaomi.eu/community/threads/mi-11-ultra-unable-to-access-camera-lenses-in-apps-camera2-api.61456/) and XDA threads: Xiaomi shows only the main and front cameras to third-party apps. A system property with a package list (for example `vendor.camera.aux.packagelist`) can add apps, but it needs system access. On this phone, that property is empty (checked in `phone-lenses.md`).

### Qualcomm

I found no public Qualcomm documentation for `org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom`, `inSensorZoom.InSensorZoomState`, or `inSensorZoom.SensorSwitched`. The public AOSP Qualcomm HAL is for old chips. Its vendor tag file (`platform/hardware/qcom/camera`, branch `main`, `msm8998/QCamera2/HAL3/QCamera3VendorTags.cpp`, 849 lines) has no `insensor` tag and no `sessionParameters` section. The open Snapdragon Camera app source that I checked does not use them. Thus our experiment with this tag is not based on any documentation.

## 5. Comparison with the device findings

New read-only check on `7fad170e` (2026-09-25), camera 0 (`dumpsys media.camera`, HAL static information):

| Item | Value |
|---|---|
| Normal (visible) camera devices | 2 (`0`, `1`) |
| `android.control.zoomRatioRange` | `[1.0 10.0]` |
| `android.scaler.availableMaxDigitalZoom` | `10.0` |
| `ULTRA_HIGH_RESOLUTION_SENSOR` in `availableCapabilities` | no |
| `android.sensor.pixelMode` in `availableRequestKeys` | **no** (only in `availableResultKeys`) |
| Maximum-resolution keys (`availableStreamConfigurationsMaximumResolution`, `pixelArraySizeMaximumResolution`, `binningFactor`, ...) | named in `availableCharacteristicsKeys`, but **no value entries** in the HAL static metadata |
| `android.scaler.availableStreamUseCases` | `0 1 2 3 4 5` and vendor values `65536`, `65537`. **No `CROPPED_RAW` (6)** |
| `android.control.availableSettingsOverrides` | `[0]` (OFF only) |
| `ro.odm.build.media_performance_class` | `0` |
| Our session now | `settingsOverride = OFF`, `EnableInsensorZoom = 0` |

| Device finding | What the documentation says | Verdict |
|---|---|---|
| Our zoom on camera 0 is a digital crop | Crop region "can be used to implement digital zoom". `zoomRatio` is optical only by "switching between lenses". Camera 0 is one physical camera. | **Confirmed** |
| `cropRegion` stays `0 0 4080 3060` at 2x and 4x | With `zoomRatio`, the crop region is in after-zoom coordinates (the documented example). | **Confirmed** (the crop region does not show the zoom) |
| The ultrawide and the logical camera are hidden, no zoom below 1.0 | Hiding physical cameras is recommended since Android 10. Zoom below 1.0 is required only for media performance class devices, and this phone declares class 0. | **Confirmed**: allowed by the documentation |
| No macro camera, Xiaomi "macro" is a main-camera mode | The Xiaomi SDK lists a macro lens as a possible single camera, in general. It says nothing about this model. | **Not decided** by the documentation. The device data (module list) stays the evidence. |
| The vendor in-sensor zoom tag does not give detail for our app | No public Qualcomm or Xiaomi document describes the tag. | **Not decided** by the documentation. The measurement stays the evidence. |
| "Zoom never adds detail" | The HAL "may" use an in-sensor crop, or un-bin and remosaic "at certain zoom levels". | **Too strong in general.** Correct for this phone: our measurements showed no gain. |

Documented public paths that we have not tried:

1. `SENSOR_PIXEL_MODE_MAXIMUM_RESOLUTION`: **not possible on camera 0**. The key is not a request key, and the maximum-resolution map has no values. I did not call the Java API (`CameraCharacteristics.get(SCALER_STREAM_CONFIGURATION_MAP_MAXIMUM_RESOLUTION)`) because the brief forbids a camera or app action. The HAL data makes a `null` result very likely.
2. `CROPPED_RAW`: **not possible**. Camera 0 does not list it.
3. `CONTROL_SETTINGS_OVERRIDE_ZOOM`: not supported (OFF only). It would only change latency.
4. Zoom through `SCALER_CROP_REGION` instead of `CONTROL_ZOOM_RATIO` (the open idea in `dd-android.md`): the documentation says that both give the same 2x zoom. The HAL could still treat them differently, but no document says so. A test is cheap, and the chance of a gain is low.
5. The Xiaomi Camera Engine SDK: the only documented Xiaomi path to other lenses and SAT zoom. It needs an enterprise account and an authorization, and this phone is not in its list. I do not recommend it for this project.

## 6. The evidence rule "distance gives detail; zoom does not"

Current text in `mcp/debug_devices_mcp/instructions.py`, rule 6:

> Phone zoom is often only a digital crop: it frames the area but adds no detail. [...] Distance gives detail; zoom does not.

Verdict: **correct for this phone today, but too strong as a general statement.** The documentation permits devices that add real detail at some zoom levels (in-sensor crop, un-bin and remosaic), or that switch to a telephoto lens. The first sentence already says "often". The last sentence is absolute.

Proposed text for the end of rule 6:

> Phone zoom is often only a digital crop: it frames the area but adds no detail. On the current phone (camera 0, one physical camera), all zoom is a digital crop: tests showed no detail gain at 2x or 4x. When a marking or a small part is too small to read, ask the user to move the phone closer (near the minimum focus distance, about 10-12 cm), then use zoom only to frame. On this phone, distance gives detail and zoom does not.

If the rule must stay device-neutral, use this last sentence instead:

> Unless a test on the connected phone shows a detail gain at zoom, assume that distance gives detail and zoom does not.
