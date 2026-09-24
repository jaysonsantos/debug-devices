# debug-devices

Tools that let an AI agent see and measure real hardware while you debug it: a phone camera, a multimeter read through a webcam, and the boardview file of the board.

![The monitor page: webcam crop, live phone screen, phone controls, and the activity log](docs/images/monitor-demo.webp)

A short MP4 of the same demo is in [docs/images/monitor-demo.mp4](docs/images/monitor-demo.mp4).

## What it does

You debug a board on your bench. An AI agent (Claude Code, Codex, or ChatGPT desktop) helps you, and this project gives it eyes and data:

- **Phone camera:** the agent zooms, turns on the torch, rotates, and takes photos of the board through a small Android app.
- **Multimeter:** the PC webcam looks at the multimeter. The agent reads the value, the unit, and the mode through a vision model.
- **Board file:** the agent reads the boardview file and finds parts, nets, test points, and their positions.
- **Monitor page:** you see the webcam, the phone screen, and each action of the agent on a local web page.
- **Your instructions:** the agent reads your notes about the device and the repair (`instructions.md`) at the start of each session.

## How to use it

### 1. Set up one time

1. Open the dev shell in this repository: `nix develop`. With direnv, put `dotenv_if_exists` and `use flake` in `.envrc`.
2. Copy `.env.example` to `.env`. Set `OPENROUTER_API_KEY`. Set `DEBUG_DEVICES_ADB_SERIAL` to the serial of your phone (`adb devices`). Other Android devices on the network then get no command.
3. Build the app and install it on the phone. The steps are in [android/README.md](android/README.md). Allow the camera when the app asks.
4. Run `uv sync`.
5. Point the PC webcam at the multimeter display.

### 2. Write your instructions

Copy the template and fill it in:

```sh
cp instructions.example.md instructions.md
```

Write the device under test, the fault, the paths of the board file and the schematic, your bench set-up, safety limits, the measurements so far, the next step, and your preferences. Git ignores `instructions.md`, so it stays on your PC. Do not put secrets in it.

When an agent connects to the server, it gets the order to call `bench_instructions` first. That tool returns your file. The file is read again on each call, so you can add new measurements during a session. Details: [Bench instructions](#bench-instructions-instructionsmd).

### 3. Connect an agent

Choose one:

- **Claude Code (CLI):** run `scripts/claude.sh` from any folder. Add `--browser` as the first argument to open the monitor page in Firefox.
- **Codex (CLI):** run `scripts/codex.sh`, with the same `--browser` option.
- **Claude Code in this folder:** put the server in `.mcp.json` (start from `.mcp.json.example`, with absolute paths and `nix develop`, see below). Start `claude` here and approve `debug-devices` one time.
- **Codex CLI or ChatGPT desktop in this folder:** put the server in `.codex/config.toml`:

  ```toml
  [mcp_servers.debug_devices]
  command = "nix"
  args = ["develop", "/abs/path/debug-devices", "--command", "uv", "run", "--directory", "/abs/path/debug-devices", "debug-devices-mcp", "--no-ui-open-browser"]
  startup_timeout_sec = 180
  tool_timeout_sec = 120
  ```

  Codex loads it only in this folder (the folder must be trusted). In ChatGPT desktop, open this folder in Codex.

`.mcp.json` and `.codex/config.toml` contain absolute paths, so git ignores them. `nix develop` gives the server `obv-dump`, `ffmpeg`, `scrcpy`, and `adb`.

### 4. Debug

A normal session:

1. Say **"start the bench"**. The agent runs `bench_start`: it starts the monitor page, the webcam, and the phone, opens the board file when your instructions give its path, and gives you the page URL.
2. Open the page (default `http://127.0.0.1:18766/`). Draw the crop box around the multimeter display one time. Only this area goes to the vision model.
3. Ask questions. Examples:
   - "Where is U2? Show it on the board."
   - "Which test point is near U2 on the PP3V3 net?"
   - "Zoom the phone to 3x and take a photo."
   - "Read the multimeter."
4. Add each result to `instructions.md`, or tell the agent to note it.
5. Say **"stop the bench"**. The agent runs `bench_stop`, and the webcam and the phone are free again.

**Voice:** in ChatGPT desktop, use the dictation button in Codex. You speak, and the text goes to the agent that has the tools. The phrases "start the bench" and "stop the bench" work well.

**Lazy start:** the server starts nothing until a tool needs it. At process start, it opens no port and starts no ffmpeg, adb, scrcpy, or browser. The first tool call starts the page, the first webcam use starts the webcam, and `phone_connect` starts adb. The webcam stops after 5 minutes without use. So a session that does not use the bench costs nothing.

## Tools

| Group | Tools |
|---|---|
| Bench | `bench_instructions`, `bench_start`, `bench_stop`, `monitor_open` |
| Phone camera | `phone_connect`, `phone_status`, `phone_zoom`, `phone_torch`, `phone_rotation`, `phone_snapshot` |
| Webcam and multimeter | `webcam_snapshot`, `multimeter_read` (source `webcam` or `phone`) |
| Board file | `board_open`, `board_find_part`, `board_match_marking`, `board_part_pins`, `board_find_net`, `board_parts_near`, `board_render`, `board_register_photo`, `board_locate_in_photo` |

Evidence rules: the agent answers questions about what is visible on the board from a fresh `phone_snapshot`, meter values only from `multimeter_read`, and board questions from the board tools, marked as boardview data. A visible marking stays as it is seen; `board_match_marking` gives the boardview candidates.

Details of each tool: [mcp/README.md](mcp/README.md).

## Parts

- `android/`: the Android app. It is a remote-controlled back camera (zoom, torch, rotation, snapshot). It serves the HTTP API in [docs/phone-api.md](docs/phone-api.md) on `127.0.0.1` of the phone. See [android/README.md](android/README.md).
- `mcp/`: the Python MCP server (stdio) and the monitor page. See [mcp/README.md](mcp/README.md).
- `boardview/`: `obv-dump`, a small C++ CLI on the [OpenBoardView](https://github.com/OpenBoardView/OpenBoardView) parsers. It reads a boardview file and writes JSON. See [boardview/README.md](boardview/README.md).
- `docs/`: the API contracts ([phone](docs/phone-api.md), [boardview JSON](docs/boardview-json.md)), the research notes, the QA guide, and the agent reports.
- `scripts/`: the agent start scripts, QA tools, the live reload monitor, and the demo recorder.

## Start scripts

`scripts/claude.sh` and `scripts/codex.sh` start the agent in the dev shell, with this MCP server. Run them from the project that you debug. The agent starts in the current directory, and the server reads `.env` from this repository.

```sh
~/p/personal/debug-devices/scripts/claude.sh             # Claude Code
~/p/personal/debug-devices/scripts/codex.sh              # Codex
~/p/personal/debug-devices/scripts/claude.sh --browser   # also open the monitor page in Firefox
```

- `--browser` must be the first argument. The scripts give all other arguments to the agent, for example `scripts/claude.sh --browser --model opus`.
- Without `--browser`, the page does not open. Ask the agent for `monitor_open` to get the URL.
- `scripts/agent.sh <claude|codex>` does the same work. The two scripts call it.

## Bench instructions (instructions.md)

- Default path: `instructions.md` at the repo root. Another path: `--instructions-file` or `DEBUG_DEVICES_INSTRUCTIONS`.
- At connection, the server sends a short header ("the user wants to debug hardware now, call `bench_instructions` first") and the first 8 KiB of the file as MCP server instructions. Some clients (for example Codex) keep only the header. The agent then calls `bench_instructions` and gets the full file.
- A change to the header needs a restart of the server. The tool always returns the current file.
- The server does not log the file and does not send it to OpenRouter.
- Without the file, the server works and tells the agent how to create it.

## Board files (OpenBoardView)

The agent can read the boardview file of the board that you repair. Then it can tell where a part is, which pins are on a net, and which test point is near a part. With a phone photo of the board, it can also find a part in the photo.

How it works:

1. `obv-dump` reads the file with the OpenBoardView 10.0.0 parsers (MIT) and writes JSON (contract: [docs/boardview-json.md](docs/boardview-json.md)). The flake builds it from a pinned tag plus the patches in `boardview/patches/`. The dev shell has it on `PATH`.
2. The MCP server runs `obv-dump` as a subprocess with a timeout, and keeps each board in memory by its SHA-256. A parser crash on a bad file does not stop the server.
3. The board tools use millimeters. The part box comes from the pins when the file has no box.

| Tool | What it does |
|---|---|
| `board_open` | Loads a file. Gives the format, the counts of parts, pins, nets, and test points, and the board size. |
| `board_find_part` | Finds parts by reference (`U2`), by glob (`C1*`), or by manufacturer code. Without a match, it gives the parts whose name starts with the query. |
| `board_match_marking` | Matches a marking that you see on the board (for example `U730`) to boardview parts: exact, start of a name (cut-off or hidden silkscreen), part of a name, or characters read wrong (O/0, I/1). With a registered photo position, it names the nearest part. |
| `board_part_pins` | Gives each pin of a part: number, name, net, position, and side. |
| `board_find_net` | Gives the parts and pins on a net, its test points, and the nearest test point to each part. |
| `board_parts_near` | Gives the parts near a part or a point. |
| `board_render` | Draws one side of the board as a PNG, with the parts and nets that you ask for marked. It is a drawing from the file, not a photo. |
| `board_register_photo` | Maps board positions to a phone photo from 4 or more reference parts (homography). |
| `board_locate_in_photo` | Gives the pixel positions of parts or net pins in that photo, and can mark them on it. |

Formats: the formats that OpenBoardView reads. Examples: Test_Link `.brd`, BRD2, `.bdv`, `.bvr`, `.asc`, Samsung `.cad`, GenCAD `.cad`, `.cst`, ASUS `.fz`, ASRock `.cae`, Altium ASCII, and XZZ `.pcb`. OpenBoardView cannot read Allegro binary `.brd` and TVW files.

Limits:

- Many boardview files have no part rotation and no part box. The positions come from the pins.
- `.fz`, `.cae`, and XZZ files are encrypted. You supply the keys in `.env` (`BOARDVIEW_FZ_KEY`, `BOARDVIEW_CAE_KEY`, `BOARDVIEW_XZZ_KEY`). This repository has no keys.
- Boardview files from repair sites are proprietary. Keep them outside this repository. The local test reads one board from `BOARDVIEW_TARGET` and skips when it is not set.

For boards that you designed in KiCad, the design files have better data (exact rotation and part boxes). See the research in [docs/research/boardview-claude.md](docs/research/boardview-claude.md).

## Live reload

`scripts/dev-monitor.sh` uses `--ui-start eager`: the page and the webcam stream start at once, like before the lazy start.

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
