"""One JPEG frame from a V4L2 webcam through ffmpeg."""

from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from debug_devices_mcp.constants import ffmpeg
from debug_devices_mcp.process import CommandError, CommandRunner

JPEG_MAGIC = b"\xff\xd8"


CROP_SEPARATOR = ","
CROP_PARTS = 4


class Crop(BaseModel):
    """A rectangle of the webcam frame in pixels: top-left corner, width, and height."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @classmethod
    def parse(cls, text: str) -> Crop:
        """Parse `x,y,w,h`, for example `640,360,800,400`."""
        parts = [part.strip() for part in text.split(CROP_SEPARATOR)]
        if len(parts) != CROP_PARTS:
            raise ValueError(f"crop must be x,y,w,h (4 integers), not {text!r}")
        x, y, width, height = (int(part) for part in parts)
        return cls(x=x, y=y, width=width, height=height)

    def ffmpeg_filter(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


class WebcamError(Exception):
    """ffmpeg could not grab a frame."""


def ffmpeg_args(ffmpeg_path: str, device: Path, warmup_frames: int, crop: Crop | None = None) -> list[str]:
    """Skip `warmup_frames` frames (exposure and focus settle), crop, then write one JPEG to stdout."""
    filters = [rf"select=gte(n\,{warmup_frames})"]
    if crop is not None:
        filters.append(crop.ffmpeg_filter())
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
        ",".join(filters),
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


class WebcamOptions(BaseModel):
    ffmpeg_path: str
    device: Path
    warmup_frames: int
    timeout: timedelta
    crop: Crop | None = None


class Webcam:
    def __init__(self, runner: CommandRunner, options: WebcamOptions) -> None:
        self._runner = runner
        self._options = options

    @property
    def device(self) -> Path:
        return self._options.device

    @property
    def crop(self) -> Crop | None:
        return self._options.crop

    @crop.setter
    def crop(self, crop: Crop | None) -> None:
        """Change the crop at run time (for example from a monitor UI). The next frame uses it."""
        self._options = self._options.model_copy(update={"crop": crop})

    async def capture_jpeg(self) -> bytes:
        options = self._options
        args = ffmpeg_args(options.ffmpeg_path, options.device, options.warmup_frames, options.crop)
        try:
            result = await self._runner.run(args, options.timeout)
        except CommandError as exc:
            raise WebcamError(str(exc)) from exc
        if not result.ok:
            stderr = result.stderr.decode(errors="replace").strip()
            raise WebcamError(f"ffmpeg could not read {options.device} (exit {result.returncode}): {stderr}")
        if not result.stdout.startswith(JPEG_MAGIC):
            raise WebcamError(f"ffmpeg returned no JPEG frame from {options.device}")
        return result.stdout
