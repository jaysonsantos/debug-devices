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
- scrcpy uses the `adb` from the shell. Always give the serial: `scrcpy -s 0a1b2c3d`. With more than one device and no serial, scrcpy stops with an error, and the Fire TV devices are also visible to adb.

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

## Bench feedback 2: items 10 and 11 (dd-research-2)

Date: 2026-09-27. Brief: `docs/briefs/bench-feedback-2.md`, section "dd-research part". Bench rule kept: no phone, no webcam, no install, no server restart. Only fakes and a generated PDF.

### Item 10: `schematic_find`

- New module `mcp/debug_devices_mcp/schematic.py`:
  - `pdftotext -bbox <pdf> -` gives every word with its box (PDF points, origin top left). The server keeps this index until the file size or time changes.
  - Match order: exact word, then a name inside a joined word (`U7301,U7302`, `PP3V3(S5)`), then a part of a longer word (`R19` in `R190`). The last kind is listed only when there is no exact or joined hit; `other_contains_hits` counts it otherwise.
  - Each hit: page, word, box, page size, and the nearby text (the other words in the crop area, in reading order).
  - The first 3 hits get a PNG crop: `pdftoppm -f N -l N -r 150 -x -y -W -H -png -singlefile` (PNG on stdout). The margin is the named constant `defaults.MARGIN_PT` (72 pt). The hit has a red outline.
  - Errors say what to do: no schematic set (how to set it), a missing file, a query with spaces, a poppler failure.
  - Local only: the result says so (`local_only`). The server never sends the PDF, its text, or the crops to a web service.
- Configuration (`config.py`, `.env.example`): `DEBUG_DEVICES_SCHEMATIC` / `--schematic` (empty = none), `DEBUG_DEVICES_PDFTOTEXT_PATH`, `DEBUG_DEVICES_PDFTOPPM_PATH`, `DEBUG_DEVICES_SCHEMATIC_TIMEOUT` (60 s).
- `bench_instructions` returns `schematic` (the file that `schematic_find` reads, and a note when the file is missing). I did not add an MCP resource: the note in `bench_instructions` reaches every client.
- `flake.nix`: `pkgs.poppler-utils` (poppler 26.06.0) in the MCP group. The old name `poppler_utils` is a removed alias in nixpkgs.
- Tests: `mcp/tests/test_schematic.py` (16 tests). `mcp/tests/minimal_pdf.py` writes a small PDF with invented text; no real schematic is in the repository. The poppler tests skip when `pdftotext` or `pdftoppm` is not on `PATH`. A fake-runner test checks the exact poppler commands and that nothing else runs.
- `mcp/README.md`: a row in the tool table and a "Schematic" section.

### Item 11: rotated markings and SMD value codes

- New module `board/rotation.py`: the one table for a marking read upside down. Pairs that turn into each other: 6/9, 3/E, 7/L, n/u, d/p, b/q, M/W, m/w. Characters that stay the same: 0 O o 1 I l 8 S s Z z X x N H 5 2 (and space, dash, underscore). One way only: a turned 1 is read as T. `read_rotated_180` reverses the text and turns each character. A character without an upright form (for example `R`) gives no rotated reading.
- New module `board/value_code.py`: `decode_value_marking(text)` gives every value-code reading, each marked `interpretation_only`:
  - EIA 3-digit (`100` = 10 Ω, `472` = 4.7 kΩ, `000` = 0 Ω) and EIA 4-digit (`1002` = 10 kΩ). The last digit is the power of ten, 0-7.
  - R/K/M as the decimal point (`4R7` = 4.7 Ω, `R19` = 0.19 Ω, `4K7` = 4.7 kΩ). The note says that on an inductor, R codes are µH.
  - EIA-96 (`01C` = 10 kΩ, `68X` = 49.9 Ω), and a single `0` as a jumper.
  - An ambiguous code gives all readings (`47R`: 3.01 Ω as EIA-96, 47 Ω as R notation).
- New module `board/marking_readings.py`: `lookup_marking` matches the marking as seen and, with `try_rotations`, its rotated reading. The rotated reading wins only when its match level is better (exact < prefix < contains < confusion < none). The message says which reading matched and lists the value-code interpretations.
- `board_match_marking` gets `try_rotations` (default true). The result has three new fields: `reading` (`as_seen` or `rotated_180`), `rotated_reading`, and `value_interpretations`. With the bench example, `"00T"` gives `rotated_reading: "100"` and `100` = 10 Ω (EIA 3-digit, interpretation only).
- Tests: `mcp/tests/test_marking_rotation.py` (33 tests): the table (every pair turns both ways, every character has one role), the readings, the value codes, the lookup, and the MCP tool.

### For dd-mcp-2: my edits in your area

`board/marking.py` and `board/tools.py` are dd-mcp's files. My edits there are small:

- `board/marking.py`: 2 imports, and 3 new fields with defaults at the end of `MarkingMatch` (`reading`, `rotated_reading`, `value_interpretations`). No change to `find_marking_parts` or `message`.
- `board/tools.py`: 1 import; `match_marking` gets `try_rotations: bool = True` and calls `lookup_marking` instead of `find_marking_parts`; the new fields go into `MarkingMatch`; the tool gets `try_rotations` and 4 more lines of description.
- The logic is in my new modules (`board/rotation.py`, `board/value_code.py`, `board/marking_readings.py`), so later changes in your files do not need to touch it.
- Also: `config.py` (a `schematic` region and `_none_if_empty`), `instructions.py` (the `schematic` field and one optional parameter), and `server.py` (one import and 11 lines in `build_server`).

### Checks

| Check | Command | Result |
|---|---|---|
| New tests | `uv run pytest mcp/tests/test_schematic.py mcp/tests/test_marking_rotation.py` | 49 passed |
| Related tests | the above plus `test_board_marking.py`, `test_instructions.py`, `test_config.py` | 74 passed |
| All Python tests | `uv run pytest` | 670 passed, 1 skipped (2026-09-27 about 22:45, with the other agents' changes of that time) |
| Lint | `ruff check` and `ruff format --check` on my files | pass |
| Hooks | `prek run --files` on my files, `flake.nix`, `.env.example`, `mcp/README.md` | pass |
| Poppler | `nix develop --command pdftotext -v` | 26.06.0 |

### Notes

- **Dev reload.** Several `debug-devices-mcp-dev` processes run. The reload proxy restarts its server when a `.py` file in `mcp/debug_devices_mcp/` changes. Other agents changed package files at the same time (22:08-22:35), so the reloads were already happening. I did not start, stop, or restart a server myself. If the live bench server is a `-dev` process, it reloaded with each change.
- I ran `git add -N` (intent to add, no content) on my new files, so that the hooks see them. I did not commit.
- The schematic path is read at server start. A new path needs a server restart (after "bench done").

### Open

- Rotation covers 180 degrees only. A marking read at 90 degrees needs another table (most characters have no 90-degree reading).
- An upper-case `U` in a turned photo (a turned `n`) has no entry, because the table keeps only the lower-case pair `n`/`u`.
- Value codes cover resistors only. Capacitor codes (pF with the 3-digit code) and the EIA 3-digit multipliers 8 and 9 are not decoded.
- `schematic_find` needs a PDF with text. A scanned schematic (images only) gives no hits. OCR is not in scope.
