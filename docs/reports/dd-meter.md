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
- It saves each frame crop to `<state>/sevenseg/dataset/<uuid7>.jpg` with a JSON file: the raw vision reading, the local reading, and the region fills. It keeps at most 500 frames.
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
6. Docs: `AGENTS.md` (Parts) and `mcp/README.md` do not mention `sevenseg/` yet. These files belong to other agents.
