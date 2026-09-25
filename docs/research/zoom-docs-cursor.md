# Is phone zoom only a crop?

Check date: 2026-09-25. Phone: adb serial `7fad170e`, model `2510ERA8BG`, API 36. This note uses the public Android and Xiaomi documents. The device facts come from `docs/research/phone-lenses.md` and the in-sensor zoom rounds in `docs/reports/dd-android.md`. One read-only `dumpsys media.camera` on this serial confirms the keys below. The command did not open a camera.

## Conclusion

On this phone, public zoom is a crop of the 12.5 MP binned image. It does not add detail. Move the phone closer to get more detail.

The sentence "distance gives detail; zoom does not" is correct for this phone. It is too strong as a rule for every Android phone. A zoom adds detail only when the device switches to another lens, or when it reads the sensor without binning. This phone does not give those modes to a third-party app.

## 1. Camera2 zoom

`CaptureRequest.CONTROL_ZOOM_RATIO` was added in API level 30.

https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#CONTROL_ZOOM_RATIO

The reference says:

> Instead of using SCALER_CROP_REGION for zoom, the application can now choose to use this tag to specify the desired zoom level.
>
> By using this control, the application gains a simpler way to control zoom, which can be a combination of optical and digital zoom. For example, a multi-camera system may contain more than one lens with different focal lengths, and the user can use optical zoom by switching between lenses.

The same page says zoom-out needs `zoomRatio`. `SCALER_CROP_REGION` does not zoom out. The example for 2.0x on one sensor is:

> zoomRatio = 2.0, scaler.cropRegion = (0, 0, 2000, 1500)

In that example the crop rectangle stays the full active array. The ratio does the zoom. The result crop rectangle then uses the after-zoom field of view.

`CaptureRequest.SCALER_CROP_REGION` was added in API level 21.

https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#SCALER_CROP_REGION

The reference says:

> The desired region of the sensor to read out for this capture. This control can be used to implement digital zoom.

The output stream crops to that rectangle and then scales the pixels to the stream size. The crop is usually after the RAW conversion. A RAW stream is not cropped unless the device supports `CROPPED_RAW`.

`CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE` was added in API level 30.

https://developer.android.com/reference/android/hardware/camera2/CameraCharacteristics#CONTROL_ZOOM_RATIO_RANGE

> Minimum and maximum zoom ratios supported by this camera device.
>
> If the camera device supports zoom-out from 1x zoom, minZoom will be less than 1.0.

`CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM` was added in API level 21.

https://developer.android.com/reference/android/hardware/camera2/CameraCharacteristics#SCALER_AVAILABLE_MAX_DIGITAL_ZOOM

> The maximum ratio between both active area width and crop region width, and active area height and crop region height, for android.scaler.cropRegion.
>
> Starting from API level 30, when using android.control.zoomRatio to zoom in or out, the application must use android.control.zoomRatioRange to query both the minimum and maximum zoom ratio.

A lens switch is a logical-camera behavior. The HAL document says:

> (Android 11 or higher) For a logical multi-camera device supporting optical zoom, implement the ANDROID_CONTROL_ZOOM_RATIO API, and use ANDROID_SCALER_CROP_REGION for aspect ratio cropping only.

https://source.android.com/docs/core/camera/multi-camera

The app guide says the logical camera "is entirely dependent on the OEM implementation of the Camera HAL."

https://developer.android.com/media/camera/camera2/multi-camera

On one physical camera, the documents describe digital zoom. They do not describe a lens switch. Optical zoom in these pages means a switch between physical cameras of a logical camera.

`CONTROL_ZOOM_METHOD` was added in API level 36. `AUTO` is the old behavior. `ZOOM_RATIO` forces the ratio path.

https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#CONTROL_ZOOM_METHOD

## 2. In-sensor zoom and full-resolution mode

The public name is not "in-sensor zoom". The public name is maximum-resolution sensor pixel mode.

`CaptureRequest.SENSOR_PIXEL_MODE` was added in API level 31.

https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#SENSOR_PIXEL_MODE

> When operating in SENSOR_PIXEL_MODE_DEFAULT mode, sensors would typically perform pixel binning in order to improve low light performance, noise reduction etc. However, in SENSOR_PIXEL_MODE_MAXIMUM_RESOLUTION mode, sensors typically operate in unbinned mode allowing for a larger image size.

The device must list `REQUEST_AVAILABLE_CAPABILITIES_ULTRA_HIGH_RESOLUTION_SENSOR` for the mandatory maximum-resolution combinations. The capability text says:

> It can operate in 'default' mode and 'max resolution' mode. It generally does this by binning pixels in 'default' mode and not binning them in 'max resolution' mode. [...] The maximum resolution mode pixel array size [...] will be at least 24 megapixels.

https://developer.android.com/reference/android/hardware/camera2/CameraMetadata#REQUEST_AVAILABLE_CAPABILITIES_ULTRA_HIGH_RESOLUTION_SENSOR

The app then reads sizes from `SCALER_STREAM_CONFIGURATION_MAP_MAXIMUM_RESOLUTION`. Default-map sizes and maximum-resolution sizes must not be mixed in one request.

`SCALER_AVAILABLE_STREAM_USE_CASES` was added in API level 33. The value `CROPPED_RAW` is 6.

https://developer.android.com/reference/android/hardware/camera2/CameraMetadata#SCALER_AVAILABLE_STREAM_USE_CASES_CROPPED_RAW

> Certain types of image sensors can run in binned modes in order to improve signal to noise ratio while capturing frames. However, at certain zoom levels and / or when other scene conditions are deemed fit, the camera sub-system may choose to un-bin and remosaic the sensor's output. This results in a RAW frame which is cropped in field of view and yet has the same number of pixels as full field of view RAW, thereby improving image detail.

That is the public text for a zoom that adds detail. The camera "may choose" to un-bin. The app does not force it, except by asking for a RAW stream with use case `CROPPED_RAW`. The same page says: "This stream use case may not be supported on some devices." The crop that the HAL used is `CaptureResult.SCALER_RAW_CROP_REGION`.

`CaptureRequest.CONTROL_SETTINGS_OVERRIDE` was added in API level 34. The value `ZOOM` applies zoom keys sooner. It changes latency. It does not change the sensor mode.

https://developer.android.com/reference/android/hardware/camera2/CaptureRequest#CONTROL_SETTINGS_OVERRIDE

`REQUEST_AVAILABLE_CAPABILITIES_REMOSAIC_REPROCESSING` is a reprocess path from a binned RAW pattern to a regular Bayer pattern. The text says it is present only on devices that already have the ultra-high-resolution capability.

https://developer.android.com/reference/android/hardware/camera2/CameraMetadata#REQUEST_AVAILABLE_CAPABILITIES_REMOSAIC_REPROCESSING

A third-party app can get the unbinned frame only when the camera lists `ULTRA_HIGH_RESOLUTION_SENSOR`, and the app sets `SENSOR_PIXEL_MODE_MAXIMUM_RESOLUTION` and a size from the maximum-resolution map. A 200 MP quad-Bayer sensor does not grant that path by itself.

## 3. CameraX

`CameraControl.setZoomRatio` was added in CameraX 1.0.0. The ratio must be inside `ZoomState.getMinZoomRatio()` and `getMaxZoomRatio()`.

https://developer.android.com/reference/androidx/camera/core/CameraControl#setZoomRatio(float)

https://developer.android.com/media/camera/camerax/configuration

The CameraX source uses `CONTROL_ZOOM_RATIO` on API 30 and later when `CONTROL_ZOOM_RATIO_RANGE` is present. It uses `SCALER_CROP_REGION` on older devices. Commit `8de5e7d` (2020-09-16) says:

> Use Android 11 zoom API(CONTROL_ZOOM_RATIO) to implement zoom on android 11 devices and CONTROL_ZOOM_RATIO_RANGE contains valid range. Use SCALER_CROP_REGION for zoom otherwise.

https://android.googlesource.com/platform/frameworks/support/+/8de5e7d2ea222f38eb759038a909c307079cd2ee

CameraX does not document `SENSOR_PIXEL_MODE` or in-sensor zoom.

`ResolutionSelector.PREFER_HIGHER_RESOLUTION_OVER_CAPTURE_RATE` was added in CameraX 1.3.0. It reads `getOutputSizes` and `getHighResolutionOutputSizes` from `SCALER_STREAM_CONFIGURATION_MAP`. The reference says:

> Since Android 12, some devices might support a maximum resolution sensor pixel mode, which allows them to capture additional ultra high resolutions retrieved from SCALER_STREAM_CONFIGURATION_MAP_MAXIMUM_RESOLUTION. This mode does not allow applications to select those ultra high resolutions.

https://developer.android.com/reference/androidx/camera/core/resolutionselector/ResolutionSelector#PREFER_HIGHER_RESOLUTION_OVER_CAPTURE_RATE()

`getHighResolutionOutputSizes` is a slower size list on the default map. It is not the unbinned 200 MP mode.

## 4. Xiaomi and HyperOS

Xiaomi publishes a camera SDK. The page title is "能力介绍" (capability introduction). Update time: 2025-07-15.

https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1811

The page says the SDK gives third-party apps the hardware cameras: ultrawide, main, telephoto, and macro, plus SAT zoom ratios 0.5x, 1x, 2x, 3.2x, 5x, and 10x. The page does not say "in-sensor zoom", "remosaic", or "200 MP".

The access page, update time 2026-04-30, requires an application, a review, and Maven credentials from Xiaomi support.

https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1868

The test phones on that page are Xiaomi Civi 5 Pro, Xiaomi 15, Xiaomi 15 Pro, Redmi K80 Pro, Redmi K80, and Xiaomi 15 Ultra. Redmi Note 15 Pro+ (`2510ERA8BG`) is not in that list. The full model table is a password-protected Xiaomi drive link, not a public document.

No public Xiaomi page says that a normal Camera2 app can open the hidden auxiliary cameras. No public Xiaomi page says that `EnableInsensorZoom` works for third-party apps.

Label for the best non-official source: the HAL dump on this phone (`docs/research/phone-lenses.md`). It is a device fact, not a Xiaomi document. Qualcomm does not publish a third-party guide for `org.codeaurora.qcamera3.sessionParameters.EnableInsensorZoom`. A search on 2026-09-25 did not find one.

## 5. Documents against the device facts

Read-only dump of camera 0 on 2026-09-25:

- `android.control.zoomRatioRange` = 1.0 to 10.0.
- `android.scaler.availableMaxDigitalZoom` is present.
- `android.sensor.info.activeArraySize` = `[0, 0, 4080, 3060]`.
- `android.sensor.info.pixelArraySize` = `[4080, 3060]`.
- `android.request.availableCapabilities` = `BACKWARD_COMPATIBLE`, `CONSTRAINED_HIGH_SPEED_VIDEO`, `RAW`, `YUV_REPROCESSING`, `PRIVATE_REPROCESSING`, `READ_SENSOR_SETTINGS`, `MANUAL_SENSOR`, `BURST_CAPTURE`, `MANUAL_POST_PROCESSING`, `STREAM_USE_CASE`.
- `ULTRA_HIGH_RESOLUTION_SENSOR` is absent.
- `android.scaler.availableStreamUseCases` = 0, 1, 2, 3, 4, 5, 65536, 65537. Value 6 (`CROPPED_RAW`) is absent.
- `android.control.availableSettingsOverrides` = `[0]` (`OFF` only).
- Vendor `qcfa_dimension` = `[16320, 12240]`.
- Vendor `com.xiaomi.camera.supportedfeatures.insensorzoom` = byte `47`.

| Device fact | What the documents say |
|---|---|
| Zoom 1.0-10.0 on camera 0 is digital. The crop rectangle stays `[0, 0, 4080, 3060]` while `zoomRatio` is 2 or 4. | The documents agree. On API 30+, ratio 2.0 with a full-array crop rectangle is the documented 2x example. The crop is then scaled to the stream. |
| Camera 0 is one physical camera. Zoom does not switch lenses. | The documents agree. A lens switch is defined for a logical multi-camera. |
| The ultrawide (ID 2) and the logical camera (ID 3) are hidden. | The documents allow this. Android 9 and later let the OEM hide physical IDs. They do not require a third-party app to see every lens. https://source.android.com/docs/core/camera/versioning |
| The 200 MP array is a vendor tag. The public array is 4080 x 3060. | The documents agree that the public sizes are the stream maps. A vendor quad-CFA tag is not a public size. |
| `EnableInsensorZoom = 1` does not set `InSensorZoomState` or `SensorSwitched`. Sharpness at 2x and 4x does not rise. | The documents cannot decide a Qualcomm vendor tag. The public un-bin path is `SENSOR_PIXEL_MODE` plus the ultra-high-resolution capability. This camera does not list that capability. |
| AF `MACRO` is in `afAvailableModes`. There is no macro camera. | The documents agree that `CONTROL_AF_MODE_MACRO` is a focus mode, not a lens. |

Paths that are still untried, and what the documents say about them:

- `SENSOR_PIXEL_MODE_MAXIMUM_RESOLUTION` on camera 0. The capability is absent, and the public pixel array is already 4080 x 3060. The documents say the unbinned sizes come from the maximum-resolution map of a camera that lists the capability. This camera does not. Do not expect a 200 MP JPEG from this key.
- `CROPPED_RAW`. The use-case value 6 is absent. The documents say the use case may be unsupported. There is no public RAW crop that adds detail.
- `CONTROL_SETTINGS_OVERRIDE_ZOOM`. `availableSettingsOverrides` is `[0]`. The value `ZOOM` is not offered. The control only changes zoom latency.
- `ResolutionSelector` high-resolution mode. CameraX says this mode does not select the maximum-resolution map.
- The Xiaomi camera SDK. It is a real official SDK, but it needs Xiaomi approval, and this model is not in the published test list. It is not a path the current app can call.

## 6. Wording for the evidence rule

Use this wording:

"On this phone, public zoom crops the 12.5 MP binned image and then scales it. The crop does not add detail. Move the phone closer to get more detail. Zoom adds detail only on a phone that switches lens or that advertises the ultra-high-resolution sensor mode. This phone does not advertise that mode to a third-party app."
