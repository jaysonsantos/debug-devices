# debug-devices

Tools that let a coding agent (Claude Code) see and measure real hardware while it debugs.

![The monitor page: webcam crop, live phone screen, phone controls, and the activity log](docs/images/monitor-demo.webp)

A short MP4 of the same demo is in [docs/images/monitor-demo.mp4](docs/images/monitor-demo.mp4).

## Parts

- `android/`: an Android app. It is a remote-controlled back camera with zoom in, zoom out, torch, and snapshot. It serves the HTTP API in [docs/phone-api.md](docs/phone-api.md). See [android/README.md](android/README.md).
- `mcp/`: a Python MCP server (stdio) for Claude Code:
  - It controls the phone camera through an ADB port forward.
  - It reads a multimeter through the PC webcam and an OpenRouter vision model.
  - It serves a local monitor page that shows the webcam, the live phone screen, and each tool call.
  - It reads boardview files and answers questions about parts, nets, and their positions on the board.
  - See [mcp/README.md](mcp/README.md).
- `boardview/`: `obv-dump`, a small C++ CLI on the [OpenBoardView](https://github.com/OpenBoardView/OpenBoardView) parsers. It reads a boardview file and writes JSON. See [boardview/README.md](boardview/README.md).
- `docs/`: the API contracts ([phone](docs/phone-api.md), [boardview JSON](docs/boardview-json.md)), the research notes, the QA guide, and the agent reports.
- `scripts/`: QA tools, the live reload monitor, and the demo recorder.

## Quick start

1. Open the dev shell: `nix develop`. With direnv, put `dotenv_if_exists` and `use flake` in `.envrc`.
2. Copy `.env.example` to `.env`. Set `OPENROUTER_API_KEY`. If more than one ADB device is connected, set `DEBUG_DEVICES_ADB_SERIAL` to the serial of the phone.
3. Build and install the app on the phone. The steps are in [android/README.md](android/README.md).
4. Run `uv sync`.
5. Add the server to Claude Code. Replace `<repo>` with the absolute path of this repository:

   ```sh
   claude mcp add debug-devices -- uv run --directory <repo> debug-devices-mcp
   ```

When the server starts, the monitor page opens in a new Firefox window. Point the webcam at the multimeter. Then draw the crop box around the display on the page.

## Board files (OpenBoardView)

The agent can read the boardview file of the board that you repair. Then it can tell where a part is, which pins are on a net, and which test point is near a part. With a phone photo of the board, it can also find a part in the photo.

How it works:

1. `obv-dump` reads the file with the OpenBoardView 10.0.0 parsers (MIT) and writes JSON (contract: [docs/boardview-json.md](docs/boardview-json.md)). The flake builds it from a pinned tag plus the patches in `boardview/patches/`. The dev shell has it on `PATH`.
2. The MCP server runs `obv-dump` as a subprocess with a timeout, and keeps each board in memory by its SHA-256. A parser crash on a bad file does not stop the server.
3. The board tools use millimeters. The part box comes from the pins when the file has no box.

| Tool | What it does |
|---|---|
| `board_open` | Loads a file. Gives the format, the counts of parts, pins, nets, and test points, and the board size. |
| `board_find_part` | Finds parts by reference (`U2`), by glob (`C1*`), or by manufacturer code. |
| `board_part_pins` | Gives each pin of a part: number, name, net, position, and side. |
| `board_find_net` | Gives the parts and pins on a net, its test points, and the nearest test point to each part. |
| `board_parts_near` | Gives the parts near a part or a point. |
| `board_render` | Draws one side of the board as a PNG, with the parts and nets that you ask for marked. |
| `board_register_photo` | Maps board positions to a phone photo from 4 or more reference parts (homography). |
| `board_locate_in_photo` | Gives the pixel positions of parts or net pins in that photo, and can mark them on it. |

Formats: the formats that OpenBoardView reads. Examples: Test_Link `.brd`, BRD2, `.bdv`, `.bvr`, `.asc`, Samsung `.cad`, GenCAD `.cad`, `.cst`, ASUS `.fz`, ASRock `.cae`, Altium ASCII, and XZZ `.pcb`. OpenBoardView cannot read Allegro binary `.brd` and TVW files.

Limits:

- Many boardview files have no part rotation and no part box. The positions come from the pins.
- `.fz`, `.cae`, and XZZ files are encrypted. You supply the keys in `.env` (`BOARDVIEW_FZ_KEY`, `BOARDVIEW_CAE_KEY`, `BOARDVIEW_XZZ_KEY`). This repository has no keys.
- Boardview files from repair sites are proprietary. Keep them outside this repository. The local test reads one board from `BOARDVIEW_TARGET` and skips when it is not set.

For boards that you designed in KiCad, the design files have better data (exact rotation and part boxes). See the research in [docs/research/boardview-claude.md](docs/research/boardview-claude.md).

## Live reload

`scripts/dev-monitor.sh` runs the MCP server with the monitor page and no MCP client. It uses `watchexec` to restart the server when a file in `mcp/debug_devices_mcp` changes (`.py`, `.html`, `.js`, `.css`). Stdin stays open with no input, so the stdio server does not stop.

```sh
scripts/dev-monitor.sh
```

Open http://127.0.0.1:18766/ one time. The page connects again after each restart. The script gives its arguments to the server, for example `scripts/dev-monitor.sh --adb-serial <serial>`.

Only one process can read the webcam. When this monitor runs, other MCP servers (for example Claude Code sessions) take their webcam frames from it. See "More than one MCP server" in [mcp/README.md](mcp/README.md).

## Record the demo

`scripts/record_demo.py` records the page of a running monitor with Playwright (Firefox, for the WebCodecs H.264 phone screen). It then writes `docs/images/monitor-demo.webp` and `docs/images/monitor-demo.mp4`:

```sh
uv run --with playwright playwright install firefox   # one time
uv run python scripts/make_demo_meter.py              # docs/images/demo-meter.jpg
uv run --with playwright python scripts/record_demo.py --fake-phone docs/images/demo-meter.jpg
```

- `scripts/make_demo_meter.py` takes a webcam frame of the running monitor. It keeps only the multimeter area and draws a 7-segment reading ("4.70 kΩ") on the LCD.
- With `--fake-phone`, the recorder starts `scripts/fake_phone.py` with that photo as the snapshot. It also starts a separate demo MCP server with the fake adb and its own settings. The demo server takes the webcam frames from the monitor on port 18766.
- The script clicks Connect, zooms, sets the torch on and off, takes a snapshot, scrolls, and reads the multimeter from the phone snapshot. This sends one request to OpenRouter, and one more if the first fails.
- The webcam can see more than the multimeter. Outside the crop box, the recorder darkens the view by 82% and blurs it. It also hides the log rows from before the recording.
- Turn on the room light, and look at the frames before you publish a new recording.

## Development

Run these in the dev shell:

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
boardview/tests/run.sh
(cd android && ./gradlew assembleDebug testDebugUnitTest)
prek run --all-files
```

To run the local board test, set `BOARDVIEW_TARGET` to a board file on your PC.
