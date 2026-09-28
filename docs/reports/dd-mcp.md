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
- Real phone test before the app install, with `--adb-serial 0a1b2c3d`: `phone_connect` returned a clear error: "the camera app dev.jayson.debugdevices.camera is not installed on 0a1b2c3d. Build and install android/ first."
- Real phone test after the app install. Driver: `mcp.Client` over stdio, `debug-devices-mcp --adb-serial 0a1b2c3d`. Only `0a1b2c3d` got commands. dd-qa used the phone at the same time, so each result shows the state at that moment only.

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

One stdio session with the settings from `.env` (model `openai/gpt-6-luna`, serial `0a1b2c3d`). The key did not go into any output.

| Call | Result |
|---|---|
| `phone_connect` | OK, serial `0a1b2c3d` |
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
- Two more `multimeter_read` calls in the `--no-ui` session failed at once: "Device or resource busy". The monitor (pid 227982, `debug-devices-mcp --adb-serial 0a1b2c3d`) holds `/dev/video0` with its ffmpeg stream. The calls did not reach OpenRouter. As the orchestrator told me, I then took the frames from the monitor endpoint (driver script in my scratch directory).

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

Before that snapshot: the phone had reconnected over USB (the ADB transport id changed from 19 to 20), and the ADB forward `tcp:18765` was gone. `/v1/health` on 18765 gave "connection refused". I ran `adb -s 0a1b2c3d forward tcp:18765 tcp:8765` again (the same command that `phone_connect` runs). The monitor state still showed the phone as connected. Proposal for dd-ui: when a phone call gives `PhoneUnreachableError`, run the forward again one time. Another option: show "phone disconnected" in the monitor.

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

## Hot reload proxy for local development

Brief: `reload.md`. Goal: the MCP server that Claude Code or ChatGPT desktop (Codex) uses runs the new code after a change, without a restart of the app.

### What I did

- New module `mcp/debug_devices_mcp/devreload.py` and entry point `debug-devices-mcp-dev` (`pyproject.toml`). No new dependency (`watchfiles` is not in `uv.lock`): the proxy polls the modification times.
  - Child: `python -m debug_devices_mcp <same arguments>`, from the same virtual environment (the project is installed as editable, so a new child imports the new code). The child runs in its own process group. Its stderr goes to the proxy's stderr. The proxy's stdout carries only JSON-RPC.
  - Watch: `.py`, `.html`, `.js`, `.css` under `mcp/debug_devices_mcp/` (not `__pycache__`), poll every 1 s, debounce 300 ms (`ReloadOptions`).
  - It keeps the client's `initialize` and `notifications/initialized`, and sets `capabilities.tools.listChanged = true` in the `initialize` answer to the client.
  - Reload: stop the child, start a new child, replay `initialize` with its own id (the answer does not go to the client) and `initialized`, then send `notifications/tools/list_changed`. Log: `[devreload] reloaded (N files changed)`.
  - Queue: client messages during a reload wait and go to the new child. Requests that the old child did not answer get JSON-RPC error -32001 "debug-devices reloaded its code; call the tool again".
  - Broken code: when the new child exits or does not answer `initialize` within 30 s, the proxy logs the problem, and requests get error -32002 "debug-devices cannot start after a code change: <problem>. Fix the code; it retries then." It tries again at the next change. A child that stops by itself (a crash) is handled the same way.
  - Client EOF: stop the child, exit.
- **SIGTERM finding.** The server has no SIGTERM handler: the default action ends Python without the lifespan cleanup, so the webcam ffmpeg stream and scrcpy can stay behind. So the proxy stops a child like this: close its stdin first (the server cleans up; the exit watchdog ends it within 2 s), SIGTERM after `stop_timeout` (5 s), then SIGKILL, both to the child's process group so that ffmpeg and scrcpy also stop. Proposal for the server owner: a SIGTERM handler that runs the same cleanup as a stdin EOF.
- `scripts/mcp-server.sh`: `--dev-reload` as the first argument, or `DEBUG_DEVICES_DEV_RELOAD=1`, runs `debug-devices-mcp-dev`. The cached dev-shell environment stays the same. The script header documents it.
- Tests `mcp/tests/test_devreload.py` (7): a fake child script in `tmp_path` answers `initialize` and a tool with a version from a file. Covered: the file snapshot, forward and `listChanged`, the initialize replay (checked in the child's log: our id, then `initialized`), the queue during a slow restart, the in-flight error (also covers the SIGTERM step: the fake tool sleeps 60 s), a broken child then recovery, and client EOF. They passed 3 times in a row (about 2.5 s). No fake child stays behind.
- Docs: `README.md` ("Reload inside an MCP client"), `mcp/README.md` ("Reload proxy"), `AGENTS.md` (commands line), `.env.example` (a comment line: the script reads `DEBUG_DEVICES_DEV_RELOAD` from its own environment, not from `.env`).

### Checks

- `uv run pytest`: 217 passed, 1 skipped. `uv run ruff check` and `uv run ruff format --check`: pass.
- `nix develop --command prek run --files <my files>`: all hooks pass, shellcheck included.
- Real check: `mcp.Client` over stdio with `scripts/mcp-server.sh --dev-reload --no-ui-open-browser --instructions-file <temporary file>`. I used a temporary instructions file, so that the user's real `instructions.md` did not come into the output. Call 1 of `bench_instructions` answered. Then I changed only the modification time of `mcp/debug_devices_mcp/instructions.py`. Stderr: `[devreload] reloaded (1 files changed)`. Call 2 answered from the new server. `git diff HEAD -- mcp/debug_devices_mcp/instructions.py` is empty: no source change stays. I deleted the temporary files.
- The first real run found a bug: at client EOF, the proxy logged "the server exited with code 0" as a crash. Fixed with a `closing` flag. A test checks it.

### Open items

- The old server's monitor page logs "timeout graceful shutdown exceeded" from uvicorn at each reload (open page connections). The stop still ends within the timeout. The dd-ui owner can shorten the uvicorn graceful timeout.
- A code change in the proxy itself (`devreload.py`) reloads the child, not the proxy. A change of the proxy needs a restart of the MCP client.
- Clients must support `notifications/tools/list_changed` to see new or changed tools. Changed code of existing tools works also without it.

## Bench workflow P1-P2, round 1: meter check

Brief: `docs/briefs/p1-p2-evidence.md`. Source: `docs/reports/bench-workflow-improvements.md` ("P1: Check meter mode and unit before a conclusion").

### What I did

- `multimeter.py`, new region "check":
  - `UnitFamily` with an explicit `unknown`. `unit_family()` reads the model's unit text: an SI prefix (`p n µ/u m k M`) and a known symbol (Ω in both code points, "ohm", V, A, F, Hz, °C/°F, %). Empty text, "unknown", "?", and any other text are `unknown`.
  - `MeterStatus`: `confirmed`, `uncertain`, `disputed`, `unreadable`.
  - `check_reading(reading, expected_mode)`: unit family against the mode (`MODE_FAMILIES`: Ω for resistance and continuity, V for voltage and diode, A for current, F, Hz, °C/°F, %). A conflict gives `disputed`. An unknown unit, the mode "other", or a confidence below `MIN_CONFIRMED_CONFIDENCE` (0.7) gives `uncertain`. `expected_mode` is context: a unit that does not fit it gives `disputed`; another mode of the same family gives `uncertain`; it never raises a status.
  - `MeterResult`: the model's fields at the top level (the monitor page reads `readable`, `display_text`, `unit`, `mode`, `range`, `confidence`, `flags`, `notes`), plus `status`, `unit_family`, `overload` (OL, 0L, O.L), `value` (only for `confirmed`, and not for an overload), `expected_mode`, `problems`, and `request` ("Take a new frame that shows the LCD symbols and the dial together ... If the symbols stay unclear, ask the user to confirm the physical meter mode.").
  - Prompt and schema: the model writes `"unknown"` in `unit` when it cannot read the unit symbol, and never takes the unit from the dial alone.
- `server.py`, `multimeter_read`: new argument `expected_mode`; it returns `MeterResult`. `include_image` still returns the exact image that went to the model, and the monitor log keeps it.
- `instructions.py`, evidence rule 2: only `confirmed` is a measurement; for the other states, no numeric conclusion; `expected_mode` is context, never proof.
- Tests (`test_multimeter.py`, `test_server.py`): unit families; clear Ω, kΩ, MΩ, V, and OL; an obscured unit symbol (uncertain, no value); "443 V" in a resistance test (disputed, no value); a unit that does not fit the mode; `expected_mode` never confirms; unreadable; the tool with `expected_mode` and `include_image`.

### Checks

- `uv run pytest`: 374 passed, 1 skipped.
- Ruff on my files: pass.

### Process note

- I ran `ruff check --fix mcp` and `ruff format mcp` one time on the whole `mcp/` directory by mistake. It changed `mcp/tests/test_devices.py` (dd-ui's file) at 19:50:29: ruff format and safe fixes only. dd-ui must check it. After that I ran ruff only on a list of my files.
- For dd-ui: the monitor page shows `display_text unit` for every result. It can show `status` too, so that a disputed "443 V" does not look like a measurement.

## Bench workflow P1-P2, round 2: capture ids

Source: "P1: Tie statements to the exact frame".

### What I did

- New module `mcp/debug_devices_mcp/evidence.py`:
  - `CaptureLog`: records a `Capture` (UUID v7 `capture_id`, UTC `captured_at`, kind `phone_snapshot` or `meter_image`, source, scene epoch). It keeps the last 500 captures.
  - Scene epoch: the log listens to the existing scene detector (`SceneState.add_listener`). Each scene change starts a new epoch.
  - `status(capture_id)`: `valid_for_position_claims` only for a `phone_snapshot` of the current epoch while the scene is not marked as changed. A meter image, an unknown id, or a photo from before a move is not valid, with the reason.
  - `require_current_photo(capture_id)`: the check for tools that take a photo id (rounds 3 and 4); it raises `StaleCaptureError` (a `ToolError`).
  - `tag_image()`: puts the capture id and time into the `_meta` of the image block.
  - Tool `capture_status(capture_id)`.
- `server.py` (small, separate edits): `Services.captures` (attached to the scene in `__post_init__`), `SnapshotInfo.capture_id`/`captured_at`, a "capture id" region at the end of the `phone_snapshot` tool, the capture in `multimeter_read` (recorded right after the frame capture, before the model call), and `register_evidence_tools`. I re-read the file before each edit: dd-ui changed `SnapshotInfo` and the snapshot pipeline (P0) in parallel, and my edits sit next to theirs.
- `multimeter.py`: `MeterResult.capture_id`/`captured_at`. `multimeter_read` keeps each checked result by its capture id (`CaptureLog.meter_results`) for round 4.
- Evidence rule 8: position statements name a current `phone_snapshot` capture id; after a scene change an older id is invalid for new position claims.
- Tests `mcp/tests/test_evidence.py`: UUID v7 and UTC time; different ids for two calls; a meter result and its returned image have the same capture id (image `_meta`); a photo id is valid, then invalid after a scene change; meter images and unknown ids are not valid; the log is bounded.

### Checks

- `uv run pytest`: 379 passed, 1 skipped. Ruff on my files: pass.

## Bench workflow P1-P2, round 3: part identity

Source: "P1: Check physical part identity".

### What I did

- New module `mcp/debug_devices_mcp/board/identity.py`:
  - `IdentityState`: `visible_marking` (the photo shows a marking; boardview names are only candidates), `candidate` (boardview estimates), `confirmed`.
  - `identify()`: the pointed pixel goes back to board mm (`homography.unmap_point`, the inverse of the fit). The parts at that point are the parts whose box (plus 1 mm) contains it, else the parts within 3 mm.
    - With a marking: only an exact marking match can confirm. It also needs a registration that is checked (5 or more pairs), the part on the registered side, and the marking within 6 mm of the part. Otherwise the claim is `visible_marking`, with the reason and a request.
    - Without a marking (landmark): confirmed only when one part is at the point, no look-alike part is within 15 mm (same refdes letters, same pin count, box size within 1.3x), and the registration is checked. Otherwise `candidate`, with the pointed part and its look-alike parts, and a request for a closer photo.
    - A part on the other side is never confirmed. The request says to isolate the power safely before the board is turned.
  - `IdentityRegistry`: the claims of the session (bounded to 100). `report()` evaluates each confirmation again: when its photo id is no longer current (scene change) or its registration is stale, it shows as `candidate`, with the old refdes first in the candidates.
- `board/tools.py`: new tools `board_identify(photo_id, marking, registration_id, x_px, y_px)` (it refuses a stale photo id through the capture log and a stale registration) and `board_identity()`. `BoardSession.identities`, `BoardSession.photos` (the capture log, set in `Services.connect_board`), and `BoardSession.use_board()` (another board clears the claims). `board_locate_in_photo` returns `identity: "candidate"` and a note that positions are boardview estimates. `board_match_marking` returns `identity: "visible_marking"`.
- `board/homography.py`: `unmap_point()`.
- `server.py`: one line in `connect_board` (`self.board.photos = self.captures`).
- Evidence rule 9: say which identity state a part name has; only confirmed is a physical fact.
- Fixture `mcp/tests/fixtures/boardview/identity.json` (synthetic, invented names; README updated).
- Tests `mcp/tests/test_board_identity.py`: four similar coils give candidates, not one refdes; a clear marking plus a checked registration confirms one part; a scene change removes that confirmation and refuses the old photo id; an opposite-side target is not confirmed; a marking without registration stays `visible_marking`; a unique connector confirms by landmark; a 4-pair registration does not confirm; `board_locate_in_photo` and `board_match_marking` never confirm.

### Checks

- `uv run pytest`: 528 passed, 1 skipped (with dd-ui's new tests). Ruff on my files: pass.

## Bench workflow P1-P2, round 4: bench state

Source: "P2: Keep a compact bench state".

### What I did

- New module `mcp/debug_devices_mcp/bench_state.py`:
  - `BenchState`: power (`unknown`, `powered`, `isolated`; the user's confirmation and its time; the residual-voltage measurement), meter mode (from a confirmed reading, or confirmed by the user), probe contact (with a current photo id), confirmed measurements (source capture id, time, label, mode, value, unit, LCD text, overload, confidence state), part candidates (kept apart from the measurements), photo ids (last 20), steps (text, kind, done, evidence id), and the last probe short. `next_step` is the first uncompleted step.
  - `BenchStateStore`: reads the file on each call (another MCP server can write it) and writes it atomically (temporary file, then rename). Without a path it keeps the record in memory: `Services` built directly (tests) uses that.
  - Safety gate (`gate()`): a resistance, continuity, or diode step needs the power `isolated`, the user's confirmation, and a residual voltage of at most `SAFE_RESIDUAL_VOLTS` (0.5 V) from a confirmed voltage reading taken after the confirmation. `mV` is converted.
  - Tools: `bench_state()`; `bench_state_update(...)` (power, user confirmation, meter mode confirmed by the user, probe contact with `photo_id`, part candidates, new steps, completion of a visual step with a photo id; photo ids are checked with the capture log); `bench_record_measurement(capture_id, label, step_id)` (only a `confirmed` meter result, by its capture id; it completes the step when the mode fits the step kind, and it refuses a resistance, continuity, or diode step without the gate); `bench_begin_step(step_id)` (refuses the step and lists what is missing); `bench_probe_short()` (power back to `unknown`, confirmation and residual check cleared, a power check step inserted as the next step). A safe residual reading completes the open power check steps.
- `instructions.py`: `bench_instructions` returns `current_step` (the next uncompleted step) after the user's text. `DEFAULT_BENCH_STATE_FILE` (next to `instructions.md`). Evidence rule 10.
- Settings: `--bench-state-file` / `DEBUG_DEVICES_BENCH_STATE_FILE` (empty means the default). `.env.example` and `.gitignore` (`bench-state.json`) updated. The test settings point the file into `tmp_path`.
- `server.py` (small, separate edits): `Services.bench`, the store from the settings in `from_settings`, `register_bench_state_tools`, and the current-step callback for `register_instructions_tool`.
- `pyproject.toml`: `bench_state.py` gets the same per-file ruff rule as `board/tools.py` (the arguments of an MCP tool function are the tool API).
- The record holds only what the agent passes (for example a few part names). It copies no board file data.
- Docs: `mcp/README.md` (the four features under "Evidence rules"), `README.md` (tool table).
- Tests `mcp/tests/test_bench_state.py` (13): low-confidence and disputed results cannot enter; the full gate for a resistance step (refused before isolation, refused without the user's confirmation, refused without a residual check, then allowed; the measurement completes the step, and the completed step is no longer next); an unsafe residual blocks; a residual taken before the confirmation does not count; a probe short goes back to the power check and `bench_instructions` shows it; a safe residual completes the power check; a probe contact needs a current photo (refused after a scene change); measurement steps complete only with a measurement, visual steps with a photo; the file store (round trip, no temporary file left, a bad file gives a clear error); the gate lists all missing items; `.gitignore` has the file; the setting.

### Checks

- `uv run pytest`: 541 passed, 1 skipped. No `bench-state.json` was made in the repo.
- `nix develop --command prek run --files <my files>`: all hooks pass (after one `typos` fix in this report).
- I did not edit the user's `instructions.md`.

### Open items (all four rounds)

- The report's "Local continuity update" (mark the completed measurement in the user's `instructions.md`) is the user's text: I did not change it. The bench record now carries the steps, and `bench_instructions` shows its current step.
- The monitor page shows `display_text unit` of a meter result without its `status` (dd-ui's page).
- I ran ruff format and `ruff check --fix` on the shared `server.py`, `config.py`, and `constants.py` (they were in my file list). I did not check line by line if this changed any line of dd-ui in those files; dd-ui can check with `git diff`.

## Bench feedback 1, item 5: board_parts_at_photo

Brief: `docs/briefs/bench-feedback-1.md`, "dd-mcp part".

### What I did

- New tool `board_parts_at_photo(registration_id, x_px, y_px, radius_px=20)` in `board/tools.py` (with the identity tools): the inverse of `board_locate_in_photo`. The pixel goes back to board mm (`unmap_point`), and the radius in pixels becomes mm at that point. It returns the parts of the registered side whose box is within the radius, nearest first: refdes, side, center, distance to the box (0 inside), distance to the center, and `inside_box`. The result has `identity: "candidate"` and `evidence: "supporting evidence: boardview positions mapped through the photo registration, not a visual fact ..."`. At most 10 parts.
- It uses the scene guard and the strict photo check of item 6.
- Tests `mcp/tests/test_board_parts_at.py`: the pixel of a coil gives that coil (inside its box, 20 px = 2 mm); a larger radius adds its two neighbours at 5.08 mm, in distance order; a bottom part is not listed for a top registration.

### Open item

- With live tracking, the tool still needs the same scene and view as the registered photo. The tracker maps the live frames, not an older snapshot, so it cannot map a snapshot pixel after a move.

## Bench feedback 1, item 6: registration reuse for a newer phone_snapshot

### Problem found

`Pointing._board_to_snapshot` only scales the registration to the size of the last snapshot. A zoom, a flip, or a turn is our own camera command, so the scene watcher takes a new reference and the registration stays valid while its pixel mapping is wrong.

### What I did

- `evidence.py`: `CameraView` (zoom ratio to 3 decimals, in-sensor zoom mode, lens focal length, image turn, flips) from the camera status and the transform that `Services.phone_snapshot` already reads. Each `phone_snapshot` capture records its view. `CaptureLog.reuse_problem(registered_photo_id)`: none when the last photo is the registered photo, or when both photos are of the current scene epoch with the same view; otherwise the reason (for example "the camera view changed after the registered photo (zoom_ratio): register your last phone_snapshot again").
- `board/tools.py`: `Registration.photo_id` (the last `phone_snapshot` at registration time); `BoardSession.current_photo_id` and `BoardSession.snapshot_guard` (set by the server).
- `server.py` (small edits): `Services.last_view` (set in `Services.phone_snapshot`, one line after the status call), the view in the capture record, `guard_snapshot_registration()`, and a call to it from `guard_registration()` when the live tracker does not follow the registration. A stale registration keeps its own message.
- Result: `board_locate_in_photo` (also with `highlight`), `board_identify`, and `board_parts_at_photo` accept a newer `phone_snapshot` of the same scene and view, and refuse it after a zoom, flip, turn, sensor mode, or lens change with a clear message. `board_identify` and `board_parts_at_photo` use the strict check also with live tracking.
- Tests (`test_board_parts_at.py`): a newer snapshot with the same view works for `board_parts_at_photo` and `board_locate_in_photo`; after `phone_zoom` 2.0 and a new snapshot, `board_parts_at_photo`, `board_locate_in_photo`, and `board_identify` refuse with "camera view changed (zoom_ratio)"; the rules of `reuse_problem` (same photo, same view, flip change, scene change, unknown photo).

### For dd-ui

- `phone_point_to` goes through `Pointing._registration`, which calls only `scene.guard()`. Without the tracker it can map a registration to a snapshot with another zoom. Calling `Services.guard_snapshot_registration(registration_id)` there (when the tracker does not follow) closes that gap.

### Checks

- `uv run pytest`: 545 passed, 1 skipped. Ruff on my files: pass.

## Bench feedback 1, item 7: dim LCD (webcam controls and hint)

### What I did

- New module `mcp/debug_devices_mcp/webcam_controls.py`:
  - `V4l2Controls` reads the controls with `v4l2-ctl -d <webcam> --list-ctrls` (parsed into name, type, value, range, default, flags) and sets them with `--set-ctrl name=value,...`. It checks each value against the range of the device first.
  - `WebcamControls`: brightness, contrast, gain, `auto_exposure` (true: automatic, V4L2 menu 3; false: manual, 1), and `exposure` (`exposure_time_absolute`). A fixed exposure turns the automatic exposure off first, in the same call.
  - `WebcamControlStore`: `webcam-controls.json` in the state directory (not the repo, and not `ui-settings.json`). `ensure_applied()` sends the saved values one time per server process before the first webcam use; a failure only logs.
  - Tool `webcam_controls(brightness, contrast, gain, auto_exposure, exposure)`: no argument reads (current values and ranges); arguments set, check, and save.
- `multimeter.py`: `dim_hint()`. When the result is not confirmed and the model's notes say that the LCD is dim or faint, `request` starts with "The LCD is dim: turn on the meter backlight (the <meter model> backlight key), or raise the webcam exposure or brightness (webcam_controls), then call multimeter_read again." `check_reading()` takes the meter model (from `--meter-model`, for example "PROSTER T21D").
- `server.py`: `Services.webcam_controls` (memory store by default; the state directory file in `from_settings`), `ensure_applied()` before `webcam_snapshot` and before `multimeter_read` with the webcam, the meter model in the check, and the tool registration. `config.py`: `--v4l2-ctl-path` (`.env.example` updated).
- A UI setting for these controls is for dd-ui later: the page can call the `webcam_controls` tool.
- Tests `mcp/tests/test_webcam_controls.py` (8, fake `v4l2-ctl` only): parsing, the order of the exposure settings, set + range check + save, apply one time per process, no command without saved values, the tool (read, set, refuse out of range), the dim hint with and without a meter model, and no hint for a confirmed or a blurred (not dim) reading.

### Checks

- `uv run pytest`: 561 passed, 1 skipped. `prek run --files <my files>`: pass.
- Hardware: no test on the real webcam (the orchestrator stopped hardware use for the live bench session). Before that message I ran `v4l2-ctl -d /dev/video0 --list-ctrls` one time (read only) to see the controls of this webcam: brightness -64..64, contrast 0..64, gain 0..100, `auto_exposure` menu (3 = aperture priority), `exposure_time_absolute` 1..5000.

## Bench feedback 1, addition: board session over a server restart

Problem from the bench session: after a server restart, `board_register_photo` said "no board is open".

### What I did

- New module `mcp/debug_devices_mcp/board/session_store.py`: `BoardSessionRecord` (board path, board SHA-256, the registrations as JSON with their photo capture ids, the last registration id) and `BoardSessionStore` (a JSON file, written atomically; without a path it stays in memory). The file is `board-session.json` in the state directory (`$XDG_STATE_HOME/debug-devices` or `~/.local/state/debug-devices`), not in the repo and not `ui-settings.json`.
- `board/tools.py`:
  - `BoardSession.with_store()`, `save()`, and `restore()`. `board_open` records the absolute path that it got (the path in the obv-dump output can differ).
  - `restore()` runs one time, at the first board tool call after a start: it opens the recorded board again with `obv-dump`. The restored registrations are stale: a new process has no capture ids and no scene reference for them, so it cannot check them. A changed board file (another SHA-256) drops the registrations; a missing file leaves no board open. `restore_note` says what happened.
  - `board_tool(server, session)` replaces `@server.tool()` for the 12 board tools: restore before the tool, save after it (also after an error). `functools.wraps` keeps the signature and the description for the MCP schema.
- `server.py`: `from_settings` gives the board session the file in the state directory (one line). Tests never call `from_settings`, so they never write the real state directory.
- Tests `mcp/tests/test_board_restart.py` (4, `tmp_path` only): after a restart, `board_find_part` works without `board_open` (one obv-dump run) and the old registration is refused as stale; a changed board hash drops the registrations; a missing board file leaves "no board is open" and a note; without a record nothing runs and no file is written.

### Checks

- `uv run pytest`: 565 passed, 1 skipped. `prek run --files <my files>`: pass.
- No real phone, webcam, or running server was used (live bench session).

### Open item

- `restore_note` is only in the log and on the session object. A tool result (for example `board_open` or the stale-registration message) can show it; that is a small follow-up.

## Bench feedback 2, items 1 and 2: multi-frame meter check and the user-confirmed mode

Brief: `docs/briefs/bench-feedback-2.md`, "dd-mcp part". Seen on the bench: "51.0 V" (the LCD showed 5.10 V) and "93.2 V" at a USB-C point marked confirmed; "diode" read twice while the dial was on DC V.

### What I did

- New module `mcp/debug_devices_mcp/meter_frames.py`: `signature()` (the digits and the decimal point position of the LCD text), `base_value()` (the value in V or A with the unit prefix applied), and `combine()`. The combined status is the worst frame status; then:
  - a moved decimal point between frames ("5.10" and "51.0") gives `disputed`;
  - a value above `--max-voltage` (default 30 V) or `--max-current` (default 10 A) gives `disputed`;
  - digits that change with the same decimal point give `uncertain`, with `value_min`/`value_max`;
  - `frames` (each frame: capture id, time, LCD text, unit, mode, value, confidence, status) and `stable` are in the result.
- `server.py`: `read_meter()` (used by `multimeter_read`, and by `bench_measure` in item 3). It captures `frames` images (default 2, at most 3) `--meter-frame-interval` apart (default 1 s) and starts each model call at once, so the calls run in parallel. Every image has its own capture id; the result has the id of the first image, and every frame id finds the result (`CaptureLog.meter_results`). `include_image` returns every frame image, each with its capture id in `_meta`.
- Item 2: `check_reading(..., user_mode)`: a mode that the user confirmed on the dial replaces the model's mode for the unit check; the result keeps `model_mode` and has `mode_source: "user"`. `bench_state.recent_user_mode()` gives that mode when the user confirmed it in the last 10 minutes (`USER_MODE_MAX_AGE`). With the user's mode, a confirmation needs 2 frames that agree; a unit of another family stays `disputed`. The server cannot see a dial change; the time limit and the unit family check cover it.
- Settings `--max-voltage`, `--max-current`, `--meter-frame-interval` (`.env.example` updated; the test settings use an interval of 0). Evidence rule 2 mentions the frames and the user's mode.
- Tests `mcp/tests/test_meter_frames.py` (15): the signature; equal frames confirmed; a moved decimal point disputed; the voltage and current limits with prefixes (93.2 V and 12 A disputed, 900 mV and 250 mA confirmed); changing digits uncertain with the range; the worst status wins; the tool returns every frame image with matching ids; the tool disputes "5.10" + "51.0"; a recent user mode confirms "diode" + V as DC V with 2 frames and not with 1; a conflicting unit stays disputed; a user mode older than 10 minutes is not used.
- Changed tests: `test_server.py` (2 webcam captures by default) and dd-ui's `test_ui_app.py` (the log has 2 images of the model input: two lines).

### Checks

- `uv run pytest`: 616 passed, 1 skipped. Fakes only (live bench session).

### Cost

- Two frames mean two model calls per `multimeter_read` (in parallel, so the time is about one call plus 1 s). `frames: 1` keeps the old cost, but a value can then only be as good as one frame.

## Bench feedback 2, item 3: bench_measure

### What I did

- `server.py`:
  - The body of the `phone_snapshot` tool is now the function `take_phone_snapshot(services, save_path, max_side)`, unchanged (including dd-ui's `carry` of a stale registration). The tool calls it.
  - New tool `bench_measure(expected_mode, frames=2, save_path, max_side, include_meter_images=False)`, in its own region `register_bench_measure_tools`. It starts `read_meter()` (webcam) as a task and takes the phone snapshot at the same moment. It returns a `BenchMeasurement`: `measurement_id` (UUID v7), `meter` (the checked multi-frame result, with its capture id), `photo` (the `SnapshotInfo` with its capture id), `gap_seconds` (first meter frame to the photo), and a note. Content: the JSON, the photo, and (with `include_meter_images`) the meter frames, each image with its capture id in `_meta`. If the photo fails, the meter task is cancelled.
- Evidence rule 8: use `bench_measure` when a measurement needs the photo of the probe contact.
- Tests `mcp/tests/test_bench_measure.py` (3): the value and the photo have different capture ids, the photo is valid for a position claim, the images come in order (photo, then the meter frames) with their ids, the meter result is in the capture log for `bench_record_measurement`; without meter images only the photo; a phone failure gives a tool error.

### Checks

- `uv run pytest`: all pass (fakes only).

### For dd-ui

- `phone_snapshot` now calls `take_phone_snapshot()`: a change to the snapshot pipeline goes there.

## Bench feedback 2, item 8: side labels of boardview files

### What I did

- `board/model.py`:
  - Through-hole parts and mounting holes (names `MH*`, `H<n>`, `HOLE*`, `SCREW*`, `STANDOFF*`, `SPACER*`) get the side `both`, and their pins too. `Part.labeled_side` keeps the label of the file when it differs.
  - `check_sides()`: for each part, the nearest part of the same type (same refdes letters, same pin count) within 3 mm; the file is "mixed" when at least 10 such pairs, and at least 30 % of them, have opposite labels. It uses a 3 mm grid (0.28 s for the target board, index included).
  - `SideLabels` and `Board.apply_side_labels()`: "auto" (the check), "mixed" (the user says that the labels are not reliable), "trust" (no warning). `Board.side_ok()` does not rule a part out by its label in a mixed file.
- `board/tools.py`: `board_open(path, side_labels="auto")`; `BoardSummary.relabeled_both_sides` and `side_warning`. In a mixed file, `board_locate_in_photo` keeps the pins of both labels and adds the warning to its notes (`on_registered_side` still says what the label says), and `board_parts_at_photo` lists parts of both labels with `side_warning`. The session record keeps the choice over a restart.
- `board/identity.py`: in a mixed file, an other-side label no longer refuses a confirmation: the other checks decide, and the reason says that the labels are mixed. In a file with reliable labels, the refusal stays.
- Fixture `mcp/tests/fixtures/boardview/sides.json` (synthetic, invented names; README updated).
- Tests `mcp/tests/test_board_sides.py` (5): both sides for mounting holes and a through-hole connector; a mixed file is detected and the clean fixture is not; the tools warn instead of ruling out (a bottom-labelled capacitor listed for a top registration; a bottom-labelled marked IC confirmed with the note); the user's choice overrides the check; "mixed" keeps over a restart.

### Result on the target board (local only, counts)

- 64 parts are now on both sides (through-hole parts and mounting holes).
- The check says "not mixed": 263 of 2216 same-type neighbour pairs (12 %) have opposite labels. Per 20 mm area, the highest share is 42 % (8 of 19), and 6 areas have 25 % or more. The distribution is smooth, so the check cannot tell mislabelled parts from normal neighbours on the two sides of a double-sided board, and I cannot look at the board to check. For this file, open it with `board_open(path, side_labels="mixed")`, as the user reported mixed labels.

### For dd-ui

- The pointing (`pointer.plan`, `OTHER_SIDE_MESSAGE`) still rules parts out by `on_side`. `Board.side_ok()` gives the same answer as `on_side` unless the file is mixed.

## Bench feedback 2, item 9: bench record

### What I did

- `bench_state.py`:
  - `Measurement.power_state` and `power_confirmed_by_user`: the power state of the record and the user's confirmation when the measurement entered it.
  - `bench_record_measurement(..., done_before_gate)`: the user's reason when a resistance, continuity, or diode test was already done before the gate was complete (the bench case: the test with the power off, before the residual-voltage check). The measurement then completes its step and keeps the reason. Without the reason the refusal stays, and it names the parameter. `bench_begin_step` stays strict for new steps.
  - `change_step()`: `bench_state_update(skip_step=..., step_reason=...)` skips a step; `complete_step` with `step_reason` and no `photo_id` completes a step from the user's report. `Step.completed_by` (`evidence`, `user_report`, `skipped`) and `Step.reason`. The next step then moves on.
  - `add_photo()`: every `phone_snapshot` (also the one of `bench_measure`) adds its capture id to `photo_ids` (up to 1000; no duplicates). `take_phone_snapshot()` in `server.py` calls it.
- Tests (`test_bench_state.py`, 4 new): the power state on a measurement; a test done before the gate attaches with the reason (and not without it); skip and complete with a reason move the next step; every photo id is in the record, in order.

### Checks (items 1, 2, 3, 8, 9)

- `uv run pytest`: 681 passed, 1 skipped. `nix develop --command prek run --files <my files>`: pass.
- Fakes only: no real phone, no real webcam, no restart of a running server (live bench session). One local `obv-dump` run on the target board for the side-label counts (item 8), with counts only in the output.

## Meter decimal point, round 1: digits and decimal point as separate fields

Brief: `docs/briefs/meter-decimal.md` (dd-mcp part). Bench case: the Proster T21D showed 1.415 V; three frames read "14.15", so the frame check could not find the wrong point. Rule of the brief: a new check can only lower a status.

### What I did

- `multimeter.py`, schema: two new fields before `value`: `digits` (the digit characters left to right, no sign, no point) and `digits_before_point` (null when there is no point). The strict schema sends them as required and nullable; the Python default `None` is only for local callers and old test data.
- Prompt: "Find the decimal point first: a small dot at the bottom of the LCD between two digits. Say between which digits it is ..." and `display_text` and `value` must agree with the two fields. The `value` description says: the digits with the point where the LCD shows it.
- Check `consistency_problems()`: the signature of `display_text` (digits and point position; `signature()` moved from `meter_frames.py` to `multimeter.py`, and `meter_frames` imports it) must equal the two fields, and `value` must equal the number that the LCD text makes (with its sign, in the unit as shown). A mismatch adds "the digits, the decimal point, and the value of the model do not agree (...)" and lowers "confirmed" to "uncertain". Not checked: an unreadable display, OL, no digits, and missing fields (only the value is compared then).
- `lower(status, new_problems, problems)`: the one helper for these checks; it can only lower "confirmed" to "uncertain".
- `MeterResult.digits` and `digits_before_point`: the model's fields as evidence.
- Tests `mcp/tests/test_meter_decimal.py` (round 1 part): the schema fields; agreeing answers stay confirmed ("1.415", "-0.123", "443.0", "512"); "1.415" with value 14.15 is uncertain; "14.15" with the point field 1 is uncertain; OL and old data are not checked; a disputed result stays disputed.

### Checks

- `uv run pytest`: 683 passed before; all pass after (fakes only). The strict-mode schema test passes.

## Meter decimal point, round 2: meter display profile (meter_counts)

### What I did

- Setting `meter_counts` (`--meter-counts`, `DEBUG_DEVICES_METER_COUNTS`, default 0 = unknown, no check). `.env.example` and `mcp/README.md` updated. The test settings remove the variable.
- `multimeter.py`, `display_problems(reading, counts)` (called in `check_reading(..., counts)`; it can only lower "confirmed" to "uncertain"):
  - a. the digit count must be the digit count of the display (6000 counts: 4 digits). "51.0" and "93.2": "the meter shows 4 digits; the model read 3 (...): a digit or the decimal point is probably wrong".
  - b. the digits as one integer must be below the count (6000: at most 5999).
  - c. auto range (flag AUTO or range "auto"): a leading zero with the point after the second digit or later, or with no point ("05.10", "0510"), is not possible. "0.123" is possible; in a manual range the leading zero is not checked.
  - Not checked: unreadable, OL, no digits.
- The prompt names the digit count when it is known: "Its display has 6000 counts: it shows 4 digits." (`VisionClient.meter_counts`, set in `Services.from_settings`). This helps the model; it is not a check.
- `server.py`: `read_meter()` passes `settings.meter_counts` to `check_reading()` (one argument), and `from_settings` sets `vision.meter_counts` (one line). The orchestrator's markings fix is untouched.
- Tests (round 2 part of `test_meter_decimal.py`): "51.0" and "93.2" with 6000 counts are uncertain; "6.123" is above the counts; "05.10" in auto range (flag or range) is uncertain, "0.123" in auto range and "05.10" in a manual range stay confirmed; "1.415", "5.100", "443.0", "5999" stay confirmed; unknown counts check nothing; the prompt names 4 digits.

### Checks

- `uv run pytest`: all pass (fakes only).

## Meter decimal point, round 3: expected value as context

### What I did

- `meter_frames.py`, region "expected value": `expected_problems(result, expected, counts)`. It takes the number that the LCD text shows (digits, point, sign: the model's `value` is only kept for a confirmed result) and converts it to the base unit of its family (V, A, Ω; `multimeter.unit_parts()` gives the prefix). When it is outside [expected / `EXPECTED_BAND`, expected x `EXPECTED_BAND`] (`EXPECTED_BAND = 3.0`; no check for 0 or no value, OL, or other families):
  - the problem "14.15 V is 4.3 × the expected 3.3 V: check the decimal point; ask the user to read the LCD";
  - the same digits with the point in the other positions: the nearest value inside the band is named, for example "1.415 V, with the point one place to the left, is near the expected 3.3 V". With `meter_counts` known and auto range, a position with an impossible leading zero is not named.
- `combine(..., expected_value=..., counts=...)` (keyword-only) runs it on the first frame and lowers "confirmed" to "uncertain" with `lower()`. The request then says: "Check the decimal point: ask the user to read the LCD, or read the meter again with the LCD larger in the frame." `MeterResult.expected_value` keeps the context.
- `server.py`: `expected_value` on `multimeter_read` and `bench_measure` (with the tool docs: base unit, context only, the band, the point shift, and the `meter_counts` checks), passed through `read_meter()` to `combine()`. `pyproject.toml`: `server.py` gets the same per-file ruff rule as `board/tools.py` (the arguments of a tool function are the tool API).
- `instructions.py`, evidence rule 2: pass `expected_value` when the test has a nominal value; it is context, never proof; a value far from it is more likely a misplaced decimal point: ask the user to read the LCD.
- Tests (round 3 part of `test_meter_decimal.py`): "14.15" x 3 with expected 3.3 (the exact brief texts); a value below the band names a shift to the right; a value in the band stays confirmed; expected 0 checks nothing; mV and kΩ converted; an impossible shift is not named; the expected value never raises a status.

### Checks

- `uv run pytest`: all pass (fakes only).

## Meter decimal point, round 4: tests of the brief's cases

### What I did

Tests through the tools (`multimeter_read`, `bench_measure`) with fake vision answers, in the round 4 part of `mcp/tests/test_meter_decimal.py`:

- "14.15" (digits "1415", 2 before the point) in 3 frames with `expected_value` 3.3: "uncertain", no value, and a problem names 1.415 V.
- "51.0" with `meter_counts` 6000: the problem "the meter shows 4 digits; the model read 3". The status is "disputed", not only "uncertain", because 51.0 V is also above the 30 V bench limit. The same count check alone gives "uncertain": "5.10" (3 digits, below the limit) is "confirmed" without a count and "uncertain" with 6000 counts.
- A self-contradicting answer (display "1.415", value 14.15): "uncertain" with "the digits, the decimal point, and the value of the model do not agree".
- "1.415" in 3 frames with `expected_value` 3.3 and 6000 counts: "confirmed", value 1.415, with the model's digits and point in the result.
- `bench_measure(expected_value=3.3)` with a "14.15" answer: the meter part is "uncertain" and keeps the expected value.
- The strict-mode schema test (`test_sent_schema_is_strict`) passes with the two new fields.

### Checks (rounds 1-4)

- `uv run pytest`: 710 passed, 1 skipped (683 before). `nix develop --command prek run --files <my files>`: all pass.
- Bench rule kept: fakes only, no webcam or phone, no change to `.env` or the settings file, no restart, no change to `ui/static/`. The orchestrator's markings fix in `server.py` is untouched (`set_markings`, `_set_old_app_markings`, `app_hides_markings`).
- The running MCP server does not load these files until its next start.

### Open items

- The fields and the checks can only lower a result. If the model puts the point in the wrong place in every frame, and there is no expected value and no count, a wrong value can still be "confirmed". Set `DEBUG_DEVICES_METER_COUNTS=6000` for the Proster T21D after "bench done" (the `.env` change is the user's), and pass `expected_value` for rails with a nominal value.
- The brief's "not now" items (two-pass LCD zoom, a live check with the real meter) wait for the user's decision after "bench done".
- For dd-ui (no change now, `ui/static/` is frozen): the page shows `display_text unit`; it can also show `status` and, for a result far from the expected value, the named point shift.

## Meter decimal point, round 5: the display profile only for volts and amperes

### What I did

The display profile (`--meter-counts`) gave false "uncertain" results for real readings. Many meters blank the leading digit in the lowest range of a function: a 6000-count meter shows " 12.3" mV (600.0 mV range), " 51.0" Ω (600.0 Ω range), and "50.0" % duty with 3 digits.

- `multimeter.py`: the new constant `PROFILE_FAMILIES` (voltage, current) and the helper `uses_display_profile(unit, counts)`. `display_problems` runs its checks (digit count, counts limit, leading zero) only for these two families. This is the same scope as the bench limit. Frequency and capacitance often have other counts (for example 9999).
- The "fewer digits" rule is only for the base unit without a prefix (V, A): "51.0" V and "93.2" V stay "uncertain". A reading with a prefix (mV, uA, mA) can be the lowest range with a blank leading digit. "More digits", the counts limit, and the leading zero in auto range stay for all prefixes of V and A.
- `meter_frames.py`: the point-shift suggestion for `expected_value` uses the same scope for its leading-zero filter.
- The prompt line for the counts changed: "For volts and amperes, its display has 6000 counts: at most 4 digits. The lowest range can show a blank for the leading digit: do not add a zero for it." The old line ("it shows 4 digits") told the model a rule that is now false. It could also make the model add a "0" for a blank digit ("012.3" mV). This is a prompt change. The orchestrator approved it.
- Docs: `mcp/README.md` (the `--meter-counts` row and the "Decimal point" bullet), the `multimeter_read` docstring in `server.py`, the `meter_counts` description in `config.py`, and the comment in `.env.example`. In `server.py` I changed only the docstring; the markings fix is not changed.

### Tests (round 5 part of `mcp/tests/test_meter_decimal.py`)

- Not lowered with 6000 counts: "12.3" mV, "51.0" Ω, "50.0" %, "9.999" kHz, "12.3" uA, "12.3" mA.
- Still "uncertain": "51.0" and "93.2" in V and in A (the problem "the meter shows 4 digits; the model read 3").
- They stay for prefixes: "123.45" mV (more digits), "612.3" mV (above the counts), "012.3" mV in auto range (leading zero).
- Other functions have no limit: "9999" Hz, "06.80" uF in auto range, and "0.4430" MΩ are "confirmed".
- Through `multimeter_read` with a fake vision answer: "12.3" mV with 6000 counts is "confirmed", value 12.3.
- The prompt test has the new text.

### Checks

- `uv run pytest`: 721 passed, 1 skipped. `uv run ruff check` and `ruff format --check` on my files: pass. `nix develop --command prek run --files <my files> mcp/README.md .env.example`: all pass.
- Bench rule kept: fakes only, no webcam or phone, no change to `.env` or the settings file, no restart, no change to `ui/static/`. Nothing committed.

## Meter decimal point: check against the bench misreads (priority 4)

### What I did

I checked rounds 1-5 against the four bench misreads. Each case goes through `multimeter_read` with a recorded model answer. The new tests are in the "bench misreads" part of `mcp/tests/test_meter_decimal.py`. The real `.env` has `DEBUG_DEVICES_METER_COUNTS=6000`, so the "6000 counts" results apply to the live server.

| Case (model answer) | Result with 6000 counts | Other context | Tests | Open |
| --- | --- | --- | --- | --- |
| "51.0 V", the LCD showed 5.10 V | uncertain, no value: "the meter shows 4 digits; the model read 3" | 0 counts: disputed (above the 30 V bench limit). `expected_value` 5: also "5.1 V, with the point one place to the left" | `test_bench_51_0_volts_is_not_confirmed` (new), `test_51_0_with_6000_counts_is_uncertain`, `test_tool_disputes_a_misread_decimal_point` | A, B |
| "93.2 V" at a USB-C point (about 5.1 V) | uncertain, no value: the digit problem; with `expected_value` 5: "19 × the expected 5 V" | 0 counts: disputed (bench limit) | `test_bench_93_2_volts_at_usb_c_is_not_confirmed` (new), `test_plausibility_limits` | A, B, C |
| About 443 kΩ read as "443.0 V" | disputed, no value (bench limit; 4 digits, so the profile accepts it) | `expected_mode` resistance: disputed ("expects resistance"). User-confirmed resistance: disputed | `test_bench_443_kohm_read_as_volts_is_disputed` (new), `test_443_volts_during_a_resistance_test_is_disputed` | none for 443 V; see E |
| DC V dial read as "diode" ("5.10" V) | uncertain (3 digits) | User-confirmed DC V, 2 frames or more: confirmed as `dc_voltage` (`model_mode` diode). `expected_mode` dc_voltage: uncertain. No mode context and no digit problem: confirmed as diode | `test_bench_dc_volts_read_as_diode_is_uncertain_in_a_dc_voltage_test` (new), `test_recent_user_mode_confirms_when_two_frames_agree`, `test_user_mode_in_the_single_frame_check` | D |

No case gives a value. The only exception is the diode case without mode context: the value (5.1 V) is correct, but the mode is wrong.

### Open items

- A. The bench limit uses the frame value, which exists only for a confirmed frame. When a different check lowers a frame (the digit count, low confidence), the limit check does not run. So with 6000 counts, "93.2 V" is "uncertain", not "disputed", and the "above the bench limit" problem is not shown. Proposal: calculate the limit from the LCD digits, as `expected_problems` does (a small change in `meter_frames.limit_problem`).
- B. The bench-limit problem repeats once for each frame (3 identical lines for 3 frames). Proposal: remove the duplicates.
- C. The point-shift suggestion is only a candidate. For "93.2" with an expected 5 V, it names 9.32 V, but the real value was about 5.1 V: a digit was also misread. The request still says "ask the user to read the LCD".
- D. Without a user-confirmed mode or an `expected_mode`, a diode reading of 5.1 V is "confirmed" as diode. Proposal: a diode reading above the diode test voltage (for example 3 V) is "uncertain": the dial is probably on DC V.
- E. Known from round 4: a point shift that keeps 4 digits and stays below the limit (for example "14.15" for 1.415 V) is "confirmed" without `expected_value`.

### Live check

Not done. The user's MCP server (`--ui-start eager`) holds `/dev/video0` through its ffmpeg. A test server on UI port 18799 (`--no-ui-open-browser`, adb `/bin/false`, state and runtime folders in my scratch directory) got "Device or resource busy" from `webcam_snapshot`. The test server did not get a frame and did not send an OpenRouter request. It stopped at the end of the call.

### Checks

- `uv run pytest`: 746 passed, 1 skipped. ruff and prek on `mcp/tests/test_meter_decimal.py`: pass. I made no change to the code. Nothing committed.

## QA round 4 fixes, round 1: bench limit for all frames (open items A and B, QA B-F4)

### What I did

- A: `meter_frames.base_value` now takes the number from the LCD text (digits, point, sign) and the unit prefix. It does not use `value`, because a frame has a value only when it is confirmed. So the bench limit also applies to a frame that a different check lowered (the digit count, a low confidence). An overload or an LCD without digits gives no limit problem.
- B: `combine` shows each limit problem once. Two frames with different LCD texts above the limit keep both texts.

Result: "93.2 V" with 6000 counts, or at a low confidence, is now "disputed" with "93.2 V is above the bench limit of 30 V". Before the fix, it was "uncertain". A confirmed frame gives the same result as before, because its value must agree with the LCD text.

### Tests

- `test_bench_93_2_volts_at_usb_c_is_not_confirmed`: with 6000 counts and `expected_value` 5, "disputed", and the limit problem shows only once.
- `test_the_bench_limit_applies_to_a_frame_that_another_check_lowered`: "51.0 V" at confidence 0.3 (0 counts), and "51.0 V" with 6000 counts. Every frame is "uncertain" without a value, and the result is "disputed" with one limit problem.
- `test_the_bench_limit_uses_the_prefix_and_skips_an_overload`: "45.00 mV" is confirmed. "O.L" gives no limit problem.
- `test_two_frames_above_the_limit_keep_both_texts`: "51.0 V" and "93.2 V" give two lines.

### Checks

- `uv run pytest`: 758 passed, 1 skipped. ruff and prek on `meter_frames.py` and `test_meter_decimal.py`: pass. Nothing committed.

## QA round 4 fixes, round 2: the safety gate (B-E3, B-E4, B-E8)

### What I did (`mcp/debug_devices_mcp/bench_state.py`)

- B-E3: `PowerRecord.residual_points` keeps the latest confirmed voltage reading at each point after the isolation confirmation. The point is the measurement `label` (case and spaces do not count). A new reading replaces only the reading at the same point. So a safe reading at another point does not clear an unsafe one. The gate lists each unsafe point: "the residual voltage 5.10 V at 'C12 positive side' is not safe (limit 0.5 V): stop, let the board discharge, and measure again at the same point with the same label (a safe reading at another point does not clear it)". `residual` is now the highest unsafe point, or the latest safe reading when no point is unsafe. A new isolation confirmation (`bench_state_update` power isolated, user_confirmed_isolation true) clears the points, and the gate then needs a new residual reading. A record from before this change (only `residual`) still works.
- B-E4: a power-check step completes only when three conditions are true: the reading comes after the user's isolation confirmation, it is safe, and the gate is open (no other unsafe point). Otherwise the step stays open, and the tool result has a new `notice` field with the reason. The measurement enters the record in all cases, so an unsafe reading blocks the gate. I did not refuse it, because a refusal would lose the unsafe reading. The automatic completion of the open power checks has the same condition, and it now sets `completed_by` "evidence".
- B-E8: `residual_volts` uses `unit_parts` (µ and μ become u) and `meter_frames.PREFIX_FACTORS`, with the correct case. "450 µV" is 0.00045 V (safe). "0.4 kV" is 400 V and "0.4 MV" is 400000 V (not safe). A unit that is not volts, an unknown prefix, or an overload gives no number, so it is not safe.
- Docs: the `bench_record_measurement` description, rule 10 in `instructions.py` (a safe residual voltage at every measured point; use the same label to measure a point again), and the "Bench state" bullet in `mcp/README.md`.

### Tests (`mcp/tests/test_bench_state.py`, "QA round 4, the safety gate")

- `test_a_safe_reading_at_another_point_does_not_hide_an_unsafe_one`: 5.10 V at one point, then 0.01 V at "VBUS": the gate stays closed, and the power check stays open. 8.0 mV at the same point (another case and other spaces) opens the gate.
- `test_the_highest_unsafe_point_decides_and_a_new_confirmation_clears_the_points`: 5.1 V at A and 12 V at B give two unsafe lines, and `residual` is B. After B reads 0.00 V, `residual` is A. A new confirmation clears the points.
- `test_a_power_check_step_needs_a_safe_residual_after_the_confirmation`: a reading before the confirmation, and 5.10 V after it, leave the step open with a `notice`. The unsafe reading is in the record. 0.02 V completes the step.
- `test_residual_volts_use_the_unit_prefix` (9 cases: µV, μV, uV, mV, V, kV, MV, Ω, an unknown prefix) and `test_450_microvolts_opens_the_gate_and_0_4_kilovolts_blocks_it`.

### Checks

- The bench, meter, and instructions tests pass (111). ruff and prek on my files pass.
- The full `uv run pytest` run has failures in files of other agents' work in progress: `test_phone_stop.py`, `test_lazy.py`, and `test_discovery.py`. One more is in `test_board_identity.py`: the new photo-size check in `board/tools.py` (QA B-E5/B-F2, another agent) refuses the test's 64x48 photo against a 1200x1000 registration. I did not change these files. Nothing committed.

## QA round 4 fixes, round 3: a measurement keeps the user's meter mode (B-F3)

### What I did (`mcp/debug_devices_mcp/bench_state.py`)

- `record_measurement` does not replace a recent mode that the user confirmed on the dial (source "user", at most 10 minutes old). Before the fix, each measurement wrote the mode with the source "reading", and `recent_user_mode` then returned None. So the next `multimeter_read` lost the user's mode.
- A reading can still replace an expired user mode, because `multimeter_read` does not use it.
- The new helper `is_recent_user_mode` holds the age rule for `recent_user_mode` and `record_measurement`. The new constant `READING_SOURCE` replaces the literal "reading".

### Tests (`mcp/tests/test_bench_state.py`, "a measurement keeps the user's meter mode")

- `test_recording_a_measurement_keeps_the_users_meter_mode`: the user confirms DC V, and the model reads "diode". `multimeter_read` confirms the reading, and `bench_record_measurement` records it. The record keeps the source "user", and the next `multimeter_read` still has `mode_source` "user".
- `test_a_reading_replaces_an_expired_user_mode`: an 11-minute-old user mode is replaced by a resistance reading (source "reading").

### Checks

- `test_bench_state.py` and `test_meter_frames.py`: 47 passed. ruff on my files: pass. Nothing committed.

## QA round 4 fixes, round 4: the frames must agree on the unit, the mode, and the sign (B-E1, B-E2)

### What I did (`mcp/debug_devices_mcp/meter_frames.py`)

- The new function `disagreements` compares the frames on three items:
  - The unit: family and prefix, through `unit_parts`, so "µV" and "uV" are the same. An unreadable unit is not a different unit, because that frame is already "uncertain".
  - The checked mode: a mode that the user confirmed replaces the model's mode.
  - The sign: "−" (MINUS SIGN) and "-" are the same. A zero has no sign, because a meter can show "-0.00" and "0.00".
- A difference gives "disputed", no value, `stable` false, and the problem "the unit changed between the frames (V, mV): one of the frames is a misread" (the same for mode and sign). The request asks for a new read.
- Before the fix, "4.98 V" and "4.98 mV" gave a confirmed 4.98 V, and "-0.12" and "0.12" gave a confirmed -0.12 V.
- Docs: the `multimeter_read` description and the "Meter frames" bullet in `mcp/README.md`.

### Tests (`mcp/tests/test_meter_frames.py`, "the frames must agree")

- Disputed: V and mV, DC V and AC V, "-0.12" and "0.12". The unit problem names both units. Through the tool, V and mV are disputed.
- Still confirmed: "-0.00" and "0.00"; MINUS SIGN and hyphen-minus; MICRO SIGN and "u".
- A "V" frame and a frame with an unreadable unit stay "uncertain", with no unit problem.

## QA round 4 fixes, round 5: the orchestrator's decisions on open items C, D, and E

### What I did

- C: no change (the orchestrator's decision).
- D: the new setting `--max-diode-voltage` (`DEBUG_DEVICES_MAX_DIODE_VOLTAGE`, default 3.0 V; in `.env.example`, the config table, and the test fixture's variable list). The limit check uses the number on the LCD and the unit prefix. If a diode reading is above the setting, the result is "uncertain" with this problem: "5.10 V in diode mode is above the diode test voltage of 3 V: the dial is probably on DC V". The request tells the agent to ask the user for the dial mode and to record DC V. A DC V mode that the user confirmed replaces "diode", so the problem does not show then. The limit goes to `combine` through `MeterLimits.max_diode_volts` (`read_meter` in `server.py`).
- E: a known limit in the `multimeter_read` and `bench_measure` descriptions and in the "Decimal point" bullet of the README. A decimal point shift that keeps the digit count and stays below the bench limit (for example "14.15" for 1.415 V) can pass as "confirmed". For a value that decides a repair step, give `expected_value`, or ask the user to confirm the LCD.
- `server.py` edits (small, after a new read): the `MeterLimits` call in `read_meter` and the two tool descriptions.

### Tests (`mcp/tests/test_meter_frames.py`, "a diode reading above the diode test voltage")

- "5.10 V" in diode mode: "uncertain", with the note once and the dial request.
- Still confirmed: "0.512" V, "512" mV, "O.L", and "2.950" V.
- A limit of 6 V confirms "5.10 V". `--max-diode-voltage 2.5` is parsed.
- Through the tool: "uncertain" without a user mode; "confirmed" as `dc_voltage` after the user confirms DC V.

### Checks (rounds 1-5)

- `uv run pytest`: 824 passed, 1 skipped (all tests, also the files of the other agents). ruff and prek on my files: pass.
- The running MCP server loads these changes only after a restart. I did not restart it. Nothing committed.

## QA round 6 follow-up: N5, N6, and the rest of B-E2

### What I did

- N5 (`bench_state.py`): only DC voltage (`RESIDUAL_MODE`) is the residual-voltage check. Only a DC reading can complete a power-check step or open the gate.
  - An AC reading after the isolation confirmation is recorded, and the tool result has the `notice` "residual check needs DC V: this AC reading does not count as the residual-voltage check (a capacitor holds a DC charge that AC V does not show)". With a power-check `step_id`, the step stays open with the same note.
  - One addition for safety: an AC reading above 0.5 V still blocks the gate as an unsafe point (230 V AC at "VBUS" is not a board without power). A safe DC reading with the same label clears it.
  - `POWER_CHECK_TEXT` and the gate text now say "DC voltage mode".
- N6 (`bench_state.py`): the step is now checked after the reading enters the record. If the step is refused and the reading counts for the gate, `record_measurement` does these things:
  - It keeps the reading without the step.
  - It raises `ResidualKeptError`, a `ToolError`. The tool saves the record first, then refuses.
  - The refusal says: "The voltage reading is recorded without the step, because it counts for the safety gate: do not record it again."
  - A reading counts for the gate when it is a DC reading or an unsafe AC reading after the confirmation.
  - An unknown `step_id` is now a refusal of the same kind (before, `find_step` raised before any record).
  - Any other refused reading changes nothing, as before.
  - A second call with the same capture id does not add the measurement again. So a retry with the correct step completes the step with the recorded reading.
  - `add_measurement` and `complete_power_checks` are new helpers.
- B-E2 rest (`meter_frames.py`): `sign_key` uses the `signed_value` rule, a minus sign anywhere before the first digit. So "DC -5.10" and "DC 5.10" are "disputed". Before the fix, they gave a confirmed -5.1 V.
- Docs: the `bench_record_measurement` and `bench_begin_step` descriptions, rule 10 in `instructions.py` ("a safe DC residual voltage"), and the "Bench state" bullet in `mcp/README.md`.

### Tests

- `test_bench_state.py`, "QA round 6":
  - An AC reading with and without the power-check step: recorded, the note, the step open, the gate closed.
  - 230 V AC blocks an open gate, and a safe DC reading at the same point opens it again.
  - The QA case in two forms (a resistance `step_id` and an unknown `step_id`): the gate is open after "C12 0.01 V", and "VBUS 5.10 V" with the wrong step is refused but recorded. `bench_begin_step` refuses, and a retry does not add the reading again.
  - The other order: the refused unsafe reading first, then a safe reading at another point, and the gate stays closed.
  - A refused resistance reading with a voltage step records nothing.
- `test_meter_frames.py`: "DC -5.10" and "DC 5.10", and "5.10" and "- 5.10", are disputed. "DC -5.10" and "-5.10" agree.

### Checks

- `uv run pytest`: 872 passed, 1 skipped. ruff and prek on my files: pass. Nothing committed.

### Open (not in this brief)

- QA also asks to consider B-E3 again. The point is the free-text label, so one label at two points (for example "residual") lets a later safe reading replace an unsafe one. Also, after a new isolation confirmation, a safe reading at any point opens the gate, but a confirmation does not discharge a capacitor. Possible fixes: keep an unsafe reading until the same label reads safe after a waiting time, or ask for a reading at every earlier unsafe point after a new confirmation. This needs the orchestrator's decision.

## QA round 6 follow-up: the B-E3 decision (residual points)

### What I did

- Rule 1 (the new module `mcp/debug_devices_mcp/bench_points.py`): a residual reading needs a point name.
  - A residual reading is a DC reading after the isolation confirmation, or an unsafe AC reading.
  - The name is a part pin ("C12.1", "C12:1", "C12 pin 1") or a net. With a board open, `point_name` checks it against the board: the part and the pin exist, or the net exists.
  - It refuses these names with "name the point: part.pin or net (for example C12.1 or PP3V3_S5)":
    - a generic name ("residual", "test", "point", empty, or only these words and numbers);
    - a pattern;
    - a bare part (the error lists its pins);
    - a pin or a net that the board does not have.
  - Without a board, a free name is accepted, and the `notice` warns that the name must stand for this one point only.
  - Every spelling of one pin gives one key ("c12.1"), so "C12 pin 1" and "C12.1" are the same point.
  - A refused name does not lose an unsafe reading. The reading sets the last unsafe time, so the gate stays closed until a newer confirmation. The tool saves this and asks the agent to record the reading again with the point name.
- Rule 2 (`bench_state.py`): the points moved from `PowerRecord` to `BenchState.residual_points`, together with the new field `last_unsafe_at`. So a new isolation confirmation, a power change, or a probe short does not clear them. The gate opens only when all three conditions are true:
  - the confirmation is newer than the last unsafe reading;
  - every point has a safe latest reading (a newer safe DC reading at the same point, or a user clearance);
  - at least one safe DC reading comes after the confirmation.
- Rule 3: `bench_state_update` has two new fields, `clear_residual_point` (the point name) and `clear_residual_reason` (required).
  - The clearance counts as a safe reading for that point.
  - It is kept as a user action in `residual_clearances`.
  - It does not count as the safe DC reading after the confirmation.
- `server.py`: a small edit only. `build_server` gives `register_bench_state_tools` an `open_board` function: `services.board.restore()`, then the open board, or None.
- A bench state file from before this round loads. Its old residual fields are dropped, so the gate then needs a new residual reading.
- Docs: the `bench_record_measurement` and `bench_state_update` descriptions, rule 10 in `instructions.py`, and the "Bench state" bullet in `mcp/README.md`.

### Tests (`mcp/tests/test_bench_state.py`)

- Rule 1:
  - Part pin and net names on the open fixture `identity.json` (synthetic, license note in the fixture folder).
  - Refused names: a bare part, a missing pin, a missing part, a pattern, a free text.
  - Generic names are refused with and without a board.
  - The free-name warning without a board.
  - The QA case "same generic label at two points": "residual" 5.10 V is refused and closes the gate. "residual" 0.01 V is refused. "C8850 pin 1" 0.01 V is recorded, but the gate needs a new confirmation. "C8850.1" 5.10 V is the same point.
- Rule 2:
  - A safe reading at another point, and then at the same point in another spelling, does not open the gate before a new confirmation.
  - The QA case "safe reading at another point after a new confirmation" keeps the gate closed.
  - A probe short does not clear the points.
  - A power-check step stays open until a newer confirmation and a safe reading come.
  - An unsafe AC reading clears only with a new confirmation and a safe DC reading at that point.
- Rule 3: a clearance needs a reason and an existing point, is kept as a user action, and counts only for its point.
- The earlier tests now use point names ("C12.1", "VBUS"), and the fields are in `state.residual_points`.

### Checks

- `uv run pytest`: 909 passed, 1 skipped. ruff and prek on my files: pass. Nothing committed.

## QA round 8 follow-up (last round): N13 to N17, and the three B-E3 rule 2 cases

### What I did

- N13 (fail safe, `bench_state.py`): every voltage reading above 0.5 V closes the gate, also when it is "uncertain" or "disputed".
  - `result_voltage` takes the number from the LCD text of the result and of each frame, and it uses the highest one. The unit prefix counts. An unreadable unit in a voltage mode counts as volts. An overload counts as above every limit.
  - `multimeter_read` and `bench_measure` call `note_meter_reading` in `read_meter` (`server.py`). The reading becomes an unsafe "unknown point <capture id>", and the result has the new field `bench_notice`.
  - `bench_record_measurement` puts the event at the named point, or at the unknown point when the name is refused. It saves the record, then refuses an unconfirmed result.
  - When the agent records the same capture with a point name, the unknown point moves to that point.
  - An unconfirmed safe reading never opens the gate.
  - One exception: a diode-mode reading is not a voltage reading for the gate, because the meter shows its own test voltage (a diode drop is about 0.6 V). Without this exception, every diode test would close the gate. A diode reading above 3 V is already "uncertain" (item D).
- B-E3 rule 2, the three cases:
  - (a) and N15: an unsafe reading is a point in any power state, also before the current confirmation. It needs a newer safe DC reading at the same point, or a user clearance.
  - (b): a refused name keeps the unsafe reading as an unknown point, so a new confirmation and a safe reading at another point do not open the gate. A later record of that capture with a point name moves the event to that point.
  - (c): `set_point` keeps the newer entry. An older reading never replaces a newer one, and at the same time the unsafe one stays.
- N14: `label_conflict` refuses a capture id that is already recorded for another point ("capture ... is already recorded for 'VBUS': one reading belongs to one point"). Other spellings of the same point are the same point.
- N16 (`bench_points.py`): `GROUND_NET_PATTERNS` ("*GND*", "*VSS*", "0V", "EARTH*", "CHASSIS*") is the one named list. A ground net, or a pin on a ground net (checked against the open board), is refused as a point name. So it is never a point and never the safe reading.
- N17:
  - The step result is decided before the reading changes the gate.
  - The open power checks follow the gate, also when the step is refused.
  - A re-record of the same capture with a step completes that step and sets the step on the measurement.
  - The refusal no longer says "do not record it again". It says: "To attach it to a step, record the same capture_id again with that step_id and the same label."
- Safe confirmed DC readings at a named point now update that point in any power state (latest reading per point). The gate still needs one safe DC reading after the confirmation.
- Docs: the module docstring, the `bench_record_measurement` and `multimeter_read` descriptions, rule 10 in `instructions.py`, and the "Bench state" bullet in `mcp/README.md`.
- `server.py`: a small edit only: one import, and two lines in `read_meter` after `combine`.

### Tests (`mcp/tests/test_bench_state.py`, "QA round 8")

- N13:
  - An uncertain 5.10 V closes an open gate, and it is saved at "VBUS".
  - An uncertain 0.01 V opens nothing.
  - Through `multimeter_read` and through `bench_measure`: a disputed read ("5.10" and "51.0") closes the gate at an unknown point, with `bench_notice`. A later `bench_record_measurement` with "VBUS" moves the point, and the result is still refused.
- (a): the confirmation comes again before the unsafe capture is recorded.
- (b): the generic-label test, with a new confirmation before the safe reading.
- (c): 0.01 V at t1 and 5.10 V at t2, recorded in the order t2, t1.
- N15: 12.00 V with the power on is a point after the isolation.
- N14: a capture for "VBUS" cannot become "C12.1", but " vbus " is the same point.
- N16: GND, AGND, PGND, VSS_IO, and DVSS are refused without a board, and "C8850.2" (GND net) and "GND" are refused with the fixture board. A GND reading does not open the gate.
- N17: a refused step with a safe reading completes the open power check, and a re-record attaches the step.
- Changed tests: a safe reading before the confirmation is now a point; the fixture pin for the part-pin case is "L501.2", because "C8850.2" is on GND.

### Checks

- `uv run pytest`: 934 passed, 1 skipped. ruff and prek on my files: pass. Nothing committed.

### Open (for the orchestrator)

- Workflow cost of N13 and N15: each voltage reading above 0.5 V with the power on (a normal rail test) becomes a point. Before a resistance, continuity, or diode step, each of these points needs a newer safe DC reading after the isolation, or a user clearance. A `multimeter_read` that the agent does not record keeps an unknown point. The user must then clear it, or the agent must record the capture with its point name.
- Every recorded voltage reading now needs a point name (part.pin or net when a board is open). Descriptive labels such as "3V3 rail" are refused with a board open.

## Bulk clear of all residual points (default for the user's decision D7)

### What I did (`mcp/debug_devices_mcp/bench_state.py`)

- `bench_state_update` has two new fields: `clear_all_residual_points: true` and `user_words`. `clear_all_points` clears all unsafe residual points at once, also unknown points.
- `user_words` is required and must not be empty. It is stored as given (not trimmed), with the time, in one `residual_clearances` entry: `point` "all", `reason` = the user's words, `cleared_points` = the cleared keys. Each cleared point also keeps the words in `user_reason`.
- The bulk clear needs a user isolation confirmation and a safe DC reading at a point that is not a ground net, after that confirmation. The anchor is the newest such reading.
- It refuses, with the reason, in these cases:
  - no confirmation;
  - no safe reading after the confirmation;
  - no unsafe point is older than the anchor;
  - every unsafe point is newer than the anchor ("every unsafe point ('C12.1') is newer than the safe reading at 'VBUS'; measure those points again").
- A point whose unsafe reading is newer than the anchor stays unsafe. `notice` lists what was cleared and what was kept.
- A cleared point has no `source_id`, so a clearance never counts as "the safe DC reading after the confirmation". After a new confirmation, the gate needs a new safe reading. The last unsafe time does not change, so after an unsafe reading the gate still needs a newer confirmation.
- The tool description says: `user_words` must quote the user's own sentence exactly; never invent, shorten, or paraphrase it. Rule 10 in `instructions.py` and the "Bench state" bullet in `mcp/README.md` say the same.

### Tests (`mcp/tests/test_bench_state.py`, "bulk clear")

- Refusals: no words, an empty string, only spaces; no confirmation; a safe reading only before the confirmation; every unsafe point newer than the anchor; a GND point (from an old record) as the only safe reading.
- Cleared and kept: 12 V at PP12V and 5.10 V at C12.1 with the power on, then an isolation, VBUS 0.01 V, and L1.1 5.10 V. The bulk clear clears PP12V and C12.1, keeps L1.1, stores the words as given, and the gate stays closed.
- The gate opens after a bulk clear. After a new confirmation, it needs a new safe reading again.
- The unknown point of a `multimeter_read` is cleared too.

### Checks

- `uv run pytest`: 943 passed, 1 skipped. ruff and prek on my files: pass. Nothing committed.

## QA round 10 follow-up: N13 rest, N24, N25, N26, and the N16 and N17 gaps

I edited only `bench_state.py`, `bench_points.py`, three small parts of `server.py`, the README, and my tests. I did not edit `multimeter.py`, `meter_frames.py`, `config.py`, `constants.py`, or `pyproject.toml` (dd-meter works in them).

### What I did

- N13 rest (`lcd_voltage`, `result_voltage`):
  - A diode reading above the diode test voltage (`--max-diode-voltage`, default 3.0 V) counts as a voltage for the gate, because the dial is probably on DC V. A diode reading at or below the limit, and an open diode ("OL"), do not count.
  - Each frame uses its own mode (`FrameReading.mode`), so a "0.60" diode frame and a "5.10" DC V frame count as 5.10 V.
  - The low gap (3): with an unreadable unit, the mode "other" counts as volts too (`UNKNOWN_UNIT_VOLT_MODES`), as DC V and AC V do.
  - `BenchStateStore` has the limit (`max_diode_volts`), and `server.py` gives it `settings.max_diode_voltage`.
- N24 (`clear_all_points`): a cleared point now has the anchor's time, not the time of the clear, because the bulk clear covers only the readings up to the anchor. So a kept unsafe reading (newer than the anchor) replaces the clearance when its capture gets that point's name later. The case from the report now keeps C12.1 unsafe. `cleared_points` lists only the points that were really cleared.
- N25 (the store lock):
  - `BenchStateStore.update(change)` holds one lock from load to save: a lock file next to the record (`fcntl.flock` with a 5 s limit, as in the monitor settings store) and a thread lock. `update_async` runs it in a worker thread.
  - Every writer uses it: `bench_state_update` (its body is now `apply_update` with a `StateUpdate`), `bench_record_measurement`, `bench_probe_short`, `add_photo`, and `note_meter_reading` (now async; `read_meter` awaits it).
  - `open_board()` runs before the lock, so a slow board load does not hold the lock.
  - A `ResidualKeptError` saves the record, then goes up; any other error saves nothing.
- N26 (`reopen_power_checks`): when a point becomes unsafe, each completed power-check step opens again, with the reason "reopened: the unsafe reading 5.10 V at 'C12.1' closed the safety gate". The next safe reading that opens the gate completes it again and clears the reason.
- N16 gaps: `GROUND_NET_PATTERNS` also has "*GRND*" and "*GROUND*", and `is_ground` removes spaces ("0 V" is "0V"). `gate()` ignores a ground point from an old record.
- N17 wording: "To attach it to a step that fits this reading (a voltage or power-check step), record the same capture_id again with that step_id and the same label."
- Docs: the "Bench state" bullet in `mcp/README.md` and the safety sentence in the `multimeter_read` description.

### Tests (`mcp/tests/test_bench_state.py`, "QA round 10")

- N13:
  - Diode 5.10 V counts; 0.60 V, 2.95 V, and "OL" do not; mode "other" with no unit counts; a limit of 2.0 V counts 2.50 V.
  - A "0.60" diode frame and a "5.10" DC V frame count as 5.10 V.
  - `multimeter_read` with "5.10 V" diode in both frames: uncertain, with `bench_notice` and an unknown point.
  - A limit of 6 V in the store counts nothing.
- N24: the case from the report. After the bulk clear, recording the kept capture as "C12.1" makes C12.1 unsafe. A new confirmation and "VBUS" 0.01 V do not open the gate.
- N25:
  - A slow `open_board` in `bench_record_measurement` while `note_meter_reading` writes an unsafe point: both entries are saved, and the gate is closed.
  - 40 `add_photo` threads and one `note_meter_reading` on a real file: nothing is lost.
- N26: a completed power check opens again after 5.10 V at another point, and completes again after the new safe readings.
- N16 and N17: "GROUND", "Ground", "GRND", "0 V", "chassis_gnd", and "SIG_GROUND" are refused. An old GND point does not count. The refusal asks for a step that fits.

### Checks

- `uv run pytest`: 972 passed, 1 skipped. ruff and prek on my files: pass. Nothing committed.

## QA round 11 follow-up: N32 and N33

### What I did (`mcp/debug_devices_mcp/bench_state.py`)

- N32: a diode mode that only the user's dial confirmation gives does not hide a DC voltage.
  - `result_voltage` builds a `DiodeContext` from the result: `user_diode` (the checked mode is diode and `mode_source` is "user") and `model_read_volts` (`model_mode` is DC V or AC V).
  - `diode_rule` counts a diode reading as a voltage in three cases:
    - above `--max-diode-voltage`, as before;
    - with a user diode mode, when the model read DC V symbols;
    - with a user diode mode, when the value is above a typical diode drop (`TYPICAL_DIODE_DROP_MAX`, 1.0 V).
  - The reason goes into the gate notice, for example: "the user confirmed diode mode, but the model read DC V symbols: it counts as a voltage (fail safe)". A diode reading from the LCD alone keeps the old rule.
  - Cost: with a user diode mode, an LED test (1.6 to 3 V) closes the gate. It then needs a new DC reading at that point or a clearance.
  - I did not edit `multimeter.py`: the fields `model_mode` and `mode_source` already exist in `MeterResult`.
- N33: an unsafe reading is not lost after a lock timeout.
  - `note_meter_reading` keeps the result in the store (`keep_unsaved`) and starts one background retry (`flush_later`: 12 tries, 5 s apart).
  - Every `load` of this server applies the kept readings, so the gate stays closed here.
  - The next `update` (any bench-state write, also `add_photo`) saves them and forgets them only after the save.
  - The `multimeter_read` notice says "the bench state is NOT SAVED YET". Every bench-state answer (`view`) has a note while readings are unsaved.
  - Limit: another MCP server sees the reading only after the save.
- Docs: the "Bench state" bullet in `mcp/README.md`, and the safety sentence in the `multimeter_read` description (`server.py`, one sentence).

### Tests (`mcp/tests/test_bench_state.py`, "QA round 11")

- N32:
  - `diode_voltage` in 6 cases: user diode with DC V symbols (2.50 V and 0.62 V count); user diode above 1 V counts; user diode at 0.62 V does not; an LED test from the LCD does not; 5.10 V from the LCD counts.
  - Through `multimeter_read`: user diode, the model reads DC V "2.50". The result is a confirmed diode reading, `bench_notice` explains the rule, and the gate is closed.
- N33:
  - Another holder of the lock file and a 7.00 V reading: the notice says "NOT SAVED YET"; this server counts the point, and its answer says that it is not saved; the file has no point. After the lock is free, the background retry saves it, and the note goes away.
  - Without the retry, the next `add_photo` saves the photo and the kept reading together.

### Checks

- `uv run pytest`: 1068 passed, 1 skipped. One earlier run had 7 failures in `test_sevenseg_compare.py` while dd-meter changed `sevenseg/`. They passed again after that change: 28 passed alone, and the full run was clean. ruff and prek on my files: pass. Nothing committed.

## N45: snapshot retry during a camera rebind

### What I did

- The contract rule in `docs/phone-api.md`: during a camera rebind, the app answers `/v1/snapshot` with 503 `camera_not_ready` after its own 5 s wait. Clients try again until their start timeout ends.
- `server.py`: `Services.snapshot_when_ready` (new) is the only caller of `PhoneClient.snapshot`. `Services.phone_snapshot` (the old `server.py:638`) uses it, and so do `phone_snapshot` and `bench_measure`.
  - On a `PhoneApiError` with the code `camera_not_ready`, it tries again every `poll_interval` until `app_start_timeout` ends. Then it raises that normal error ("phone API error 503 camera_not_ready: camera change still running").
  - Other errors do not retry.
  - One import (`Still`). No other `phone.snapshot()` call exists in the MCP server.
- The fake phone did not need a change: the new test file has its own `BusyPhone` subclass of the test `FakePhone`. I did not change `scripts/fake_phone.py` or `mcp/tests/test_server.py`.

### Tests (`mcp/tests/test_snapshot_retry.py`)

- 503 `camera_not_ready` twice, then 200: `phone_snapshot` gives the still after 3 requests.
- 503 until the start timeout (50 ms in the test): the tool error has "503 camera_not_ready" and the app's message, and there was more than one request.
- 500 `capture_failed`: the tool error comes after one request (no retry).

### Checks

- `uv run pytest`: 1077 passed, 1 skipped. ruff and prek on `server.py` and the new test file: pass. Nothing committed.
