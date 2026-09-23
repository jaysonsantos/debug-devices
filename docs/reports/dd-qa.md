# dd-qa report (phase 1)

## What I did

- Probed the webcam and captured one frame.
- Wrote `scripts/fake_phone.py`: a fake phone that implements `docs/phone-api.md`, with a self-check.
- Wrote `scripts/qa_contract.py`: contract checks against any base URL.
- Wrote `scripts/fake_adb.py`: a fake `adb` so that the MCP can reach the fake phone. It never calls the real adb.
- Wrote `scripts/multimeter_capture.sh`: captures frames and a CSV for the accuracy check.
- Wrote `docs/qa.md`: the test plan.

I did not edit `android/` or `mcp/`. I did not commit.

## Hardware probe

- `/dev/video0`: `PC-LM1E` (uvcvideo), USB bus `usb-0000:06:00.3-6.4`. `/dev/video1` is its metadata node.
- Formats (`v4l2-ctl -d /dev/video0 --list-formats-ext`):
  - MJPEG: 1920x1080 at 30/25/20/15/10/5 fps, and 1280x1024, 1280x720, 1024x576, 960x720, 864x480, 640x480, 640x360, 352x288, 320x240 at 30 fps.
  - YUYV: 1920x1080 and 1280x1024 at 5 fps only, 640x480 at 30 fps.
- Capture that works (1.4 s, 86 KB):

  ```sh
  ffmpeg -hide_banner -loglevel error -y -f v4l2 -input_format mjpeg -video_size 1920x1080 \
    -i /dev/video0 -vf "select=gte(n\,10)" -frames:v 1 /tmp/dd-qa/frame_1080p.jpg
  ```

- Recommendation: MJPEG 1920x1080, and skip about 10 frames for the exposure to settle.
- The webcam points at a PROSTER T21D multimeter. The meter is in the lower half of the frame, at a low and oblique angle.
- The LCD is blank. The meter is off, or the selector is at OFF. The LCD fills about 12% of the frame width.
- The frame also shows other objects: photos of people and a medicine label. The multimeter tool sends the full frame to OpenRouter. See the open items.

## What works

| Command | Result |
|---|---|
| `python3 scripts/fake_phone.py --self-check` | `self-check PASSED`: 5 modes (default, fixed zoom, no flash, not ready, capture fails), all checks pass |
| `python3 scripts/fake_phone.py --port 18765 --snapshot /tmp/dd-qa/frame_1080p.jpg` then `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18765 --strict` | 11/11 checks pass. The snapshot check reads 1920x1080. |
| `python3 scripts/qa_contract.py --base-url http://127.0.0.1:18766 --expect not-ready` (fake phone with `--not-ready`) | 2/2 checks pass |
| `python3 scripts/qa_contract.py` with no server | 0/11, each check reports `Connection refused`, exit code 1, no traceback |
| `scripts/fake_adb.py -s 192.168.0.43:5555 shell input tap 1 1` | `error: device '192.168.0.43:5555' not found`, exit 1 |
| `uvx ruff format scripts/ && uvx ruff check scripts/` | Clean, with the repo `pyproject.toml` settings |
| `nix run nixpkgs#shellcheck -- scripts/multimeter_capture.sh` | Clean |
| `printf '\n\n' \| scripts/multimeter_capture.sh /tmp/dd-qa/acc 2` | 2 frames and `readings.csv` |

## Open items

### Contract gaps (proposals for `docs/phone-api.md`)

1. The contract does not say what happens when a zoom body has both `ratio` and `step`. I propose 400 `bad_request`. `qa_contract.py --strict` checks this.
2. The contract does not say what a wrong method on a known path returns (for example `GET /v1/zoom`). 405 is not in the error list. I propose 404 `not_found`, or add `method_not_allowed` (405).
3. The contract does not say that `/v1/health` returns 200 while the camera is not bound. The fake phone and `--expect not-ready` assume 200.
4. The contract does not say the state after an app restart. I propose: torch off, zoom at min.
5. The contract does not say what the torch does after a snapshot. Some phones turn the torch off after a still capture with flash. I propose that the torch state stays the same, and that the snapshot does not use the flash.

### For dd-mcp

- The MCP always calls adb. It has no flag for a direct phone URL. `scripts/fake_adb.py` solves this for tests: set `DEBUG_DEVICES_ADB_PATH=scripts/fake_adb.py` and run the fake phone on port `18765`.
- The multimeter tool sends the full webcam frame to OpenRouter. The current frame shows photos of people and a medicine label. I propose an optional crop (`--webcam-crop x,y,w,h`) to send only the meter.
- The `select_device` rule (refuse when several devices are connected and no serial is set) is correct and keeps the Fire TVs safe. Keep it.

### Target phone (update from the orchestrator)

- The target phone is adb serial `7fad170e`, model `2510ERA8BG`. Use only `adb -s 7fad170e`. The Fire TV rule stays.
- `adb -s 7fad170e get-state` returns `device`. `getprop ro.product.model` returns `2510ERA8BG`.
- `pm list packages dev.jayson.debugdevices` returns nothing. The app is not installed yet. Thus the real-phone contract test (`docs/qa.md` 1.3) did not run.
- I did not install the app. dd-android or the orchestrator owns the APK install.

### Vision model (update from the orchestrator)

- The default vision model is now `openai/gpt-6-luna`. It accepts image input and `response_format` `json_schema`.
- `docs/qa.md` now has MCP test cases for the default model id and for the `json_schema` request shape.
- No file that I own had the old model id `deepseek/deepseek-v4.1-flash`.

### For the user

- Turn on the multimeter, and move the webcam so that the LCD faces the camera and fills more of the frame. Then the accuracy check in `docs/qa.md` section 4 can start.
- `origin` is not set in this repository. I could not make sure that the branch is current with `origin/main`.

### Phase 2 (not started)

- MCP tool tests over stdio against the fake phone and the webcam.
- `./gradlew` tests.
- Review of both sides against the contract, with bugs as file:line.
