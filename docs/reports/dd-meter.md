# dd-meter report: local 7-segment decoder (compare mode only)

Brief: `docs/briefs/sevenseg.md`. I did not commit.

## Summary

- The new package `mcp/debug_devices_mcp/sevenseg/` reads the multimeter LCD from the webcam crop in about 6-20 ms per frame. It has no network access.
- The local result never changes the vision result, the status, the confidence, the bench gate, or `bench_measure`.
- The setting `--meter-local-decoder off|compare` (`DEBUG_DEVICES_METER_LOCAL_DECODER`) controls it. The default is `off`. In `off` mode, no local code runs.
- In `compare` mode, `multimeter_read` and `bench_measure` add `local_reading` and `local_agrees`. The monitor log entry gets a short `local_decoder` detail line.
- The CLI `debug-devices-sevenseg` has three subcommands: `calibrate`, `read`, and `evaluate`.
- 73 new tests use synthetic LCD images only. No real frame is in the repo.
- Real check: not done. The webcam showed the ceiling and a lamp, not the meter.

## Files

New files (mine):

| File | Content |
|---|---|
| `mcp/debug_devices_mcp/sevenseg/profile.py` | The meter profile (pydantic): LCD corners in the crop, digit boxes, segments, points, sign, and symbols. JSON in `<state>/sevenseg/profile.json`. |
| `mcp/debug_devices_mcp/sevenseg/template.py` | The built-in Proster T21D layout (normalized LCD coordinates). |
| `mcp/debug_devices_mcp/sevenseg/decode.py` | Warp, background normalization, region scores with an adaptive threshold, digit patterns, point, sign, and symbols. |
| `mcp/debug_devices_mcp/sevenseg/symbols.py` | Symbols to unit, mode, and flags. |
| `mcp/debug_devices_mcp/sevenseg/reading.py` | `LocalReading`: the same fields as `MultimeterReading` where possible, plus confidence, contrast, and weak regions. |
| `mcp/debug_devices_mcp/sevenseg/stability.py` | `combine_readings` (the last M readable frames must agree) and `sample` (N frames per second over a window). |
| `mcp/debug_devices_mcp/sevenseg/calibrate.py` | LCD corner search, template fit, and the annotated image. |
| `mcp/debug_devices_mcp/sevenseg/compare.py` | Compare mode: agreement per field, the dataset write, and `compare_local` for `read_meter`. |
| `mcp/debug_devices_mcp/sevenseg/dataset.py` | The bounded dataset (500 frames, the oldest go first). |
| `mcp/debug_devices_mcp/sevenseg/evaluate.py` | The agreement rate per field and the list of disagreements. |
| `mcp/debug_devices_mcp/sevenseg/cli.py`, `__main__.py` | The CLI. |
| `mcp/debug_devices_mcp/meter_mode.py` | `MeterMode`, moved out of `multimeter.py` (see the hooks). |
| `mcp/tests/sevenseg_synth.py` | The synthetic LCD renderer for the tests. |
| `mcp/tests/test_sevenseg_decode.py`, `test_sevenseg_calibrate.py`, `test_sevenseg_compare.py` | The tests. |

Hooks in existing files (approved by dd-orchestrator):

| File | Change |
|---|---|
| `multimeter.py` | `MeterMode` now comes from `meter_mode.py` (the old import path still works). `MeterResult` has two new fields: `local_reading` and `local_agrees` (default `None`). |
| `config.py` | The setting `meter_local_decoder` (default `off`). |
| `constants.py` | `env.METER_LOCAL_DECODER`. |
| `server.py` (`read_meter` only) | In `compare` mode, after `combine()` and after the bench gate, one call to `compare_local`. |
| `pyproject.toml` | The entry point `debug-devices-sevenseg`. |
| `.env.example` | `DEBUG_DEVICES_METER_LOCAL_DECODER=off` with a comment. |
| `mcp/tests/conftest.py` | The `settings` fixture removes `DEBUG_DEVICES_METER_LOCAL_DECODER` from the environment (direnv loads `.env` into the shell). |

`meter_frames.py` needs no change.

Why `MeterMode` moved: `MeterResult` now holds a `LocalReading`, and `LocalReading` needs `MeterMode`. With the enum in `multimeter.py`, the two modules import each other.

## How the decoder works

1. Warp the four LCD corners to a fixed size (200 px high, the width from the aspect ratio).
2. Estimate the background with a morphological close. This removes the thin dark strokes. The darkness of each pixel is relative to its local background, so uneven light and mild glare cancel.
3. Set the ink threshold of the frame: half of the 97th percentile of the darkness in all regions, and at least the Otsu split. If the contrast is below 0.12, the frame is `unreadable`.
4. Score each region by its fill (the part of its pixels that is ink). A segment is on at a fill of 0.5 or more. A symbol is a thin glyph, so it has a lower fill limit (0.1) and a lower pixel threshold.
5. Decode each digit from its 7 segments. The table has the digits, blank, `-`, `L`, and some letters. `O` of `OL` is the `0` pattern. A pattern with one unclear segment gives the only near pattern, with a problem note. Other patterns give `?`.
6. The point with the largest fill gives `digits_before_point`. The sign region gives `negative`. The symbols give the unit, the mode, and the flags.
7. The overall confidence is the weakest region that decides the value, the unit, or the mode. Status: `read`, `uncertain`, `unstable`, `unreadable`, or `no_profile`.

A frame that has a size other than the calibrated crop is `unreadable`: the webcam crop changed, so calibrate again.

## Compare mode

- `compare_local` runs only for `source` `webcam`. The profile is for the webcam crop.
- It decodes the same JPEGs as the vision model, in a thread. All frames must agree (as for the vision result).
- `local_agrees` is true only when both are readable and the local reading agrees with the combined vision result and with each vision frame. The compared fields are digits, point, sign, unit (family and prefix), and mode (the model's LCD mode, not a mode that the user confirmed).
- It saves one JSON entry per frame to `<state>/sevenseg/dataset/`: the raw vision reading, the local reading, and the region fills. It keeps at most 500 entries. Round 11 changed the images (see "Round 11 follow-up"): the dataset never keeps a webcam frame.
- If the local code fails, the result stays as it is, and the server log gets the error.

## CLI

Get a crop only through the MCP tools: `webcam_snapshot` with `save_path`.

```sh
debug-devices-sevenseg calibrate --image crop.jpg            # profile + profile.annotated.png in <state>/sevenseg
debug-devices-sevenseg read --images a.jpg --images b.jpg    # decode saved crops and combine them
debug-devices-sevenseg evaluate                              # agreement per field in the dataset
debug-devices-sevenseg evaluate --redecode                   # the same, with the current profile on the saved frames
```

`<state>` is `$XDG_STATE_HOME/debug-devices` (default `~/.local/state/debug-devices`).

Calibration finds the LCD as the largest bright quadrilateral that does not touch the crop border. Then it moves and scales the digit row of the template (a coarse search, then a fine search) until the segments are most clear. Check the annotated image: green regions are on, red regions are off. Move a region in the profile JSON if it misses its segment or symbol.

## Tests

- `uv run pytest mcp/tests/test_sevenseg_*.py`: 73 passed.
- Digits: every digit in every position, the point in each position, the sign, a blank leading digit, `OL`, a blank digit between digits, and the digit table.
- Symbols: V, mV, AC V, mA, AC A, kΩ, continuity, diode, nF, µF, kHz, %, the flags, and no unit symbol.
- Degraded frames: blur, noise, glare at two positions, all three together, JPEG quality 50, and another perspective.
- Low contrast and a changed crop give `unreadable`.
- Calibration: corners within 3 px next to a bright wall, the fit of a moved digit row, no LCD, and an unknown template.
- The profile round trip, a missing profile, and a broken profile.
- Compare mode through `multimeter_read` with a fake vision client: the vision fields are the same in `off` and `compare` mode, for frames that agree and for frames that disagree. The `off` mode runs no local code. The dataset gets 2 frames per read.
- The dataset limit, the evaluation counts, and the CLI (`calibrate`, `read`, `evaluate --redecode`).
- `uv run pytest` (the full suite, with the hooks and the work of the other agents in the tree): 1045 passed, 1 skipped.
- `prek run --files <my files>` in the dev shell: all hooks passed.

## Real check

I started a test MCP server with `--no-ui-open-browser` and `--meter-local-decoder compare`. I used only `webcam_snapshot` with `save_path`. The server took the frames from the running dev monitor (the busy-webcam path of `remote_webcam.py`), with the crop 562,288,203,367 of that monitor.

The crop showed the ceiling and a lamp, not the meter. So I did not calibrate on a real frame, and I did not call `multimeter_read`. There is no agreement number yet.

A second fact: with `--ui-port 18791`, the test server looked for the other monitor on port 18791 and failed with "Device or resource busy". With the default port, it used a free port for its own page and found the dev monitor on 18766.

## Open items

1. Real calibration. When the webcam shows the meter with digits, do these steps:
   1. Call `webcam_snapshot` with `save_path`.
   2. Run `debug-devices-sevenseg calibrate --image <path>`.
   3. Check the annotated image.
   4. Set `DEBUG_DEVICES_METER_LOCAL_DECODER=compare`.
   5. After some reads, run `debug-devices-sevenseg evaluate`.
2. The T21D template. The digit row, "AUTO", and "kΩ" come from `docs/images/demo-meter.jpg`. The positions of the other symbols (AC, DC, HOLD, diode, continuity, battery, and the unit column) are estimates. The first real calibration image will show which ones miss.
3. The demo photo shows "4. 70 kΩ" with a wide gap after the point. I cannot tell if the gap is a blank digit or a different digit pitch. The fit can move the digit row but not one digit. If the real digit pitch is not even, edit the digit boxes in the profile.
4. `sample()` (N frames per second) has no live caller. The CLI reads files only, and agents get frames only through the MCP tools. A live standalone mode needs a frame source in the MCP server.
5. `MeterResult` JSON now has `"local_reading": null, "local_agrees": null` in `off` mode.
6. Docs: done. `AGENTS.md` (Parts, by dd-orchestrator) and `mcp/README.md` ("Local 7-segment decoder (compare mode)") describe the decoder.

## Round 11 follow-up (`docs/briefs/qa-round11-followup.md`)

### N37 (privacy): the dataset never keeps a webcam frame

What happened: in compare mode, `LocalMeter._save` wrote every frame JPEG as it came from the webcam. When no crop was set, or the frame did not match the profile, this was the full webcam frame. The dataset could keep up to 500 of them.

What changed:

- An entry has an image only when a webcam crop is set and the frame size is the profile size. The image is the warped LCD only (`<entry_id>.lcd.png`, the LCD area of the profile at the warp size, 200 px high). It is never the webcam crop.
- Without a crop, or with a frame that does not match the profile, the entry has only the text results and a `note` (`image: "none"`).
- `read_meter` (`server.py`) passes `crop_set=services.webcam.crop is not None`. For frames of another monitor, this is the crop of that monitor.
- The JSON goes first, then the image (N39). A prune removes the oldest entries, every `*.jpg` file (webcam frames of the first version), and every image without an entry.
- `add()` logs a warning when it removes old webcam frames. `debug-devices-sevenseg evaluate` prunes first and prints the count.
- `evaluate --redecode` decodes the saved LCD images with the regions of the current profile. It scales an image to the current warp size. A change of the LCD corners needs new frames. Entries without an image keep their saved local reading.
- Texts: `mcp/README.md`, `.env.example`, and the setting description say that the dataset keeps only the LCD area.

Removed frames:

- The state dataset folder (`~/.local/state/debug-devices/sevenseg/dataset`) does not exist. `~/.local/state/debug-devices/` has no `sevenseg/` folder. Removed: 0 frames.
- dd-qa's scratchpad (session `e66efcd8`) has 7 dataset folders from the Round 11 check, with 14 `.jpg` files: 12 at 420x300 (synthetic test frames) and 2 at 1280x720 (`priv/sevenseg/dataset`, the N37 case). I did not open or remove them: they belong to another agent. The new prune removes them if a new version of the code writes to those folders.

### N38 (the log line)

`log_line` said "agrees" when there was no comparison (no profile). Now it says "agrees" only when `local_agrees` is true, "differs in <fields>" for differences, and "not compared" otherwise.

### Tests

New or changed tests in `mcp/tests/test_sevenseg_compare.py`:

- With a crop: 2 entries and 2 LCD images at the warp size, no other file.
- Without a crop: 2 entries, no image, the note.
- A 1280x720 frame with a 420x300 profile: the local reading is `unreadable`, and the entry has no image and the note.
- Through `multimeter_read`: with a crop, 2 LCD images; without a crop, no image.
- An entry of the first version with its `.jpg` and an image without an entry: `evaluate` removes both, prints the count, and still reads the old entry.
- A new entry removes an old `.jpg` too.
- `evaluate --redecode` on LCD images, also with a profile of another aspect.
- The log line: "not compared" without a profile, "differs in point" for a moved point.

Not in this round (low, open): N40, N41, and the CLI tracebacks. See the next section.

## Round 11 low items (N40, N41, CLI errors)

### N40: a profile with missing regions

What happened: `LcdLayout` accepted a profile without all 7 segments or with the wrong number of decimal points. The decoder then failed with a `KeyError`. Compare mode caught the error and returned no local fields and no log line, the same as off mode.

What changed:

- `LcdLayout` needs at least 1 digit, all 7 segments (a to g), and one decimal point after each digit but the last. Otherwise the profile is not valid.
- `load_profile` gives one line per problem, for example: `layout: Value error, missing segments: g. A digit needs all 7 (a to g)`. A profile that is not readable (for example a folder) gives "cannot read the meter profile".
- Compare mode with such a profile gives `local_reading.status: "no_profile"` with that message.
- If the local code raises an error, compare mode now sets `local_reading` (status `unreadable`, problem "the local decoder failed: <error>") and `local_agrees: false`, and the log line says so. The vision result stays the same.
- The log line of an unreadable local reading shows its first problem, not only "unreadable".

### N41: stability

- The frame key now has the mode. An AC/DC change between frames gives "unstable".
- When fewer frames are readable than must agree, the combined reading is `unreadable` (`readable: false`, `value: null`), with the problem "1 of 2 frames are readable, 2 must agree (...)". Before, it was "unstable".
- The problem text shows each frame with its unit and mode, for example "5.10 V dc_voltage, 5.10 V ac_voltage".

### CLI errors

- A user error prints one line on stderr (`debug-devices-sevenseg: error: ...`) and exits with code 1. It prints no traceback.
- User errors: a missing file, a folder instead of a file, a file that is not an image, a missing or bad profile, no LCD in the image, an unknown template, a bad option value (for example `--min-agree 0`), and an annotated image name with an unknown extension.
- `calibrate` encodes the annotated image before it writes anything. With a bad image name, it writes neither the profile nor the image.
- Other errors (bugs) still show the traceback.

### Tests

- N40: a profile without segment g, and a profile with 2 points for 4 digits: `load_profile` names the problem, and compare mode gives `no_profile` with the message.
- The local failure test now checks the failure reading, `local_agrees: false`, the log line, and that the vision fields do not change.
- N41: an AC/DC change is unstable; 1 readable frame of 2 is unreadable; 2 readable frames of 3 that agree are read.
- CLI: 10 user errors give one line on stderr, exit code 1, and no traceback.
- `uv run pytest mcp/tests/test_sevenseg_*.py`: 87 passed. The full suite: 1074 passed, 1 skipped. `prek` on my files: passed.

## QA round 13 (`docs/briefs/qa-round13.md`, section dd-meter)

Base commit: `3a2fd23`. Not committed.

### N53 (data loss): the prune deleted files in any folder

What happened: `evaluate` ran `Dataset.prune` first. The prune deleted every `*.jpg`, every `*.json` after the newest 500, and every `*.lcd.png` without an entry, in any `--dataset` folder.

What changed:

- `evaluate` only reads. It never deletes or changes a file.
- Only the compare writer (`Dataset.add` from `LocalMeter`) prunes, and only its own folder (`<state>/sevenseg/dataset`).
- The dataset reads, counts, and deletes only files with a UUID v7 entry name: `<uuid>.json`, `<uuid>.lcd.png`, and `<uuid>.jpg` (a webcam frame of the first version). The name must be a canonical UUID v7 (`entry_id_of` in `sevenseg/dataset.py`). All other files stay, also a UUID v4 name.
- The 500 limit counts only entry files.

### N54: a dataset write error replaced a valid local reading

What happened: a write error (for example a full disk) raised inside `LocalMeter.compare`. `compare_local` then replaced the valid local reading with "the local decoder failed".

What changed: `LocalMeter._save` catches `OSError` and `cv2.error`. It logs a warning ("the local meter dataset in <folder> was not saved"), and the log line gets "; dataset not saved (see the server log)". The local reading and `local_agrees` stay. The vision result does not change.

### N44 and N45

My files have no "N44" or "N45" text. No rename.

### Tests

- A folder with `holiday1.jpg`, `holiday2.lcd.png`, a UUID v4 `.jpg`, and 503 other `.json` files: `evaluate` changes no file and counts 0 entries. The writer (limit 3) then adds 5 entries: every other file stays, and only the newest 3 entries remain.
- An entry of the first version with its `<uuid>.jpg` and an orphan `<uuid>.lcd.png`: `evaluate --redecode` changes no file. The next write removes only the old `.jpg` and the orphan image.
- `entry_id_of`: a UUID v7 name is an entry; an upper-case UUID, a UUID v4, and `holiday1.jpg` are not.
- A dataset path that is a file (the write fails): the local reading stays `read` with "5.10", `local_agrees` is true, the log line says "dataset not saved", and the server log has the warning. The same through `compare_local`.
- `uv run pytest mcp/tests/test_sevenseg_*.py`: 89 passed. `prek` on my files: passed.
- The full suite: 1079 passed, 1 skipped, 1 failed. The failure is `test_remote_ports.py::test_a_busy_webcam_uses_the_running_monitor_on_the_default_port`: it tests `remote_webcam.py`, which dd-ui changes in this round (N50, N51). It fails alone too, and it does not use the sevenseg code.
