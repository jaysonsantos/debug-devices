# Report: dd-mcp

## What I did

- Added the Python project at the repo root: `pyproject.toml` and `uv.lock`. The build backend is `uv_build` with the module root `mcp/`. The entry point is `debug-devices-mcp`.
- Wrote the MCP server in `mcp/debug_devices_mcp/`:
  - `constants.py`: all names, defaults, and limits.
  - `config.py`: `Settings` with pydantic-settings. Each setting is a `--kebab-case` flag, a `DEBUG_DEVICES_*` variable, and a default. The server reads `.env` from the repo root. Durations are `timedelta`, given in seconds.
  - `phone_api.py`: a typed httpx client for `docs/phone-api.md`, with the models `Health`, `CameraStatus`, `ApiError`, and the request bodies.
  - `adb.py`: device selection, `adb forward`, and `am start`.
  - `webcam.py`: one JPEG frame through ffmpeg. The first 10 frames are skipped (warm-up).
  - `multimeter.py`: the OpenRouter request (image as a base64 data URL, strict `json_schema` response format), the `MultimeterReading` model, and one retry on an invalid answer.
  - `server.py`: the tools `phone_connect`, `phone_status`, `phone_zoom`, `phone_torch`, `phone_snapshot`, `webcam_snapshot`, and `multimeter_read`.
- Wrote the unit tests (94 with the dd-ui tests) in `mcp/tests/`. They use `httpx.MockTransport` for the phone and OpenRouter, and a fake command runner for `adb` and `ffmpeg`. The server tests use the in-process `mcp.Client`.
- Wrote `.mcp.json.example` and `mcp/README.md`.

## What works

- `uv run pytest`: 142 passed (includes the dd-ui tests).
- `uv run ruff check` and `uv run ruff format --check`: pass (this also covers `scripts/`).
- Stdio smoke test with `mcp.Client` and `StdioServerParameters` (`uv run --directory <repo> debug-devices-mcp`):
  - `tools/list` returns the 7 tools.
  - `webcam_snapshot` returns a real 1920x1080 JPEG from `/dev/video0` (about 70 KB).
  - `phone_connect` returns a tool error. The error lists the two Fire TV devices and asks for `--adb-serial`. The server ran only `adb devices -l`.
  - `multimeter_read` returns a tool error that names `OPENROUTER_API_KEY`. No network call.
- Real phone test before the app install, with `--adb-serial 7fad170e`: `phone_connect` returned a clear error: "the camera app dev.jayson.debugdevices.camera is not installed on 7fad170e. Build and install android/ first."
- Real phone test after the app install. Driver: `mcp.Client` over stdio, `debug-devices-mcp --adb-serial 7fad170e`. Only `7fad170e` got commands. dd-qa used the phone at the same time, so each result shows the state at that moment only.

  | Call | Result | Time |
  |---|---|---|
  | `phone_connect` | OK. `started_app: false`, app `0.1.0`, zoom 1.0, range 1.0 to 10.0, flash unit present | 194 ms |
  | `phone_status` | OK. zoom 1.0, torch off | 28 ms |
  | `phone_zoom step=in` | OK. zoom 1.5 | 320 ms |
  | `phone_zoom step=out` | OK. zoom 1.0 | 283 ms |
  | `phone_zoom ratio=2.0` | OK. zoom 2.0 | 264 ms |
  | `phone_zoom ratio=1000` | OK. clamped to 10.0 | 270 ms |
  | `phone_zoom ratio=0.01` | OK. clamped to 1.0 | 286 ms |
  | `phone_zoom` with no argument | Tool error: "give exactly one of `ratio` or `step`" | 4 ms |
  | `phone_torch enabled=true` | OK. `torch_enabled: true` | 360 ms |
  | `phone_torch enabled=false` | OK. `torch_enabled: false` | 293 ms |
  | `phone_snapshot save_path=...` | OK. Image content `image/jpeg`, 2,009,862 bytes, 3060x4080, EXIF model `2510ERA8BG`. The saved file is a correct back camera photo. | 1127 ms |

- Live multimeter test (one call, as the orchestrator allowed). Settings from `.env`: model `openai/gpt-6-luna`. The key did not go into any output.
  - `webcam_snapshot`: OK, 59,360 bytes. The webcam points at the ceiling. There is no multimeter in the frame.
  - `multimeter_read include_image=true`: tool error after 5.5 s. OpenRouter returned 400 from the provider (`Azure`): "Invalid schema for response_format 'multimeter_reading': context=('properties', 'mode'), $ref cannot have keywords {'description'}." The server did not crash.

## Decisions

- The installed SDK is `mcp` 2.2.0. In 2.x, `FastMCP` has the name `MCPServer` (`mcp.server.mcpserver`). The server uses `MCPServer`.
- Expected failures become `ToolError`. The model reads the message, and the server does not crash.
- `phone_connect` polls `/v1/health` and `/v1/status` until the camera is ready, or until `--app-start-timeout` (20 s) ends. It retries on `camera_not_ready`.
- The other phone tools do not forward the port. If the phone does not answer, the error tells the caller to run `phone_connect`.
- `multimeter_read` returns structured content (the `MultimeterReading` output schema) and a JSON text block. With `include_image`, it also returns the frame.
- The OpenRouter request sends the `X-Title` header. It does not send `HTTP-Referer`, because the repository URL is not known.

## Changes after the orchestrator update

- The default vision model is now `openai/gpt-6-luna`. I changed `constants.py`, `mcp/README.md`, and the tests.
- `adb.start_app` now raises `AppNotInstalledError` when the activity does not exist. This occurs with exit code 0 or 1.

## Bugs found in the real tests, and fixes

- **Strict schema refused (fixed).** Cause: pydantic writes the `mode` field as `{"$ref": "#/$defs/MeterMode", "description": ...}`. OpenAI strict mode refuses keywords next to `$ref`.
- **Noisy log (fixed).** The server wrote one `HTTP Request: ...` INFO line to stderr for each phone request. `__main__.py` now sets the `httpx` and `httpcore` loggers to WARNING. Stdout was not affected.

## Fixes from the orchestrator review

1. **Strict-mode schema.** `reading_json_schema()` in `multimeter.py` now:
   - puts each `$defs` entry (the `MeterMode` enum) in place of its `$ref`, and removes `$defs`,
   - writes nullable fields as `type: [x, "null"]`, not `anyOf`,
   - sets `required` to all properties and `additionalProperties: false` on every object,
   - removes `title`, `default`, `minimum`, and `maximum`. pydantic still checks `confidence` in [0, 1] when it parses the answer.

   The request has no `temperature`, `top_p`, or `stop`. It sends `provider: {"require_parameters": true}` and `max_tokens: 1000` (see `docs/research.md`). The test `test_sent_schema_is_strict` walks the sent schema. It fails on `$ref`, `$defs`, `anyOf`, missing `required` keys, or a missing `additionalProperties: false`. The test `test_strict_walker_catches_violations` checks the walker itself.
2. **Webcam crop.** New setting `--webcam-crop x,y,w,h` (`DEBUG_DEVICES_WEBCAM_CROP`), default none. The type is `Crop(x, y, width, height)` in `webcam.py`. ffmpeg crops on the PC, so only the crop goes to OpenRouter. The crop also applies to `webcam_snapshot`. Another module can set `Webcam.crop` while the server runs; the next frame uses the new value.
3. **Downscale.** `phone_snapshot` and `webcam_snapshot` have a new argument `max_side` (default 1568 px, long edge; 0 = full size). The new module `images.py` scales with Pillow and applies the EXIF orientation first. `save_path` writes the full-resolution JPEG. The JSON line has the returned size and the original size. New dependency: `pillow` 12.3.0.
4. **405 error code.** `ApiErrorCode.METHOD_NOT_ALLOWED = "method_not_allowed"` is in `phone_api.py`.

## Live test after the fixes

One stdio session with the settings from `.env` (model `openai/gpt-6-luna`, serial `7fad170e`). The key did not go into any output.

| Call | Result |
|---|---|
| `phone_connect` | OK, serial `7fad170e` |
| `phone_snapshot save_path=...` | OK. Returned 1176x1568, 124,228 bytes. Saved file 3060x4080, 2,012,826 bytes. |
| `webcam_snapshot save_path=...` | OK. Returned 1568x882, 131,641 bytes. Saved file 1920x1080, 209,422 bytes. |
| `multimeter_read` (the one allowed live call) | OK after 13.0 s. OpenRouter accepted the strict schema. |

`multimeter_read` result:

```json
{
  "readable": true,
  "value": 0.02,
  "unit": "V",
  "display_text": "0.02",
  "mode": "dc_voltage",
  "range": null,
  "flags": [],
  "confidence": 0.62,
  "notes": "The LCD is dim and slightly blurred; the reading appears to be 0.02 V."
}
```

The orchestrator said that the LCD was off. The saved webcam frame shows a PROSTER T21D with the LCD on. The LCD shows about `0.02`, and the dial is on a voltage position. Thus the reading agrees with the frame. The LCD is small in the full frame. A `--webcam-crop` around the meter will make the digits larger for the model.

## Process note

- I ran `ruff check --fix` and `ruff format` on the full repo one time (about 18:24:10), not only on `mcp/`. This changed three dd-qa files: `scripts/qa_contract.py`, `scripts/fake_phone.py`, and `scripts/qa_mcp_stdio.py`. The changes are ruff format and safe fixes only. dd-qa must check these files. After that, I ran ruff on `mcp` only.
- `ruff check` on the full repo now fails on `scripts/qa_mcp_stdio.py` (E501, PLR0915, ASYNC240). This file belongs to dd-qa. `uv run ruff check mcp` passes.

## Round 2: dd-qa bugs 3, 6, 7, 8

I kept the dd-ui changes in `server.py`, `config.py`, `multimeter.py`, and `__main__.py`: the `Monitor`, the `FrameSource` webcam, `VisionClient.model` as a setter, and the UI settings.

- **Bug 3 (reasoning budget).** The request now sends `"reasoning": {"effort": "low"}` and `max_tokens: 4000`. If `finish_reason` is `length`, the client raises `OutputTruncatedError`: "model ... stopped at the token limit (max_tokens=4000, reasoning effort low) before it finished the JSON answer". The client does not retry this case. Test: `test_length_stop_is_clear_and_not_retried`.
- **Bug 6 (bad 2xx body).** `phone_api._parse()` changes a pydantic `ValidationError` into `PhoneProtocolError` for `health`, `status`, `zoom`, and `torch`. Test: `test_bad_2xx_body_is_protocol_error`.
- **Bug 7 (larger downscale).** An image within `max_side` keeps its source bytes (this was already so). A larger image is encoded at quality 85, then 75, then 65. The first result that is not larger than the source is used. Tests: `test_scaled_image_is_not_larger_than_a_low_quality_source`, `test_image_within_max_side_keeps_source_bytes`.
- **Bug 8 (white space preview).** The error previews in `multimeter.py` and `phone_api.py` now strip the body first. Test: `test_error_preview_skips_leading_white_space`.
- **Error codes.** `ApiErrorCode` has `internal_error` (500) and `method_not_allowed` (405). Test: `test_error_codes`.
- **Dependencies.** `starlette` and `uvicorn` are direct dependencies. `mcp>=1` is now `mcp>=2.2`, because the code uses the 2.x API.

Checks: `uv run pytest` 94 passed. `uv run ruff check mcp` and `uv run ruff format --check mcp` pass.

## Round 2: live multimeter reads

Model `openai/gpt-6-luna`, reasoning effort low, no crop, full 1920x1080 frames. The key did not go into any output.

| # | Frame source | Latency | readable | value | mode | confidence |
|---|---|---|---|---|---|---|
| 1 | `multimeter_read` tool, `--no-ui` session | 10.4 s | false | null | other | 0.12 |
| 2 | monitor `http://127.0.0.1:18766/api/webcam/frame.jpg`, direct `VisionClient` | 7.4 s | false | null | other | 0.18 |
| 3 | same as 2 | 14.1 s | false | null | other | 0.17 |

- No request failed. No `finish_reason: length`. The strict schema and the reasoning settings work.
- Call 1: the saved frame shows a blank LCD at that time. `readable: false` is correct.
- Calls 2 and 3: the saved frames show `0.01` clearly, with the dial on a voltage position. `readable: false` is wrong (a false negative). Notes from the model: "The LCD is too blurred and dim to reliably read the digits".
- Two more `multimeter_read` calls in the `--no-ui` session failed at once: "Device or resource busy". The monitor (pid 227982, `debug-devices-mcp --adb-serial 7fad170e`) holds `/dev/video0` with its ffmpeg stream. The calls did not reach OpenRouter. As the orchestrator told me, I then took the frames from the monitor endpoint (driver script in my scratch directory).

### Why the model does not read a visible display (not tested)

- The meter fills about 15% of the 1920x1080 frame. OpenAI models can process an image at low detail when the request does not ask for high detail. At low detail, the digits are only a few pixels high.
- Proposal A (recommended): set `--webcam-crop` around the meter, for example `640,0,720,900` for the current position. Then the digits fill much more of the image. This change uses no code.
- Proposal B: send `"detail": "high"` in `image_url` (OpenAI image input option). I did not add this, because I cannot verify it without more live calls.
- Proposal C: if A and B do not help, try reasoning effort `medium`.

## Round 2: ground-truth reads (meter in resistance mode)

Ground truth from the user: the meter is on, in resistance mode. Expected: `mode: resistance`, an ohm-based unit (`Ω`, `kΩ`, `MΩ`), or `OL` for open probes.

Set-up: frames from the monitor endpoint `http://127.0.0.1:18766/api/webcam/frame.jpg` (1920x1080). Direct `VisionClient` calls, model `openai/gpt-6-luna`, reasoning effort low. Reads 2 and 3 used a crop `620,0,680,700` (Pillow in the driver; same effect as `--webcam-crop 620,0,680,700`). The meter now fills about half of the frame height.

| # | Image | Latency | What the frame shows (my check) | Expected | Actual | Match |
|---|---|---|---|---|---|---|
| 1 | full frame | 9.5 s | `1.309`, dial at the top position | resistance, Ω unit, about 1.309 | `diode`, `1.284 V`, readable, confidence 0.66 | No: mode, unit, and value are wrong |
| 2 | crop | 8.0 s | LCD blank (the frame caught the display between two updates) | readable false | `readable: false`, `other`, confidence 0.98 | Yes, for this frame |
| 3 | crop | 9.0 s | about `4.836` (or `483.6`); the decimal point is not clear | resistance, Ω unit | `dc_voltage`, `4.23 V`, confidence 0.62 | No: mode, unit, and value are wrong |

Result: 0 of 2 readable frames were correct. The mode was wrong each time (`diode`, `dc_voltage`), and the unit was always `V`. No request failed.

Findings:

- The resistance value changes from frame to frame (1.309, blank, about 4.8). The probes are probably open or not in good contact. A stable test needs a fixed resistor between the probes.
- The LCD can be blank in one frame. The model then correctly returns `readable: false`. `multimeter_read` can read 2 or 3 frames and use the most common reading. This is not implemented.
- On the PROSTER T21D, the Ω, diode, and continuity functions share one dial position. The dial alone does not give the mode. Only the small unit symbol at the right of the LCD gives it. That symbol is only a few pixels high, also in the crop. The model then guesses from the dial.
- The crop did not fix the problem. The digits are larger, but the unit symbol is still too small and blurred. The webcam focus is soft.

Proposals, in order:

1. Move the webcam nearer, or set a tighter crop on the LCD only, so that the unit symbol is clearly visible. Check the focus. This needs no code.
2. Change the prompt: "The unit symbol on the LCD (Ω, kΩ, MΩ, V, mV, A, F, Hz) decides the mode. If the dial position has more than one function, do not guess the mode from the dial." This is a small change in `multimeter.py`.
3. Send `"detail": "high"` for the image, and try reasoning effort `medium`.
4. Optional setting for a meter hint (for example `--multimeter-model "PROSTER T21D"`), added to the prompt.

Each proposal needs live calls to check it.

## Round 3: prompt, image detail, meter model, key check, phone source

Changes (in `multimeter.py`: the prompt and request parts; in `server.py`: `from_settings` and `multimeter_read`; I re-read both files first and kept the dd-ui `SharedWebcam` and `RemoteMonitor`):

- **Prompt.** The unit symbol and the annunciators on the LCD decide the mode. The dial only helps, because one dial position can have several functions. If the model cannot read the unit symbol, it says so in `notes` and sets confidence to 0.5 or lower.
- **Image detail.** `image_url.detail` is `high` (`ImageDetail` enum).
- **Meter model.** New setting `--meter-model` / `DEBUG_DEVICES_METER_MODEL` (free text, default empty). The user prompt then adds: "The meter is a PROSTER T21D. Use what you know about its display and dial." `VisionClient.meter_model` is a property with a setter, like `model`, so the monitor can change it at run time.
- **dd-qa R2-1.** `multimeter_read` calls `vision.require_api_key()` before it opens the webcam or asks the phone.
- **Phone source.** `multimeter_read(source="webcam" | "phone")`, default `webcam`. `MeterSource` is in `multimeter.py`. With `phone`, `capture_meter_frame()` in `server.py` takes one phone snapshot (full resolution) and scales it to the default `max_side` (1568). There is no crop for the phone. `include_image` returns the image that the model saw. No phone crop yet: it is simple to add (`Crop` and `crop_jpeg` exist), but nobody has asked for it.

Tests (new): `test_prompt_puts_the_lcd_before_the_dial`, `test_meter_model_goes_into_the_prompt`, the `detail: high` check in `test_request_shape_and_reading`, the meter model settings in `test_config.py`, `test_multimeter_without_key_does_not_open_the_webcam`, `test_multimeter_webcam_source_is_default`, `test_multimeter_phone_source_uses_scaled_snapshot`, and `test_multimeter_phone_source_without_phone`.

Checks: `uv run pytest` 118 passed. `uv run ruff check mcp` and `uv run ruff format --check mcp` pass.

## Round 3: live reads (ground truth: resistance mode, open probes, LCD shows O.L)

The monitor crop was still the old one (`591,87,509,804`). As the orchestrator said, I passed the crop explicitly: full frames from `http://127.0.0.1:18766/api/webcam/frame.jpg`, cropped in the driver with Pillow (same effect as `--webcam-crop`). Model `openai/gpt-6-luna`, reasoning effort low, detail high, new prompt. 3 calls. The key did not go into any output.

| # | Crop | Meter model | Latency | display_text | value | unit | mode | confidence | Expected: resistance, OL |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `740,215,270,125` (LCD) | `PROSTER T21D` | 7.8 s | `OL` | null | (empty) | resistance | 0.50 | Yes. The unit is empty, and the notes say that the unit symbol is not clear. |
| 2 | `740,215,270,125` (LCD) | none | 8.4 s | `OL` | null | `Ω` | resistance | 0.68 | Yes |
| 3 | `620,170,440,420` (meter and dial) | `PROSTER T21D` | 7.9 s | `OL` | null | `Ω` | resistance | 0.63 | Yes |

Result: 3 of 3 correct for mode and display. In round 2 (old prompt, no detail, soft focus), 0 of 2 were correct.

Notes:

- The LCD crop `740,215,270,125` cuts off the unit symbol at the right edge (I checked the sent image). Read 1 then did what the prompt asks: no unit, confidence 0.5, and a note. Proposal: `740,215,300,125`.
- The change from round 2 has three causes together: the user moved the meter and the focus is now sharp, the new prompt, and `detail: high`. These 3 calls do not show which cause is the most important.
- The meter model hint did not help in this test (read 1 against read 2). The sample is too small to draw a conclusion.

## Round 3: phone source live call

Skipped. Before the call, I took one phone snapshot (`GET /v1/snapshot`). The phone points at a circuit board, not at the meter. No OpenRouter call.

Before that snapshot: the phone had reconnected over USB (the ADB transport id changed from 19 to 20), and the ADB forward `tcp:18765` was gone. `/v1/health` on 18765 gave "connection refused". I ran `adb -s 7fad170e forward tcp:18765 tcp:8765` again (the same command that `phone_connect` runs). The monitor state still showed the phone as connected. Proposal for dd-ui: when a phone call gives `PhoneUnreachableError`, run the forward again one time. Another option: show "phone disconnected" in the monitor.

## Round 4: snapshot rotation

Contract: `POST /v1/rotation` with `{"degrees": 0 | 90 | 180 | 270}` or `{"auto": true}`, and `CameraStatus.rotation_degrees` and `rotation_locked`.

- `phone_api.py`: `RotationDegrees = Literal[0, 90, 180, 270]`. `CameraStatus` has `rotation_degrees` and `rotation_locked` as required fields. New request models `RotationLockRequest` and `RotationAutoRequest`, and `PhoneClient.rotation()`.
- `server.py`: new tool `phone_rotation(degrees: 0 | 90 | 180 | 270 | None = None, auto: bool = False)`. Give exactly one; otherwise a tool error. `build_server` had too many statements, so the tools are now in `register_phone_tools()` and `register_webcam_tools()`. `build_server` calls both after `monitor.instrument(server)`, as before. I re-read `server.py` first and kept the dd-ui changes (`SharedWebcam`, `RemoteMonitor`, `arm_exit_watchdog`).
- Tests: `test_rotation_body`, `test_rotation_degrees_are_checked`, `test_old_app_without_rotation_fields_is_a_protocol_error`, and `test_phone_rotation` (tool: lock, auto, both, neither, 45 degrees). The fake phone in `test_server.py` handles `/v1/rotation`.
- Checks: `uv run pytest` 142 passed. `uv run ruff check mcp` and `uv run ruff format --check mcp` pass.
- No live call: I did not change the phone state.

**Warning: the installed app is older than the contract.** `GET /v1/status` on the phone returns no `rotation_*` fields, and `POST /v1/rotation` returns 404 `not_found` (checked with one `{"auto": true}` request; the app refused it, so nothing changed). The new fields are required. After a restart, every MCP phone tool that returns `CameraStatus` fails with `PhoneProtocolError` ("... is not a CameraStatus") until dd-android installs the new APK. The monitor that runs now still has the old code in memory.

**For dd-ui (it owns the page):** yes, the page needs a rotation control. The phone can be mounted sideways, and then the snapshots come out rotated. Proposal:

- Four buttons `0`, `90`, `180`, `270` and one `Auto` button next to zoom and torch. They call `phone_rotation`.
- Show `rotation_degrees` and a "locked" or "auto" label with the other status values.
- A route like the others, for example `POST /api/phone/rotation` with `{"degrees": N}` or `{"auto": true}`.
- `app.js` reads the status fields by name. Check that the new fields do not break the status view.

## Open items

- **Webcam sharing.** With the monitor on, `multimeter_read` and `webcam_snapshot` from a second server process fail with "Device or resource busy". Inside the monitor process, `FrameSource` shares the stream. A second process (for example a `--no-ui` run by dd-qa) cannot capture. A second process can read the frame from the monitor endpoint instead. This is a design decision for dd-ui.
- The phone snapshot at full size is about 2 MB. The default `max_side` now keeps the returned image near 125 KB.
- Proposal for `.env.example` (not my path): add `DEBUG_DEVICES_WEBCAM_CROP=` with a comment (`x,y,w,h` in pixels; empty = full frame). Also add the optional variables `DEBUG_DEVICES_LOCAL_FORWARD_PORT`, `DEBUG_DEVICES_ADB_PATH`, and `DEBUG_DEVICES_FFMPEG_PATH`, each with a comment.
- Proposal for `docs/research.md` (not my path): add that OpenAI strict mode refuses keywords next to `$ref`.
- No other change to `docs/phone-api.md` is necessary.

## Status

DONE (round 2 fixes included). I do not edit `server.py`, `config.py`, `webcam.py`, or `multimeter.py` any more. dd-ui can integrate. Integration points:

- `Services.from_settings(settings)` and `build_server(services)` in `server.py`.
- `services.webcam.crop = Crop(x=..., y=..., width=..., height=...)` changes the crop at run time. `None` removes it.
- `services.webcam.capture_jpeg()` returns one JPEG. With the monitor stream open, a second process gets "Device or resource busy" (tested in round 2).

## Boardview

Brief: `bv-mcp.md`. Plan: `docs/research/boardview-claude.md` option A. Contract: `docs/boardview-json.md`.

### What I did

- New package `mcp/debug_devices_mcp/board/`:
  - `dump.py`: pydantic models of the contract (`BoardDump`, `DumpError`, enums for format, side, mounting, error code). Coordinates in mil.
  - `units.py`: the one helper pair `mil_to_mm` and `mm_to_mil`.
  - `model.py`: `Board` in mm. Part center and box from `p1`/`p2`, or from the pins (plus 0.3 mm) when the box is missing or has zero size. Indexes by part, net, and test point. Queries: `find_parts` (refdes, glob, mfgcode), `net_names`, `nearest_test_point`, `parts_near`. Test points are nails and parts named `TP*`, `PT*`, `TEST*`.
  - `loader.py`: `BoardviewLoader` runs `obv-dump` through the command runner with a timeout, parses exit 0 (`BoardDump`), exit 1 (`DumpError`), and crashes. It caches boards in memory by SHA-256. Keys go only to the `obv-dump` command line. A runner error (its text has the full command line) is not chained and not repeated.
  - `render.py`: Pillow PNG of one side: outline, part boxes, pins, labels that fit (font 16, then 11), highlighted parts and nets. Bottom is mirrored in X. `crop_to_part` makes a view of at least 15 mm around the part.
  - `homography.py`: DLT with Hartley normalization and SVD (numpy). Error check with 5+ pairs (limit 2 % of the photo point spread). `likely_outlier` names the wrong pair with 6+ pairs.
  - `tools.py`: `BoardSession` and the tools `board_open`, `board_find_part`, `board_part_pins`, `board_find_net`, `board_parts_near`, `board_render`, `board_register_photo`, `board_locate_in_photo`.
- `server.py` (small edits): `Services.board` (a default value, so other code that makes `Services` still works), set from the settings in `from_settings`, and `register_board_tools(server, services.board)` in `build_server`.
- `config.py`: `--obv-dump-path` (`BOARDVIEW_DUMP_BIN`), `--boardview-dump-timeout`, and the three keys (`BOARDVIEW_FZ_KEY`, `BOARDVIEW_CAE_KEY`, `BOARDVIEW_XZZ_KEY`). The environment names have no `DEBUG_DEVICES_` prefix, as in `.env.example`.
- `pyproject.toml`: new dependency `numpy`. Ruff `PLR0913`/`PLR0917` are off for `board/tools.py` only, with a comment: the arguments of a tool function are the tool API.
- Fixtures in `mcp/tests/fixtures/boardview/` (README with sources and licenses): `example.brd` (whitequark/kicad-boardview, 0BSD) and its real `obv-dump` output `example.brd.json` (local path removed), a hand-made `tiny.json`, and three `DumpError` files.
- Tests: `test_board.py` (model, loader, renderer), `test_board_tools.py` (tools through the in-process MCP client), `test_board_photo.py` (homography and photo tools), `test_board_target.py` (local only).
- `mcp/README.md`: new section "Boardview tools".

### What works

- `uv run pytest`: 179 passed, 1 skipped (the target test without `BOARDVIEW_TARGET`). `uv run ruff check mcp` and `uv run ruff format --check mcp` pass.
- `nix build .#obv-dump` (dd-research's derivation) builds. With it:
  - Stdio smoke test (`debug-devices-mcp --no-ui --obv-dump-path <nix store path>`) on the open example: `board_open`, `board_find_part`, `board_find_net`, `board_parts_near`, and `board_render` work. `board_open` on a file that is not a board gives "obv-dump could not read the board: unknown_format: Unrecognized file format".
  - Target test: `BOARDVIEW_TARGET=<target> BOARDVIEW_DUMP_BIN=<nix store path> uv run pytest mcp/tests/test_board_target.py -s`: passed. Result below.

Target board (local only, counts only):

| Item | Value |
|---|---|
| Format | `gencad` |
| Parts | 2,846 (1,625 top, 1,221 bottom, all `smd`) |
| Pins | 10,036, all with a net and a number |
| Nets | 2,109 |
| Nails | 0. Test points: 35 (`TP*` parts) |
| Outline | none (0 points, 0 segments) |
| `p1`/`p2` | set for 2,845 parts, but `p1 == p2` (zero size) for all. The server uses the pin boxes. |
| `rotation_deg`, `mfgcode` | set for all parts |
| Load time, first load | 1.66 s in total: `obv-dump` about 1.4 s (JSON 1.66 MB), validation 0.04 s, index 0.13 s. Second load: from the cache. |
| Render | 0.05-0.23 s. Whole board at 1568 px: about 6 px/mm, so few labels fit. A crop around a large part: 54 labels. |

I did not look at any image of the target. An image that I open goes to a model, and the brief forbids that. I checked the target renders only with numbers (size, parts drawn, labels drawn). I deleted the target JSON from my scratch directory.

### Contract findings and proposals (for the orchestrator and dd-research)

1. **`DumpError.format` is `null`.** `obv-dump` sends `"format": null` when the format is unknown (also for `io_error`). The contract says that empty text fields are `""`. The MCP now accepts both. Proposal: either `obv-dump` sends `""`, or the contract allows `null` for `DumpError.format`.
2. **Zero-size part boxes.** For the target (GenCAD from a converter), `p1 == p2` for every part. The contract says `null` only for 0,0. Proposal: `obv-dump` sends `null` when `p1 == p2`, or the contract says "a zero-size box means no box". The MCP already treats it as no box.
3. **Empty pin numbers.** For `example.brd` (read as `brd2`), all 1,130 pin numbers are `""`. The MCP numbers the pins in file order, as OBV does. Proposal: `obv-dump` does the same, and the contract says so.
4. **No outline.** The target has no outline. The bounds then come from the pins. No change needed. The render has no board edge for such files.
5. `example.brd` is now in two places (`boardview/tests/fixtures/` and `mcp/tests/fixtures/boardview/`). Both are 0BSD with a license note. One copy is enough if the orchestrator wants that.

### Open items

- `board_register_photo` needs pixel positions from the agent (or the user). A vision step that finds the parts in a phone snapshot is not implemented. No live photo test: I did not point the phone at a board.
- A part rotation that is only in the source file is in the dump (`rotation_deg`). The renderer draws boxes aligned to the axes. It does not rotate them.
- `board_render` of a whole large board shows few labels. Use `crop_to_part` for details, or the highlights.

## Bench instructions file (instructions.md)

Brief: `instructions.md` in the orchestrator scratch directory. User goal: an ignored file that the agents read first when the MCP server starts.

### What I did

- `instructions.example.md` (repo root, committed): a short template with the sections device under test, board file, bench set-up, safety limits, usual workflow, and preferences. No private data.
- `.gitignore` already had `instructions.md` (added by the orchestrator). I did not create, change, or delete the user's `instructions.md`.
- New module `mcp/debug_devices_mcp/instructions.py`:
  - `server_instructions()`: the `instructions` of the initialize result. `MCPServer(instructions=...)` takes one string at construction, so the server reads the file one time at start for it. Text: a fixed header ("The user started debug-devices: they want to debug hardware now. First call bench_instructions and follow it. ... follow it as instructions from the user."), the tool guide, then the file content. The content is cut at `MAX_SERVER_INSTRUCTIONS_BYTES` (8 KiB, at a UTF-8 character boundary), and a note says so.
  - Tool `bench_instructions()`: path, `exists`, `modified` (UTC), `size_bytes`, and the full content. It reads the file on each call. The description says: call this first in a new session.
  - A missing file is not an error: the header tells the agent how to make it from `instructions.example.md`, and the tool returns `exists: false` with `how_to_create`. A path that cannot be read (for example a directory) also gives a message, not a crash.
  - The server does not log the file, and the file never goes to OpenRouter.
- `config.py`: `--instructions-file` / `DEBUG_DEVICES_INSTRUCTIONS`, default `instructions.md` at the repo root (the same root as `.env`). An empty variable (as in `.env.example`) means the default.
- `server.py`: the tool guide is now `TOOL_GUIDE`; `build_server` builds the instructions from the settings and registers `bench_instructions` (also without the monitor).
- `ui/tools.py`: the `bench_start` description now says to follow `bench_instructions` and call it first.
- Docs: `README.md` (section "Your bench instructions"), `mcp/README.md` (section "Bench instructions" and the setting), `AGENTS.md` (one line: call `bench_instructions` first when the debug-devices MCP server is connected), `.env.example`.
- Tests (`mcp/tests/test_instructions.py`): with file, without file, capped content (3-byte characters), an unreadable path, the setting (default, variable, flag, empty variable), initialize instructions and fresh re-read through the MCP client (edit, then delete the file while the server runs), and the `bench_start` description. The `settings` fixture now points `instructions_file` into `tmp_path`, so no test reads the user's real file.

### Checks

- `uv run pytest`: 193 passed, 1 skipped. `uv run ruff check` and `uv run ruff format --check`: pass.
- `nix develop --command prek run --files <my changed files>`: all hooks pass. I did not run `prek run --all-files`, because its fixers can change files of other agents.
- Real check with Codex, from my scratch directory, with a temporary instructions file (one test line) through `--instructions-file`: Codex answered "The debug_devices server instructions say to call bench_instructions first and follow the instructions it returns", called `bench_instructions`, and printed the temporary path and the test line. I deleted the temporary file.

### Incident: the real file content came into my session

- What happened: my first Codex run was from the repo root. The project `.codex/config.toml` won over my `-c` override of `mcp_servers.debug_devices.args`, so the server read the real `instructions.md`. I printed the end of the Codex output with `tail`, and it had part of the real file.
- Where it went: only into this agent session. Not into the repository, a report, a commit, or a web service. I deleted the output file at once.
- Why: Codex uses the project config over a `-c` override for this key (not checked further). Outside the repo, `~/.codex/config.toml` has only `[mcp_servers.debug_devices.tools.bench_start]`, so a complete `-c` definition is necessary there.
- Change: the second run ran from my scratch directory and defined the server completely with `-c`. I printed only filtered lines.

### Open items

- The server instructions are read one time at start (the MCP initialize result is fixed). Edits of the file show in `bench_instructions` at once, but in the header only after a restart of the MCP server.
- `claude mcp get debug-devices` is pending approval, so I did not check with Claude Code.

## Evidence routing and visible markings

Brief: `evidence.md`. Problem: the phone showed the silkscreen marking "U730", the boardview had U7301 and U7302, and the agent used a boardview name without saying so. Agents also mixed the evidence sources.

### What I did

- **Evidence rules, one place.** `EVIDENCE_RULES` in `mcp/debug_devices_mcp/instructions.py`, one line per rule. `server_instructions()` puts it after the header and before the tool guide. The 8 KiB cap applies only to the user's file, so the rules are always complete.
  1. Visible things: a fresh `phone_snapshot`, never `webcam_snapshot`, `board_render`, or boardview data alone.
  2. Meter values: only `multimeter_read`.
  3. Board questions: the board tools; boardview data is supporting evidence; confirm on the device with `phone_snapshot`.
  4. Markings: quote as seen, then `board_match_marking`; say exact or candidates.
- **Tool descriptions:**
  - `phone_snapshot`: use it for every question about what is visible on the device.
  - `webcam_snapshot`: only for the meter framing and crop; not for the device and not to read the meter.
  - `board_render`: a drawing from the boardview file, not a photo, never proof of what is physically visible.
  - `multimeter_read`: the only tool for meter values. `source: phone` only when the phone points at the meter, never at the board (the image goes to the vision model).
  - `board_find_part`: boardview data is supporting evidence; for a visible marking, use `board_match_marking`.
- **New tool `board_match_marking(marking, side, registration_id, x_px, y_px)`** (new module `board/marking.py`, tool in `board/tools.py`):
  - The matcher ignores case, spaces, dashes, and underscores. Order: exact, prefix (cut-off or hidden silkscreen), contains, OCR confusion (O/0, I/l/1, S/5, B/8, Z/2, G/6), none. The first level with a match wins.
  - Result: `visible_marking` (as given), `resolution`, candidates (refdes, side, center, box, pin count, mfgcode, up to 5 nets), `total_candidates`, `best_candidate`, and `message`. Example: `Visible marking "U730" has no exact boardview part. Candidates: U7301, U7302. The silkscreen can be cut off or hidden. Tell them apart by position (board_register_photo, then x_px/y_px of the marking) or by their neighbours.`
  - With `registration_id` and `x_px`/`y_px`: each candidate gets its photo position and distance. They are sorted by distance. `best_candidate` is set only when the next candidate is at least `BEST_DISTANCE_RATIO` (2.0) times farther. An exact match is always the best candidate. `x_px`/`y_px` without a registration, or only one of them, is a tool error.
- **`board_find_part` fallback:** a new `match` field (`name_or_mfgcode`, `prefix`, `none`) and a `note`. Without a name, glob, or mfgcode match, it returns the prefix candidates with a note that points to `board_match_marking`.
- **Synthetic fixture** `mcp/tests/fixtures/boardview/markings.json`: all names and nets are invented (U7301, U7302, U7303 on the bottom, Q12, C8850, R10, J4, TP9). The fixture README lists it.
- **Tests** `mcp/tests/test_board_marking.py`: normalization and confusion, every resolution (also with the side filter and an empty marking), the tool without a position (message, candidate fields, errors), ranking by photo position (clearly near gives `best_candidate`, halfway gives none), the `board_find_part` fallback, and the evidence rules in the server instructions and in the tool descriptions.
- **Docs:** `README.md` (tool table, a short evidence paragraph), `mcp/README.md` (section "Evidence rules", `board_match_marking` in the table and its rules, `board_find_part` `match`), `AGENTS.md` (one evidence line).

### Checks

- `uv run pytest`: 209 passed, 1 skipped (includes new dd-ui tests). `uv run ruff check` and `uv run ruff format --check`: pass.
- `nix develop --command prek run --files <my files>`: all hooks pass. I did not run `prek run --all-files`.
- No live test: no board file and no photo. The tests use the synthetic fixture only. No board data and no photo went to OpenRouter or anywhere else.

### Open items

- The evidence rules are instructions and descriptions. The server cannot force an agent to follow them.
- `board_match_marking` does not read the photo itself. The agent must read the marking from `phone_snapshot` and give its pixel position.
