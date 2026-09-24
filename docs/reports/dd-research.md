# Report: dd-research

## What I did

- `flake.nix` and `flake.lock`: dev shell for all parts. nixpkgs `nixos-unstable` (locked 2026-09-22), `forAllSystems`, one comment per package group.
  - Python: `python314` (3.14.7), `uv` 0.12.17, `ruff` 0.16.8, `ffmpeg` 9.0.1, `v4l-utils` (Linux only).
  - Android: `jdk17`, `gradle_9` (9.7.1), `android-tools`, `ktlint` 1.8.0, and an SDK from `androidenv.composeAndroidPackages` (platform `37.0`, build-tools 37.0.0, no emulator, no NDK). The flake accepts the SDK license (`android_sdk.accept_license`, `allowUnfree`).
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
| SDK | `nix develop --command sdkmanager --list_installed` | `build-tools;37.0.0`, `platforms;android-37.0`, platform-tools 37.0.1 |
| aapt2 | `$ANDROID_HOME/build-tools/37.0.0/aapt2 version` | `2.20-15087165` |
| Android build | smoke project in the scratchpad: AGP 9.4.1, `compileSdk = 37`, `targetSdk = 37`, `buildToolsVersion = "37.0.0"`, built-in Kotlin 2.4.20, serialization, CameraX 1.6.2, Ktor CIO 3.6.0; `gradle assembleDebug` | `app-debug.apk` made; `aapt2 dump badging` shows `compileSdkVersion='37'` |
| Hooks | `nix develop --command prek run --all-files` | all pass (only on files that git knows) |
| MCP SDK | probe server with `mcp==2.2.0`, in-process `Client` | image + `structured_content` + `outputSchema` work |
| Webcam | the ffmpeg command in `docs/research.md` section 5 on `/dev/video0` | 1920x1080 JPEG, exit 0, about 2.5 s |

## Important findings for other agents

1. **dd-mcp: `mcp` 2.x removed `FastMCP`.** Use `from mcp.server.mcpserver import MCPServer, Image` and types from `mcp_types`. For image plus JSON, return `Annotated[CallToolResult, Model]`. See `docs/research.md` section 2. If you pinned `mcp<2`, that also works, but 2.2.0 is current.
2. **dd-mcp: `openai/gpt-6-luna` does not support `temperature`.** Do not send `temperature`, `top_p`, or `stop`. Send `response_format` (json_schema, strict) and `provider.require_parameters: true`.
3. **dd-mcp: webcam warm-up is necessary.** The first frame has a green cast. Skip 30 frames at 30 fps (`select=gte(n\,30)`). Use MJPEG input; YUYV 1080p is only 5 fps.
4. **dd-android: AGP 9 has built-in Kotlin.** Do not apply `org.jetbrains.kotlin.android`. `ListenableFuture.await()` needs `androidx.concurrent:concurrent-futures-ktx`. `setZoomRatio` fails outside `[min, max]`: clamp first.
5. The SDK is in the read-only Nix store. Gradle cannot download SDK packages. If the app needs another platform or build-tools version, change `flake.nix`.

## Update: Android platform 37

- `flake.nix` now has platform `37.0` and build-tools 37.0.0. I removed platform 36 and build-tools 36.0.0: the build does not need them.
- nixpkgs names the platform `"37.0"`. The SDK directory is `platforms/android-37.0`. AGP 9.4.1 finds it with `compileSdk = 37`. No `compileSdk { ... minorApiLevel ... }` block is necessary.
- `GRADLE_OPTS` now points to `build-tools/37.0.0/aapt2`.
- dd-android: set `compileSdk = 37`, `targetSdk = 37`, and `buildToolsVersion = "37.0.0"` in `android/`. An `android/` build that still says `compileSdk = 36` now fails, because platform 36 is not in the shell.

## Update: scrcpy

- `flake.nix` now has `scrcpy` 4.1 in the Linux shells (`x86_64-linux`, `aarch64-linux`), next to `v4l-utils`. It is for the monitor feature. The `aarch64-darwin` shell does not have it.
- Verify: `nix develop --command scrcpy --version` prints `scrcpy 4.1`. `nixfmt --check flake.nix` passes. I did not start scrcpy.
- scrcpy uses the `adb` from the shell. Always give the serial: `scrcpy -s 7fad170e`. With more than one device and no serial, scrcpy stops with an error, and the Fire TV devices are also visible to adb.

## Changes outside my paths

- I ran `git add -N` (intent to add, no content staged) on `flake.nix`, `flake.lock`, `.pre-commit-config.yaml`, and `.editorconfig`. A flake in a git repository sees only files that git knows. The orchestrator commits them.
- I did not touch `android/`, `mcp/`, or `docs/phone-api.md`. No proposal for the contract.
- I did not use adb. The phone and Fire TV devices were not touched.

## Open

- `prek run --all-files` checks only files that git knows. After the first commit, run it again on the whole repository (ruff, ktlint, taplo then have files to check).
- I tested only the `x86_64-linux` shell. I did not build the `aarch64-darwin` and `aarch64-linux` shells.
- OpenRouter calls were not made (no API key). The model facts come from the public `/api/v1/models` endpoint.
- The orchestrator's model change (`openai/gpt-6-luna`) is in `docs/research.md`. My paths have no code or tests that name the model.

## Boardview: obv-dump

Date: 2026-09-24. Plan: `docs/research/boardview-claude.md` option A. Contract: `docs/boardview-json.md`.

### What I did

- `boardview/` (new, C++ only, plus the test files):
  - `src/main.cpp`: `obv-dump`. It selects the parser in the same order as `BoardView::LoadFile`, and it writes `BoardDump` or `DumpError` with nlohmann_json.
  - `include/SDL.h`: a stub. The parsers use SDL only for log output, which goes to stderr.
  - `CMakeLists.txt`: compiles only the parsers, `utils.cpp`, `Crypto/des.c`, `mpc.c`, and the generated GenCAD grammar.
  - `patches/0001-keep-part-rotation.patch` (15 added lines): adds `has_rotation` and `rotation_deg` to `BRDPart`. GenCAD, FZ, and Altium ASCII (`ad`) set them.
  - `LICENSE.OpenBoardView` (the OBV MIT notice), `README.md`.
  - `tests/test_obv_dump.py` (pytest, 14 tests), `tests/run.sh`, `tests/fixtures/` with a license note for each file.
- `flake.nix`: `packages.<system>.obv-dump`. It fetches OBV at tag `10.0.0` (pinned hash), and `mpc` and `utf8.h` at the gitlink revisions of that tag (pinned hashes). It applies `boardview/patches/*.patch` with `applyPatches`, builds with CMake, and installs the license texts in `share/licenses/obv-dump/`. The dev shell has `obv-dump` on `PATH`.
- `.pre-commit-config.yaml`: one top-level `exclude: ^boardview/(tests/fixtures|patches)/`. Without it, the hygiene hooks changed the fixtures (line endings, final newline) and the patch (trailing white space).

### Target file

The local file (GenCAD 1.4, 90,037 lines, 2.4 MB) parses without a fix:

| Item | Value |
|---|---|
| Command | `BOARDVIEW_TARGET=<file> nix develop --command boardview/tests/run.sh` (14 passed) and `obv-dump <file>` |
| Time | 1.2 s (the whole run of `obv-dump`) |
| Format | `gencad` |
| Parts | 2,846 (1,625 top, 1,221 bottom). Names are unique. Examples: `C6312`, `C7160`, `D0701` |
| Pins | 10,036. Each pin side is the same as its part side. |
| Nets | 2,109 (example: `R2_OUT_MCU_X1_RSUB`) |
| Nails | 0 |
| Outline | none: the file has no `$BOARD` section. `outline` and `outline_segments` are empty. |
| Rotation | `0.0` on all parts: every `COMPONENT` has `ROTATION 0`. The converter ("XY html to CAD") writes one `SHAPE` per part with the pins already in position. |
| Part box | `p1 == p2` (the GenCAD `PLACE` point) for 2,845 parts. `null` for `U0301` (placed at 0,0). |

Conclusion for the MCP server: for this file, compute the part box from the pins, and compute the board outline from the pin extent. Pin positions are the reliable data.

### Checks

| Check | Command | Result |
|---|---|---|
| Version | `nix develop --command obv-dump --version` | `obv-dump 0.1.0 (OpenBoardView 10.0.0)` |
| Clean build | `nix build .#obv-dump --rebuild` | about 10 s, no compiler warnings (only the normal nixpkgs CMake "unused-cli" note) |
| Tests | `nix develop --command boardview/tests/run.sh` | 13 passed, 1 skipped (the local-only target test) |
| Hooks | `prek run --files` on all changed files | all pass |

Fixture results (OBV 10.0.0 with the patch):

| Fixture | Format | Parts | Pins | Nails | Outline |
|---|---|---|---|---|---|
| `example.brd` (kicad-boardview, 0BSD) | `brd2` | 245 | 1,130 | 19 | 73 points |
| `example.bvr` (kicad-boardview, 0BSD) | `bvr3` | 272 | 1,149 | 0 | 73 points |
| `rotation.cad` (written for this repository) | `gencad` | 2 | 4 | 0 | 4 segments |

The tests also check `unknown_format`, `io_error`, `key_required` (`.fz` without a key), `key_invalid` (the output does not contain the key), and exit 2 for a bad key flag.

I did not add the GenCAD 1.4 specification example. The only copy that I found is in `cyrozap/gencad-rs`, which is GPL-3.0. `rotation.cad` is our own file and tests the GenCAD path (rotation, bottom side, mirrored X).

### License of `Crypto/des.c`

`github.com/dhuertas/DES` has an MIT license: "MIT License, Copyright (c) 2020 Dani Huertas" (file `LICENSE`, added 2020-07-21). The other parts: OpenBoardView MIT, `mpc` BSD 2-Clause (Daniel Holden, 2013), `utf8.h` public domain (Unlicense text). All are compatible with each other and with the repository.

### Contract proposals for `docs/boardview-json.md`

`obv-dump` does these things now. Please add them to the contract, or tell me to change the code:

1. **Test-pad parts.** OBV adds parts named `...` for test pads (`BRDFileBase::AddNailsAsPins`, `BRDBoard::kComponentDummyName`). `obv-dump` does not write these parts. It writes their pins as `nails`, and it removes duplicate nails (same x, y, side, net). Without this, part names are not unique (`...` exists once for each side).
2. **Repeated part names.** Real files repeat names. The KiCad example has seven mounting holes named `REF**`. The second and later ones get a suffix: `REF**#2` ... `REF**#7`. The suffix never collides with an existing name.
3. **`first_pin`** is `null` when `pin_count` is 0. The contract example shows only an integer.
4. **`p1 == p2`.** GenCAD gives only the placement point, so `p1` and `p2` are the same point. Add the rule: when `p1 == p2`, the MCP server computes the box from the pins, as for `null`.
5. **`rotation_deg`** is the value from the file, in degrees, without a change for the bottom side. It is set for `gencad` (0 when the component has no `ROTATION`), `ad`, and `fz` (only when the field is a number). All other formats give `null`. A real value of `0.0` can also mean "the converter put the rotation into the shape" (see the target file).
6. **Key flags.** `--fz-key` and `--cae-key`: 44 hex words (0x prefix optional), separated by commas or spaces, as in `obv.conf`. `--xzz-key`: one 64-bit hex value.
7. **Exit 2** for a bad command line (usage text on stderr, no JSON). The contract has only 0 and 1.
8. **Allegro binary `.brd`**: `unknown_format` with `format: null` and the message "Allegro binary .brd files are not supported". Add `allegro` to the format list if the MCP server must show a better message.

### Open

- The FZ and Altium ASCII rotation lines are not tested: no open sample files exist. The FZ change only reads a field that OBV already skipped.
- XZZ, CAE, and FZ with real keys are not tested (no open samples, no keys).
- `.pre-commit-config.yaml` was not in the path list of this brief. I created that file in task 1, and the change is one `exclude` line.
