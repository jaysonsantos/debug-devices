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
  - See [mcp/README.md](mcp/README.md).
- `docs/`: the API contract, the research notes, the QA guide, and the agent reports.
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

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```
