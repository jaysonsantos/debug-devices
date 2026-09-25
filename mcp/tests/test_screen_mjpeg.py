"""The MJPEG fallback of the phone screen: real ffmpeg turns an H.264 test clip into JPEG frames for the page."""

import asyncio
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from debug_devices_mcp.h264 import AccessUnitAssembler, AnnexBParser
from debug_devices_mcp.phone_screen import PhoneScreen, PhoneScreenOptions
from debug_devices_mcp.screen_mjpeg import ScreenTranscoder, transcode_args
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore

from .conftest import FakeRunner, ok

START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
JPEG_START = b"\xff\xd8"
CLIP_SECONDS = "2"
FEED_INTERVAL = 0.05

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def make_clip(tmp_path: Path) -> list:
    """A small Annex B H.264 clip (ffmpeg test source), split into access units as the phone screen does."""
    clip = tmp_path / "clip.h264"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", CLIP_SECONDS, "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-f", "h264", str(clip)],
        check=True,
    )  # fmt: skip
    parser, assembler = AnnexBParser(), AccessUnitAssembler()
    units = [unit for nal in parser.feed(clip.read_bytes()) + parser.flush() if (unit := assembler.feed(nal))]
    if (last := assembler.flush()) is not None:
        units.append(last)
    return units


async def feed_forever(screen: PhoneScreen, units: list) -> None:
    """A phone screen stream that loops the clip."""
    while True:
        for unit in units:
            screen._publish(unit)
            await asyncio.sleep(FEED_INTERVAL)


def test_transcode_args_limit_fps_and_width() -> None:
    args = transcode_args("ffmpeg")
    assert args[args.index("-vf") + 1] == "fps=10,scale='min(1280,iw)':-2"
    assert args[args.index("-i") + 1] == "pipe:0"
    assert args[-1] == "pipe:1"


async def test_fallback_route_sends_jpeg_frames_and_stops_ffmpeg(tmp_path: Path) -> None:
    units = make_clip(tmp_path)
    assert units[0].key
    screen = PhoneScreen(PhoneScreenOptions(adb_path="adb", version="4.1"), FakeRunner(lambda command: ok()))
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(port=0))
    monitor.screen = screen
    monitor.screen_mjpeg = transcoder = ScreenTranscoder(screen, "ffmpeg")
    feeder = asyncio.create_task(feed_forever(screen, units))
    url = await monitor.ensure_page(auto_open=False)
    try:
        assert not transcoder.running  # no ffmpeg before a fallback page watches
        received = b""
        async with httpx.AsyncClient(timeout=10) as http, http.stream("GET", f"{url}api/phone/screen.mjpg") as response:
            assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
            assert transcoder.running
            async for chunk in response.aiter_bytes():
                received += chunk
                if received.count(JPEG_START) >= 2:
                    break
        assert b"Content-Type: image/jpeg" in received
        for _ in range(50):
            if not transcoder.running:
                break
            await asyncio.sleep(0.05)
        assert not transcoder.running  # the last viewer left: ffmpeg stopped
        assert transcoder.viewers == 0
    finally:
        feeder.cancel()
        await monitor.stop()


async def test_fallback_route_is_404_without_phone_screen(tmp_path: Path) -> None:
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(port=0))
    url = await monitor.ensure_page(auto_open=False)
    try:
        async with httpx.AsyncClient(timeout=timedelta(seconds=5).total_seconds()) as http:
            assert (await http.get(f"{url}api/phone/screen.mjpg")).status_code == 404
    finally:
        await monitor.stop()
