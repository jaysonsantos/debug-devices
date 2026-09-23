# Research notes

Facts, versions, and code snippets for `android/` and `mcp/`. Date of the check: 2026-09-23.

## 1. Toolchain versions (dev shell)

The dev shell comes from `flake.nix` (nixpkgs `nixos-unstable`, locked 2026-09-22). Enter it with `nix develop` or direnv.

| Item | Version | Source |
|---|---|---|
| JDK | 17 (`17.0.20.1`), `JAVA_HOME` is set | nixpkgs `jdk17` |
| Gradle | 9.7.1 | nixpkgs `gradle_9` |
| Android Gradle Plugin (AGP) | 9.4.1 (latest stable). Compatible range for this shell: 9.4.x. | Google Maven |
| AGP 9.4 needs | Gradle 9.6.0 or later, JDK 17, build-tools 36.0.0 or later, compileSdk up to 37 | AGP release notes |
| compileSdk / targetSdk | 36 | SDK in the shell: `platforms;android-36` |
| minSdk | 26 or later (recommended; CameraX 1.6 needs 23 or later) | |
| Build-tools | 36.0.0 | SDK in the shell |
| Platform-tools (adb) | 37.0.x (`android-tools` and SDK `platform-tools`) | nixpkgs |
| Kotlin | 2.4.20 (latest Kotlin Gradle plugin). AGP 9 has built-in Kotlin: do not apply `org.jetbrains.kotlin.android`. Apply `org.jetbrains.kotlin.plugin.serialization` with the same Kotlin version. | Maven Central |
| ktlint | 1.8.0 | nixpkgs |
| Python | 3.14.7 (first `python3` on PATH; `UV_PYTHON_DOWNLOADS=never`, `UV_PYTHON_PREFERENCE=only-system`) | nixpkgs `python314` |
| uv | 0.12.17 | nixpkgs |
| ruff | 0.16.8 | nixpkgs |
| ffmpeg | 9.0.1 | nixpkgs |

Library versions (latest stable on 2026-09-23):

| Library | Coordinates | Version |
|---|---|---|
| CameraX | `androidx.camera:camera-core`, `camera-camera2`, `camera-lifecycle`, `camera-view` | 1.6.2 |
| kotlinx.serialization | `org.jetbrains.kotlinx:kotlinx-serialization-json` | 1.11.0 |
| kotlinx.coroutines | `org.jetbrains.kotlinx:kotlinx-coroutines-android` | 1.11.0 |
| AndroidX Core | `androidx.core:core-ktx` | 1.19.0 |
| AndroidX Activity | `androidx.activity:activity-ktx` | 1.13.0 |
| AndroidX Lifecycle | `androidx.lifecycle:lifecycle-runtime-ktx` | 2.11.0 |
| AppCompat | `androidx.appcompat:appcompat` | 1.8.0 |
| Ktor server (CIO engine) | `io.ktor:ktor-server-cio` | 3.6.0 |

### Android SDK in the shell

- The SDK is `androidenv.composeAndroidPackages` with platform 36 and build-tools 36.0.0. No emulator, no system images, no NDK.
- `ANDROID_HOME` and `ANDROID_SDK_ROOT` point to `<sdk>/libexec/android-sdk` in the Nix store. The store is read-only: Gradle cannot install more SDK packages. To add a package, change `flake.nix`.
- The flake accepts the SDK license with `config.android_sdk.accept_license = true` and `allowUnfree = true`.
- `GRADLE_OPTS` sets `-Dorg.gradle.project.android.aapt2FromMavenOverride=$ANDROID_HOME/build-tools/36.0.0/aapt2`. The aapt2 from Maven is a dynamic binary that does not run on NixOS. The SDK aapt2 runs (`aapt2 version` prints `2.20-13193326`).
- Do not write `sdk.dir` into `local.properties`. `ANDROID_HOME` is sufficient.
- Use the Gradle from the shell to make the wrapper once: `gradle wrapper --gradle-version 9.7.1`. After that, `./gradlew` works in the shell.

Verification (all pass):

```sh
nix develop --command python3 --version      # Python 3.14.7
nix develop --command uv --version           # uv 0.12.17
nix develop --command gradle --version       # Gradle 9.7.1, JVM 17.0.20.1
nix develop --command sdkmanager --list_installed
# build-tools;36.0.0, platforms;android-36, platform-tools 37.0.1, cmdline-tools;22.0
```

Minimal `android/` build settings that match the shell. A smoke build with these settings, CameraX 1.6.2, kotlinx.serialization 1.11.0, Ktor CIO 3.6.0, and a `@Serializable` class passed: `gradle assembleDebug` made `app-debug.apk` (built-in Kotlin, no `kotlin-android` plugin):

```kotlin
// settings.gradle.kts
pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
    plugins {
        id("com.android.application") version "9.4.1"
        id("org.jetbrains.kotlin.plugin.serialization") version "2.4.20"
    }
}
dependencyResolutionManagement {
    repositories { google(); mavenCentral() }
}

// app/build.gradle.kts
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.serialization")
}
android {
    namespace = "dev.jayson.debugdevices.camera"
    compileSdk = 36
    buildToolsVersion = "36.0.0"
    defaultConfig { minSdk = 26; targetSdk = 36 }
}
kotlin { jvmToolchain(17) }
```

## 2. MCP Python SDK

Facts (checked with `mcp==2.2.0` in a probe venv, Python 3.14.7):

- Package `mcp` on PyPI, current version **2.2.0** (2026-09-07), `requires-python >=3.10`. It also installs `mcp_types` 2.2.0, which holds the protocol types.
- **mcp 2.x renamed `FastMCP` to `MCPServer`.** `from mcp.server.fastmcp import FastMCP` raises `ModuleNotFoundError`. Use `from mcp.server.mcpserver import MCPServer, Image`. To keep v1 code, pin `mcp<2`. Migration guide: <https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver>.
- Protocol types come from `mcp_types`: `CallToolResult`, `ImageContent`, `TextContent`. Fields are snake_case in Python (`structured_content`, `mime_type`, `is_error`, `output_schema`).
- `MCPServer(name=..., version=..., instructions=...)`. `server.run()` uses stdio by default (`transport="stdio"`). On stdio, stdout carries the protocol: write logs to stderr only.
- `@server.tool()` makes a tool. The docstring is the description. The type hints make the input schema.
- Return type rules (tested):
  - Return a pydantic model: the tool gets an `outputSchema`; the result has `structured_content` plus a JSON text block.
  - Return `Image(data=jpeg_bytes, format="jpeg")`: the result has one `ImageContent` with `mime_type="image/jpeg"`.
  - Image **and** structured JSON: return a `CallToolResult` and annotate the return as `Annotated[CallToolResult, Model]`. The tool then has the `outputSchema` of `Model`, and the SDK validates `structured_content` against it.
- Tests: `from mcp import Client`; `async with Client(server) as client:` connects in process (no subprocess). `await client.call_tool(name, args)` returns a `CallToolResult`.

Tested snippet (output below):

```python
from typing import Annotated

from mcp import Client
from mcp.server.mcpserver import Image, MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel

server = MCPServer(name="debug-devices", version="0.1.0")


class Reading(BaseModel):
    value: float
    unit: str


@server.tool()
def snapshot_with_reading() -> Annotated[CallToolResult, Reading]:
    """Return a JPEG and a structured reading."""
    reading = Reading(value=3.0, unit="Ohm")
    return CallToolResult(
        content=[
            Image(data=jpeg_bytes, format="jpeg").to_image_content(),
            TextContent(text=reading.model_dump_json()),
        ],
        structured_content=reading.model_dump(mode="json"),
    )


if __name__ == "__main__":
    server.run()  # stdio


# In a pytest test:
async def test_snapshot() -> None:
    async with Client(server) as client:
        result = await client.call_tool("snapshot_with_reading", {})
    assert result.structured_content == {"value": 3.0, "unit": "Ohm"}
```

Probe output:

```text
reading            [('text', None)]                           {'value': 1.5, 'unit': 'V'}
snapshot           [('image', 'image/jpeg')]                  None
snapshot_with_meta [('image', 'image/jpeg'), ('text', None)]  {'value': 2.0, 'unit': 'A'}
annotated outputSchema: {'properties': {'value': ..., 'unit': ...}, 'required': ['value', 'unit'], 'title': 'Reading', 'type': 'object'}
```

Other current versions on PyPI: `httpx` 0.28.1, `pydantic` 2.13.5.

## 3. OpenRouter: image input and structured output

Endpoint: `POST https://openrouter.ai/api/v1/chat/completions`, header `Authorization: Bearer $OPENROUTER_API_KEY`. OpenAI-compatible body.

### Default model: `openai/gpt-6-luna`

From `GET https://openrouter.ai/api/v1/models` on 2026-09-23:

| Field | `openai/gpt-6-luna` (default) | `deepseek/deepseek-v4.1-flash` (old default) |
|---|---|---|
| Input modalities | text, image, file | text, image |
| Context | 1,050,000 tokens | 1,048,576 tokens |
| Max completion tokens | 128,000 | 943,718 |
| Price, prompt | $0.10 / 1M tokens | $0.10 / 1M tokens |
| Price, completion | $0.50 / 1M tokens | $0.50 / 1M tokens |
| `response_format` | yes | yes |
| `structured_outputs` | yes | yes |
| `temperature` | **no** | yes |
| Providers | OpenAI, Azure | DeepSeek |

- `gpt-6-luna`: prompts over 272,000 tokens cost more ($0.20 / $0.75 per 1M). One webcam frame is far below that.
- `gpt-6-luna` does not list `temperature`, `top_p`, or `stop`. Do not send them. It lists `reasoning`, `reasoning_effort`, `max_tokens`, `max_completion_tokens`, `seed`.
- Endpoint list: `GET https://openrouter.ai/api/v1/models/openai/gpt-6-luna/endpoints`.

### Request

- Image: a content part `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<b64>"}}`. PNG, JPEG, WebP, and GIF are supported. Put the text part first, then the image (OpenRouter recommendation).
- Structured output: `response_format` with `type: "json_schema"`. Set `strict: true` and `additionalProperties: false`. With strict mode, every property must be in `required` (use `null` in a type union for an optional field).
- Routing: set `"provider": {"require_parameters": true}`. Then OpenRouter only uses endpoints that support every parameter in the request. Without a supporting endpoint the request fails with an error.
- The answer is a JSON string in `choices[0].message.content`. Parse it with the pydantic model (`Model.model_validate_json(content)`).

```json
{
  "model": "openai/gpt-6-luna",
  "messages": [
    {"role": "system", "content": "You read digital multimeters. Answer only with the JSON schema."},
    {"role": "user", "content": [
      {"type": "text", "text": "Read the display and the rotary switch of the multimeter."},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
    ]}
  ],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "multimeter_reading",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "value": {"type": ["number", "null"]},
          "display_text": {"type": "string"},
          "unit": {"type": ["string", "null"]},
          "mode": {"type": "string"},
          "range": {"type": ["string", "null"]},
          "confidence": {"type": "number"}
        },
        "required": ["value", "display_text", "unit", "mode", "range", "confidence"],
        "additionalProperties": false
      }
    }
  },
  "provider": {"require_parameters": true},
  "max_tokens": 1000
}
```

Pydantic can make the schema: `Model.model_json_schema()`. For strict mode, set `model_config = ConfigDict(extra="forbid")` so the schema has `"additionalProperties": false`, and give every field no default (all fields in `required`).

Sources: <https://openrouter.ai/docs/features/structured-outputs>, <https://openrouter.ai/docs/guides/overview/multimodal/image-understanding>, <https://openrouter.ai/api/v1/models>.

## 4. CameraX

Checked with `javap` on the CameraX 1.6.2 jars from the Gradle cache.

- Current stable: **1.6.2** (2026-08-26). 1.6.0 (2026-03-25) moved CameraX to the CameraPipe stack and made `SessionConfig` stable. minSdk of CameraX is 23 since 1.5.
- Artifacts: `camera-core`, `camera-camera2`, `camera-lifecycle`. Add `camera-view` only for a `PreviewView`.
- Get the provider with a suspend call: `ProcessCameraProvider.awaitInstance(context)` (extension in `camera-lifecycle`, `ProcessCameraProviderExtKt`).
- Bind: `provider.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture)` returns a `Camera` with `cameraControl` and `cameraInfo`.
- `CameraControl` methods return `ListenableFuture`:
  - `setZoomRatio(Float)`: `ListenableFuture<Void>`. The ratio must be in `[min, max]`, or the future fails with `IllegalArgumentException`. Clamp first.
  - `enableTorch(Boolean)`: `ListenableFuture<Void>`.
  - To await a `ListenableFuture` in a coroutine, add `androidx.concurrent:concurrent-futures-ktx` (`future.await()`) or `kotlinx-coroutines-guava`.
- `CameraInfo`:
  - `zoomState: LiveData<ZoomState>`; `ZoomState` has `zoomRatio`, `minZoomRatio`, `maxZoomRatio`, `linearZoom`. Read `cameraInfo.zoomState.value` on the main thread.
  - `hasFlashUnit(): Boolean`.
  - `torchState: LiveData<Int>` (`TorchState.ON` / `TorchState.OFF`).
- `ImageCapture.Builder`: `setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY | CAPTURE_MODE_MAXIMIZE_QUALITY)`, `setJpegQuality(1..100)`, `setFlashMode(ImageCapture.FLASH_MODE_OFF)`, `setResolutionSelector(...)`.
- In-memory capture: `takePicture(executor, OnImageCapturedCallback)`, or the suspend extension `imageCapture.takePicture(): ImageProxy` (`ImageCaptureExtKt`, 1.6). The default output format is JPEG: plane 0 holds the complete JPEG bytes. Close the `ImageProxy` after use.

```kotlin
val provider = ProcessCameraProvider.awaitInstance(context)
val imageCapture = ImageCapture.Builder()
    .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
    .setJpegQuality(JPEG_QUALITY)
    .build()
val camera = provider.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture)

// Zoom with clamp (main thread for LiveData.value).
val zoom = camera.cameraInfo.zoomState.value ?: error("zoom state not ready")
camera.cameraControl.setZoomRatio(ratio.coerceIn(zoom.minZoomRatio, zoom.maxZoomRatio)).await()

// Torch.
if (camera.cameraInfo.hasFlashUnit()) camera.cameraControl.enableTorch(true).await()

// Snapshot to JPEG bytes.
val jpeg: ByteArray = imageCapture.takePicture().use { image ->
    val buffer = image.planes[0].buffer
    ByteArray(buffer.remaining()).also(buffer::get)
}
```

- Note: when the torch is on, set `FLASH_MODE_OFF` on `ImageCapture`, so the capture does not fire the flash and turn the torch off.
- Sources: <https://developer.android.com/jetpack/androidx/releases/camera>, javap of `camera-core-1.6.2.aar` and `camera-lifecycle-1.6.2.aar`.

## 5. Webcam single frame with ffmpeg

Device on this PC: `/dev/video0` is `PC-LM1E` (uvcvideo). `/dev/video1` is its metadata node (no video formats). Formats (`v4l2-ctl -d /dev/video0 --list-formats-ext`):

- MJPEG: 1920x1080 at 30 fps (also 25, 20, 15, 10, 5), 1280x720, 1280x1024, 640x480, and smaller, all at 30 fps.
- YUYV: 1920x1080 only at 5 fps. Use MJPEG.

Working command (tested 2026-09-23): skip the first 30 frames (1 s at 30 fps) so exposure and white balance settle, then write one JPEG to stdout:

```sh
ffmpeg -hide_banner -loglevel error \
  -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 -i /dev/video0 \
  -vf "select=gte(n\,30)" -frames:v 1 \
  -q:v 2 -f image2pipe -c:v mjpeg - > frame.jpg
```

Result: exit code 0, `JPEG image data, baseline, 1920x1080`, about 30-60 KB, 2.2-3.0 s wall time.

- Without warm-up (`-frames:v 1` only, 0.7 s) the frame has a green color cast: the auto white balance is not settled. The frame after 30 frames has correct colors.
- In Python, set the warm-up frame count, size, and device as constants/config. In an argument list (`subprocess`/`asyncio.create_subprocess_exec`) the filter is `select=gte(n\,30)` without shell quotes: the `\,` escapes the comma for the ffmpeg filter parser.
- Only one process can open the device at a time. A second open fails with `Device or resource busy`. Map that to a clear error.
- `-q:v 2` is high JPEG quality (scale 2-31, lower is better).
