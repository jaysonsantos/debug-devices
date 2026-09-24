"""A shared webcam capture: one long-running ffmpeg process keeps the latest JPEG frame in memory.

Only one process can read a V4L2 device at a time. While the monitor window is on, this stream owns the webcam:
the page shows its frames, and `webcam_snapshot` and `multimeter_read` take their frame from it.
"""

import asyncio
import contextlib
import io
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from PIL import Image as PilImage
from pydantic import BaseModel

from debug_devices_mcp.constants import images
from debug_devices_mcp.ui.constants import defaults, stream
from debug_devices_mcp.webcam import Crop, WebcamError

logger = logging.getLogger(__name__)


class FrameSource(Protocol):
    """What the webcam tools need. `Webcam` (one ffmpeg run per frame) and `WebcamStream` both fit."""

    @property
    def device(self) -> Path: ...

    @property
    def crop(self) -> Crop | None: ...

    async def capture_jpeg(self) -> bytes: ...


# region: crop


def clamp_crop(crop: Crop, width: int, height: int) -> Crop:
    """Move and shrink the crop so that it fits in a `width` x `height` frame. The result has at least 1 pixel."""
    x = min(crop.x, width - 1)
    y = min(crop.y, height - 1)
    return Crop(x=x, y=y, width=min(crop.width, width - x), height=min(crop.height, height - y))


def crop_jpeg(jpeg: bytes, crop: Crop, quality: int = defaults.CROP_JPEG_QUALITY) -> bytes:
    with PilImage.open(io.BytesIO(jpeg)) as image:
        box = clamp_crop(crop, image.width, image.height)
        cropped = image.crop((box.x, box.y, box.x + box.width, box.y + box.height))
        output = io.BytesIO()
        cropped.convert(images.JPEG_MODE).save(output, format=images.PIL_JPEG_FORMAT, quality=quality)
        return output.getvalue()


def jpeg_size(jpeg: bytes) -> tuple[int, int]:
    """Width and height from the JPEG header. PIL does not decode the pixels for this."""
    with PilImage.open(io.BytesIO(jpeg)) as image:
        return image.size


# endregion: crop

# region: ffmpeg


def stream_args(ffmpeg_path: str, device: Path, fps: int, quality: int) -> list[str]:
    """Read the device with its default format (the same as the one-shot path, so crops match) and write mpjpeg."""
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        stream.LOG_LEVEL,
        "-f",
        stream.INPUT_FORMAT,
        "-i",
        str(device),
        "-vf",
        f"fps={fps}",
        "-c:v",
        stream.CODEC,
        "-q:v",
        str(quality),
        "-f",
        stream.OUTPUT_FORMAT,
        stream.STDOUT,
    ]


async def read_mpjpeg(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield the JPEG parts of an ffmpeg `mpjpeg` stream. Each part has a Content-length header."""
    while True:
        length: int | None = None
        while True:
            line = await reader.readline()
            if not line:
                return
            text = line.strip()
            if not text:
                if length is not None:
                    break
                continue
            name, separator, value = text.partition(stream.HEADER_SEPARATOR)
            if separator and name.strip().lower() == stream.CONTENT_LENGTH_HEADER:
                length = int(value.strip())
        try:
            yield await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            return


class StreamProcess(Protocol):
    @property
    def stdout(self) -> asyncio.StreamReader | None: ...

    @property
    def stderr(self) -> asyncio.StreamReader | None: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


type StreamSpawner = Callable[[Sequence[str]], Awaitable[StreamProcess]]


async def spawn_stream(args: Sequence[str]) -> StreamProcess:
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=stream.READ_LIMIT,
    )


# endregion: ffmpeg


class StreamOptions(BaseModel):
    ffmpeg_path: str
    device: Path
    warmup_frames: int
    timeout: timedelta
    fps: int = defaults.STREAM_FPS
    quality: int = defaults.STREAM_QUALITY
    restart_delay: timedelta = defaults.STREAM_RESTART_DELAY


@dataclass(frozen=True)
class Frame:
    seq: int
    jpeg: bytes
    captured_at: float


class StreamInfo(BaseModel):
    device: str
    running: bool
    width: int | None
    height: int | None
    frames: int
    error: str | None


type CropProvider = Callable[[], Crop | None]


class WebcamStream:
    """Keeps ffmpeg running, restarts it after a failure, and hands out the latest frame."""

    def __init__(
        self,
        options: StreamOptions,
        crop: CropProvider = lambda: None,
        spawner: StreamSpawner = spawn_stream,
    ) -> None:
        self._options = options
        self._crop = crop
        self._spawner = spawner
        self._latest: Frame | None = None
        self._size: tuple[int, int] | None = None
        self._changed = asyncio.Condition()
        self._process: StreamProcess | None = None
        self._task: asyncio.Task[None] | None = None
        self._error: str | None = None
        self._failures = 0

    # region: FrameSource

    @property
    def device(self) -> Path:
        return self._options.device

    @property
    def crop(self) -> Crop | None:
        return self._crop()

    async def capture_jpeg(self) -> bytes:
        """The next frame after this call, cropped. There is no warm-up wait: the stream runs already."""
        frame = await self.next_frame(self._seq(), self._options.timeout)
        crop = self.crop
        if crop is None:
            return frame.jpeg
        try:
            return await asyncio.to_thread(crop_jpeg, frame.jpeg, crop)
        except (OSError, ValueError) as exc:
            raise WebcamError(f"cannot crop the frame from {self.device}: {exc}") from exc

    # endregion: FrameSource

    # region: frames

    @property
    def latest(self) -> Frame | None:
        return self._latest

    def _seq(self) -> int:
        return self._latest.seq if self._latest is not None else 0

    async def next_frame(self, after_seq: int, timeout: timedelta) -> Frame:
        """Wait for a frame newer than `after_seq`. Fail at once when ffmpeg stops before that frame."""
        failures = self._failures

        def is_new() -> bool:
            return self._latest is not None and self._latest.seq > after_seq

        try:
            async with self._changed:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: is_new() or self._failures > failures), timeout.total_seconds()
                )
        except TimeoutError as exc:
            reason = f": {self._error}" if self._error else ""
            raise WebcamError(f"no frame from {self.device} after {timeout}{reason}") from exc
        if not is_new():
            raise WebcamError(f"no frame from {self.device}: {self._error}")
        assert self._latest is not None
        return self._latest

    async def frames(self, timeout: timedelta) -> AsyncIterator[Frame]:
        """Every new frame, for the MJPEG page stream. Waits through restarts."""
        seq = self._seq()
        while True:
            try:
                frame = await self.next_frame(seq, timeout)
            except WebcamError:
                continue
            seq = frame.seq
            yield frame

    async def _publish(self, jpeg: bytes) -> None:
        if self._size is None:
            try:
                self._size = jpeg_size(jpeg)
            except OSError as exc:
                logger.warning("webcam frame has no readable JPEG header: %s", exc)
        async with self._changed:
            self._latest = Frame(seq=self._seq() + 1, jpeg=jpeg, captured_at=time.monotonic())
            self._error = None
            self._changed.notify_all()

    def info(self) -> StreamInfo:
        width, height = self._size if self._size is not None else (None, None)
        return StreamInfo(
            device=str(self.device),
            running=self._process is not None and self._process.returncode is None,
            width=width,
            height=height,
            frames=self._seq(),
            error=self._error,
        )

    # endregion: frames

    # region: process

    @property
    def timeout(self) -> timedelta:
        return self._options.timeout

    @property
    def active(self) -> bool:
        """True between `start()` and `stop()`, also while ffmpeg restarts."""
        return self._task is not None and not self._task.done()

    def set_crop_provider(self, crop: CropProvider) -> None:
        self._crop = crop

    def set_warmup_frames(self, warmup_frames: int) -> None:
        """Takes effect at the next ffmpeg start. `restart()` starts it again now."""
        self._options = self._options.model_copy(update={"warmup_frames": warmup_frames})

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="webcam-stream")

    def restart(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        try:
            while True:
                await self._run_once()
                await asyncio.sleep(self._options.restart_delay.total_seconds())
        finally:
            await self._kill()

    async def _run_once(self) -> None:
        options = self._options
        args = stream_args(options.ffmpeg_path, options.device, options.fps, options.quality)
        try:
            process = await self._spawner(args)
        except OSError as exc:
            await self._fail(f"cannot start {options.ffmpeg_path}: {exc}")
            return
        self._process = process
        assert process.stdout is not None
        skipped = 0
        async for jpeg in read_mpjpeg(process.stdout):
            if skipped < options.warmup_frames:
                skipped += 1
                continue
            await self._publish(jpeg)
        stderr = await process.stderr.read() if process.stderr is not None else b""
        returncode = await process.wait()
        self._process = None
        message = stderr.decode(errors="replace").strip()
        await self._fail(f"ffmpeg stopped (exit {returncode}){': ' + message if message else ''}")

    async def _fail(self, error: str) -> None:
        """Record the error and wake the waiting callers, so a busy device fails fast."""
        logger.warning("webcam stream: %s", error)
        async with self._changed:
            self._error = error
            self._failures += 1
            self._changed.notify_all()

    async def _kill(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), defaults.PROCESS_STOP_TIMEOUT.total_seconds())
        except TimeoutError:
            process.kill()
            await process.wait()

    # endregion: process
