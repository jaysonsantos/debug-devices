"""One JPEG frame from a V4L2 webcam through ffmpeg."""

from datetime import timedelta
from pathlib import Path

from debug_devices_mcp.constants import ffmpeg
from debug_devices_mcp.process import CommandError, CommandRunner

JPEG_MAGIC = b"\xff\xd8"


class WebcamError(Exception):
    """ffmpeg could not grab a frame."""


def ffmpeg_args(ffmpeg_path: str, device: Path, warmup_frames: int) -> list[str]:
    """Skip `warmup_frames` frames (exposure and focus settle), then write one JPEG to stdout."""
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        ffmpeg.LOG_LEVEL,
        "-f",
        ffmpeg.INPUT_FORMAT,
        "-i",
        str(device),
        "-vf",
        rf"select=gte(n\,{warmup_frames})",
        "-fps_mode",
        "passthrough",
        "-frames:v",
        "1",
        "-c:v",
        ffmpeg.CODEC,
        "-q:v",
        str(ffmpeg.QUALITY),
        "-f",
        ffmpeg.OUTPUT_FORMAT,
        ffmpeg.STDOUT,
    ]


class Webcam:
    def __init__(
        self, runner: CommandRunner, ffmpeg_path: str, device: Path, warmup_frames: int, timeout: timedelta
    ) -> None:
        self._runner = runner
        self._ffmpeg_path = ffmpeg_path
        self._device = device
        self._warmup_frames = warmup_frames
        self._timeout = timeout

    @property
    def device(self) -> Path:
        return self._device

    async def capture_jpeg(self) -> bytes:
        args = ffmpeg_args(self._ffmpeg_path, self._device, self._warmup_frames)
        try:
            result = await self._runner.run(args, self._timeout)
        except CommandError as exc:
            raise WebcamError(str(exc)) from exc
        if not result.ok:
            stderr = result.stderr.decode(errors="replace").strip()
            raise WebcamError(f"ffmpeg could not read {self._device} (exit {result.returncode}): {stderr}")
        if not result.stdout.startswith(JPEG_MAGIC):
            raise WebcamError(f"ffmpeg returned no JPEG frame from {self._device}")
        return result.stdout
