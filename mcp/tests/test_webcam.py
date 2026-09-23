from datetime import timedelta
from pathlib import Path

import pytest

from debug_devices_mcp.config import Settings
from debug_devices_mcp.webcam import Crop, Webcam, WebcamError, WebcamOptions

from .conftest import JPEG, FakeRunner, failed, ok


def webcam(runner: FakeRunner, crop: Crop | None = None) -> Webcam:
    options = WebcamOptions(
        ffmpeg_path="ffmpeg", device=Path("/dev/video7"), warmup_frames=5, timeout=timedelta(seconds=1), crop=crop
    )
    return Webcam(runner, options)


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


async def test_crop_runs_in_ffmpeg() -> None:
    runner = FakeRunner(lambda _: ok(JPEG))

    await webcam(runner, Crop.parse("640, 360, 800, 400")).capture_jpeg()

    (command,) = runner.calls
    assert command[command.index("-vf") + 1] == r"select=gte(n\,5),crop=800:400:640:360"


@pytest.mark.parametrize("text", ["1,2,3", "a,b,c,d", "0,0,0,10", "-1,0,10,10"])
def test_bad_crop(text: str) -> None:
    with pytest.raises(ValueError, match=r"crop|validation|invalid"):
        Crop.parse(text)


def test_crop_setting(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.webcam_crop is None
    monkeypatch.setenv("DEBUG_DEVICES_WEBCAM_CROP", "10,20,300,200")
    assert Settings.from_cli([]).webcam_crop == Crop(x=10, y=20, width=300, height=200)
    assert Settings.from_cli(["--webcam-crop", "1,2,3,4"]).webcam_crop == Crop(x=1, y=2, width=3, height=4)
    assert Settings.from_cli(["--webcam-crop", ""]).webcam_crop is None


async def test_crop_can_change_at_run_time() -> None:
    runner = FakeRunner(lambda _: ok(JPEG))
    camera = webcam(runner)

    camera.crop = Crop(x=1, y=2, width=30, height=40)
    await camera.capture_jpeg()
    camera.crop = None
    await camera.capture_jpeg()

    first, second = (command[command.index("-vf") + 1] for command in runner.calls)
    assert first.endswith(",crop=30:40:1:2")
    assert "crop" not in second
