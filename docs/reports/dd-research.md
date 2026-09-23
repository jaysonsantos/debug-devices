# Report: dd-research

## What I did

- `flake.nix` and `flake.lock`: dev shell for all parts. nixpkgs `nixos-unstable` (locked 2026-09-22), `forAllSystems`, one comment per package group.
  - Python: `python314` (3.14.7), `uv` 0.12.17, `ruff` 0.16.8, `ffmpeg` 9.0.1, `v4l-utils` (Linux only).
  - Android: `jdk17`, `gradle_9` (9.7.1), `android-tools`, `ktlint` 1.8.0, and an SDK from `androidenv.composeAndroidPackages` (platform 36, build-tools 36.0.0, no emulator, no NDK). The flake accepts the SDK license (`android_sdk.accept_license`, `allowUnfree`).
  - Linters: `prek`, `typos`, `taplo`, `nixfmt`, `shellcheck`.
  - Environment: `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `JAVA_HOME`, `GRADLE_OPTS` with `android.aapt2FromMavenOverride` to the SDK aapt2, `UV_PYTHON_DOWNLOADS=never`, `UV_PYTHON_PREFERENCE=only-system`.
- `.pre-commit-config.yaml`: prek built-in hygiene hooks (merge conflicts, end of file, trailing white space, YAML, JSON, TOML, large files, line endings) and local `language: system` hooks: ruff check, ruff format --check, ktlint, typos, taplo, nixfmt, shellcheck.
- `.editorconfig`: LF, UTF-8, 2 spaces; 4 spaces and 120 columns for Python and Kotlin; `ktlint_code_style = android_studio`.
- `docs/research.md`: toolchain versions (section 1), MCP SDK (2), OpenRouter (3), CameraX (4), webcam capture (5).

## What works

| Check | Command | Result |
|---|---|---|
| Python | `nix develop --command python3 --version` | `Python 3.14.7` |
| uv | `nix develop --command uv --version` | `uv 0.12.17` |
| Gradle | `nix develop --command gradle --version` | `Gradle 9.7.1`, JVM 17.0.20.1 |
| SDK | `nix develop --command sdkmanager --list_installed` | build-tools 36.0.0, platforms android-36, platform-tools 37.0.1 |
| aapt2 | `$ANDROID_HOME/build-tools/36.0.0/aapt2 version` | `2.20-13193326` |
| Android build | smoke project in the scratchpad: AGP 9.4.1, built-in Kotlin 2.4.20, serialization, CameraX 1.6.2, Ktor CIO 3.6.0; `gradle assembleDebug` | `app-debug.apk` made |
| Hooks | `nix develop --command prek run --all-files` | all pass (only on files that git knows) |
| MCP SDK | probe server with `mcp==2.2.0`, in-process `Client` | image + `structured_content` + `outputSchema` work |
| Webcam | the ffmpeg command in `docs/research.md` section 5 on `/dev/video0` | 1920x1080 JPEG, exit 0, about 2.5 s |

## Important findings for other agents

1. **dd-mcp: `mcp` 2.x removed `FastMCP`.** Use `from mcp.server.mcpserver import MCPServer, Image` and types from `mcp_types`. For image plus JSON, return `Annotated[CallToolResult, Model]`. See `docs/research.md` section 2. If you pinned `mcp<2`, that also works, but 2.2.0 is current.
2. **dd-mcp: `openai/gpt-6-luna` does not support `temperature`.** Do not send `temperature`, `top_p`, or `stop`. Send `response_format` (json_schema, strict) and `provider.require_parameters: true`.
3. **dd-mcp: webcam warm-up is necessary.** The first frame has a green cast. Skip 30 frames at 30 fps (`select=gte(n\,30)`). Use MJPEG input; YUYV 1080p is only 5 fps.
4. **dd-android: AGP 9 has built-in Kotlin.** Do not apply `org.jetbrains.kotlin.android`. `ListenableFuture.await()` needs `androidx.concurrent:concurrent-futures-ktx`. `setZoomRatio` fails outside `[min, max]`: clamp first.
5. The SDK is in the read-only Nix store. Gradle cannot download SDK packages. If the app needs another platform or build-tools version, change `flake.nix`.

## Changes outside my paths

- I ran `git add -N` (intent to add, no content staged) on `flake.nix`, `flake.lock`, `.pre-commit-config.yaml`, and `.editorconfig`. A flake in a git repository sees only files that git knows. The orchestrator commits them.
- I did not touch `android/`, `mcp/`, or `docs/phone-api.md`. No proposal for the contract.
- I did not use adb. The phone and Fire TV devices were not touched.

## Open

- `prek run --all-files` checks only files that git knows. After the first commit, run it again on the whole repository (ruff, ktlint, taplo then have files to check).
- I tested only the `x86_64-linux` shell. I did not build the `aarch64-darwin` and `aarch64-linux` shells.
- OpenRouter calls were not made (no API key). The model facts come from the public `/api/v1/models` endpoint.
- The orchestrator's model change (`openai/gpt-6-luna`) is in `docs/research.md`. My paths have no code or tests that name the model.
