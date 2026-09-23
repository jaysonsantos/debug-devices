#!/usr/bin/env python3
"""Record the monitor page for the README: a Firefox video, then an animated WebP and a short MP4.

Firefox is necessary: the phone screen uses WebCodecs H.264. Run it from the repo root:

    uv run --with playwright playwright install firefox   # once
    uv run --with playwright python scripts/record_demo.py --fake-phone docs/images/demo-meter.jpg

With `--fake-phone`, the script starts scripts/fake_phone.py with that photo as the snapshot and a separate demo
MCP server (fake adb, its own state folder, no phone screen). The demo server asks the monitor on port 18766 for
the webcam, so a monitor must run there. Without `--fake-phone`, the script records the page at `--url` and uses
the real phone of that monitor.

PRIVACY: the webcam can see more than the multimeter. Outside the crop box, the recorder darkens and blurs the
view. It hides the webcam while there is no crop box, and it hides the log rows from before the recording (their
images can be uncropped). Check the frames before you publish the result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from playwright.sync_api import Page, sync_playwright

# region: constants

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_PHONE = REPO_ROOT / "scripts" / "fake_phone.py"
FAKE_ADB = REPO_ROOT / "scripts" / "fake_adb.py"
FAKE_SERIAL = "fake-phone-0001"
DEFAULT_URL = "http://127.0.0.1:18766/"
DEFAULT_WEBP = REPO_ROOT / "docs" / "images" / "monitor-demo.webp"
DEFAULT_MP4 = REPO_ROOT / "docs" / "images" / "monitor-demo.mp4"
# The demo server finds the webcam owner on this port and binds a free port for its own page.
OWNER_UI_PORT = "18766"
DEMO_FORWARD_PORT = "18865"
# The multimeter in the 1920x1080 webcam frame (x, y, width, height). The demo page shows only this part sharp.
DEFAULT_DEMO_CROP = "870,60,470,800"
CROP_PARTS = ("x", "y", "width", "height")
SETTINGS_FILE = Path("debug-devices") / "ui-settings.json"
MONITOR_URL_PATTERN = re.compile(r"monitor window: (\S+)")
SERVER_START_TIMEOUT = 90.0
SERVER_STOP_TIMEOUT = 15.0

VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900
OUTPUT_FPS = 10
OUTPUT_WIDTH = 1280
WEBP_QUALITY = 60
MP4_CRF = 30
# Keep the MP4 only when it is at most this size.
MAX_MP4_BYTES = 4 * 2**20
MS_PER_SECOND = 1000

ZOOM_STEPS = 3
PAUSE_SHORT = 0.8
PAUSE = 1.5
PAUSE_LONG = 3.0
HOLD_READING = 5.0
PHONE_TIMEOUT = 30.0
MULTIMETER_TIMEOUT = 120.0
MULTIMETER_ATTEMPTS = 2
SCROLL_STEP_PIXELS = 120
SCROLL_STEP_PAUSE = 0.05
SCROLL_TOP_MARGIN = 0.1

# The recorder-only privacy mask. The product page has none of this.
# Outside the crop box: 82% black from the crop box shadow, over a blur layer with a hole at the crop box.
PRIVACY_CSS = """
#crop { box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.82) !important; }
#stage:has(#crop[hidden]) #webcam { visibility: hidden !important; }
#demo-blur { position: absolute; inset: 0; pointer-events: none; backdrop-filter: blur(18px); }
#log li:not([data-demo]) { display: none !important; }
"""
# Runs before the page scripts, so the mask is in place before the first paint.
# Rows that the page adds after `window.__demoStart = true` get `data-demo`, so they stay visible.
INIT_JS = """
const style = document.createElement("style");
style.textContent = PRIVACY_CSS;
document.documentElement.appendChild(style);
window.__demoStart = false;
new MutationObserver((records) => {
  if (!window.__demoStart) return;
  for (const record of records) {
    for (const node of record.addedNodes) {
      if (node.nodeType === 1 && node.matches("li.call")) node.dataset.demo = "1";
    }
  }
}).observe(document, { childList: true, subtree: true });
document.addEventListener("DOMContentLoaded", () => {
  const crop = document.getElementById("crop");
  const blur = document.createElement("div");
  blur.id = "demo-blur";
  crop.before(blur);
  const follow = () => {
    const s = crop.style;
    const left = parseFloat(s.left) || 0, top = parseFloat(s.top) || 0;
    const right = left + (parseFloat(s.width) || 0), bottom = top + (parseFloat(s.height) || 0);
    blur.style.clipPath = crop.hidden ? "none" :
      `polygon(evenodd, 0% 0%, 100% 0%, 100% 100%, 0% 100%, 0% 0%, ` +
      `${left}% ${top}%, ${right}% ${top}%, ${right}% ${bottom}%, ${left}% ${bottom}%, ${left}% ${top}%)`;
    requestAnimationFrame(follow);
  };
  follow();
});
""".replace("PRIVACY_CSS", json.dumps(PRIVACY_CSS))

# endregion: constants


@dataclass(frozen=True)
class Options:
    url: str
    webp: Path
    mp4: Path
    headed: bool
    fake_phone: Path | None
    demo_crop: str


def pause(seconds: float) -> None:
    time.sleep(seconds)


# region: demo servers


def crop_setting(text: str) -> dict[str, int]:
    return dict(zip(CROP_PARTS, (int(part) for part in text.split(",")), strict=True))


def drain(stream: IO[str]) -> None:
    """Read a pipe until it closes, so the child never blocks on a full pipe."""
    for _ in iter(stream.readline, ""):
        pass


def wait_for_url(server: subprocess.Popen[str]) -> str:
    assert server.stderr is not None
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        line = server.stderr.readline()
        if not line:
            raise RuntimeError("the demo MCP server stopped before it served the page")
        if match := MONITOR_URL_PATTERN.search(line):
            threading.Thread(target=drain, args=(server.stderr,), daemon=True).start()
            return match.group(1)
    raise RuntimeError("the demo MCP server did not serve the page in time")


@contextmanager
def demo_servers(snapshot: Path, demo_crop: str) -> Iterator[str]:
    """Start the fake phone and a demo MCP server. Yield the URL of the demo page."""
    with tempfile.TemporaryDirectory() as state:
        settings = Path(state) / SETTINGS_FILE
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"webcam_crop": crop_setting(demo_crop)}))
        phone = subprocess.Popen(
            ["python3", str(FAKE_PHONE), "--port", DEMO_FORWARD_PORT, "--snapshot", str(snapshot.resolve())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        environ = os.environ | {"XDG_STATE_HOME": state, "DEBUG_DEVICES_ADB_PATH": str(FAKE_ADB)}
        command = [
            "uv", "run", "--directory", str(REPO_ROOT), "debug-devices-mcp",
            "--ui-port", OWNER_UI_PORT, "--no-ui-open-browser", "--no-phone-screen",
            "--adb-serial", FAKE_SERIAL, "--local-forward-port", DEMO_FORWARD_PORT,
        ]  # fmt: skip
        # Stdin stays open: the stdio server stops when it closes.
        server = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environ, text=True
        )
        try:
            yield wait_for_url(server)
        finally:
            assert server.stdin is not None
            server.stdin.close()
            try:
                server.wait(SERVER_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                server.terminate()
            phone.terminate()
            phone.wait()


# endregion: demo servers

# region: scenario


def smooth_scroll_to(page: Page, selector: str) -> None:
    """Scroll in small wheel steps, so the video shows the movement."""
    target = page.locator(selector).bounding_box()
    if target is None:
        return
    distance = target["y"] - VIEWPORT_HEIGHT * SCROLL_TOP_MARGIN
    direction = 1 if distance > 0 else -1
    page.mouse.move(VIEWPORT_WIDTH / 2, VIEWPORT_HEIGHT / 2)
    for _ in range(int(abs(distance) // SCROLL_STEP_PIXELS)):
        page.mouse.wheel(0, direction * SCROLL_STEP_PIXELS)
        pause(SCROLL_STEP_PAUSE)


def scroll_top(page: Page) -> None:
    for _ in range(int(page.evaluate("window.scrollY") // SCROLL_STEP_PIXELS) + 1):
        page.mouse.wheel(0, -SCROLL_STEP_PIXELS)
        pause(SCROLL_STEP_PAUSE)


def read_multimeter(page: Page, source: str) -> None:
    """Click "Read multimeter" and wait for the answer. Try one more time after an error."""
    page.select_option("#multimeter-source", source)
    for _ in range(MULTIMETER_ATTEMPTS):
        page.click("#multimeter-read")
        page.wait_for_function(
            "() => !['', 'reading…'].includes(document.getElementById('multimeter-result').textContent)"
            " || !document.getElementById('multimeter-error').hidden",
            timeout=MULTIMETER_TIMEOUT * MS_PER_SECOND,
        )
        if page.locator("#multimeter-error").is_hidden():
            return
        pause(PAUSE)


def run_scenario(page: Page, url: str, fake_phone: bool) -> None:
    page.goto(url)
    page.locator("#link.ok").wait_for()
    page.evaluate("window.__demoStart = true")
    pause(PAUSE_LONG)

    page.click("#phone-connect")
    if fake_phone:
        page.wait_for_function(f"() => document.getElementById('phone-serial').textContent === '{FAKE_SERIAL}'")
    else:
        page.locator("#phone-screen:not([hidden])").wait_for(timeout=PHONE_TIMEOUT * MS_PER_SECOND)
    pause(PAUSE_LONG)

    for _ in range(ZOOM_STEPS):
        page.click("#zoom-in")
        pause(PAUSE)
    for _ in range(ZOOM_STEPS):
        page.click("#zoom-out")
        pause(PAUSE_SHORT)

    page.check("#torch")
    pause(PAUSE_LONG)
    page.uncheck("#torch")
    pause(PAUSE)

    if fake_phone:
        page.click("#phone-snapshot")
        page.locator("#snapshot-link:not([hidden])").wait_for(timeout=PHONE_TIMEOUT * MS_PER_SECOND)
        pause(PAUSE_LONG)

    smooth_scroll_to(page, "#log-panel")
    pause(PAUSE)
    scroll_top(page)
    pause(PAUSE_SHORT)

    read_multimeter(page, "phone" if fake_phone else "webcam")
    pause(HOLD_READING)
    smooth_scroll_to(page, "#log-panel")
    pause(HOLD_READING)
    scroll_top(page)
    pause(PAUSE)


def record(options: Options, url: str, video_dir: Path) -> Path:
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch(headless=not options.headed)
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            record_video_dir=str(video_dir),
            record_video_size={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            color_scheme="dark",
        )
        context.add_init_script(INIT_JS)
        page = context.new_page()
        try:
            run_scenario(page, url, options.fake_phone is not None)
        finally:
            video = page.video
            context.close()
            browser.close()
        assert video is not None
        return Path(video.path())


# endregion: scenario

# region: convert


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def convert(video: Path, options: Options) -> None:
    scale = f"fps={OUTPUT_FPS},scale='min({OUTPUT_WIDTH},iw)':-2:flags=lanczos"
    options.webp.parent.mkdir(parents=True, exist_ok=True)
    # libwebp_anim stores only the changed parts between frames: much smaller than libwebp.
    ffmpeg(
        "-i", str(video), "-vf", scale, "-an", "-c:v", "libwebp_anim", "-lossless", "0",
        "-quality", str(WEBP_QUALITY), "-compression_level", "6", "-loop", "0", str(options.webp),
    )  # fmt: skip
    ffmpeg(
        "-i", str(video), "-vf", scale, "-an", "-c:v", "libx264", "-crf", str(MP4_CRF),
        "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(options.mp4),
    )  # fmt: skip
    if options.mp4.stat().st_size > MAX_MP4_BYTES:
        options.mp4.unlink()
        print(f"MP4 above {MAX_MP4_BYTES} bytes: removed")


# endregion: convert


def parse_args() -> tuple[Options, Path | None]:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help="page to record without --fake-phone")
    parser.add_argument("--fake-phone", type=Path, help="start a fake phone with this snapshot and a demo server")
    parser.add_argument("--demo-crop", default=DEFAULT_DEMO_CROP, help="x,y,w,h crop of the demo page")
    parser.add_argument("--webp", type=Path, default=DEFAULT_WEBP)
    parser.add_argument("--mp4", type=Path, default=DEFAULT_MP4)
    parser.add_argument("--keep-video", type=Path, help="also copy the raw Playwright video here")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()
    options = Options(
        url=args.url,
        webp=args.webp,
        mp4=args.mp4,
        headed=args.headed,
        fake_phone=args.fake_phone,
        demo_crop=args.demo_crop,
    )
    return options, args.keep_video


def main() -> None:
    options, keep_video = parse_args()
    with tempfile.TemporaryDirectory() as directory:
        if options.fake_phone is not None:
            with demo_servers(options.fake_phone, options.demo_crop) as url:
                print(f"demo page: {url}")
                video = record(options, url, Path(directory))
        else:
            video = record(options, options.url, Path(directory))
        if keep_video is not None:
            shutil.copy(video, keep_video)
        convert(video, options)
    for path in (options.webp, options.mp4):
        if path.exists():
            print(f"{path}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
