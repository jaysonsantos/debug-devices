from datetime import timedelta
from pathlib import Path

import pytest

from debug_devices_mcp.webcam import Webcam, WebcamError

from .conftest import JPEG, FakeRunner, failed, ok


def webcam(runner: FakeRunner) -> Webcam:
    return Webcam(runner, "ffmpeg", Path("/dev/video7"), 5, timedelta(seconds=1))


async def test_capture_skips_warmup_frames() -> None:
    runner = FakeRunner(lambda _: ok(JPEG))

    assert await webcam(runner).capture_jpeg() == JPEG

    (command,) = runner.calls
    assert command[0] == "ffmpeg"
    assert command[command.index("-f") + 1] == "v4l2"
    assert command[command.index("-i") + 1] == "/dev/video7"
    assert command[command.index("-vf") + 1] == r"select=gte(n\,5)"
    assert command[command.index("-frames:v") + 1] == "1"
    assert command[-1] == "pipe:1"


async def test_ffmpeg_failure() -> None:
    runner = FakeRunner(lambda _: failed(b"/dev/video7: No such file or directory"))
    with pytest.raises(WebcamError, match="No such file"):
        await webcam(runner).capture_jpeg()


async def test_not_a_jpeg() -> None:
    with pytest.raises(WebcamError, match="no JPEG"):
        await webcam(FakeRunner(lambda _: ok(b""))).capture_jpeg()
