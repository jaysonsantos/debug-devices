"""The phone screen as MJPEG, for browsers that cannot decode H.264 with WebCodecs (for example Camoufox).

One ffmpeg turns the H.264 access units of the phone screen into JPEG frames. It runs only while at least one
fallback viewer watches, and it stops after the last one leaves.
"""

import asyncio
import contextlib
import functools
import logging
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from debug_devices_mcp.phone_screen import MessageKind, PhoneScreen
from debug_devices_mcp.ui.constants import defaults, stream
from debug_devices_mcp.webcam_stream import StreamProcess, read_mpjpeg

logger = logging.getLogger(__name__)

FALLBACK_FPS = 10
FALLBACK_MAX_WIDTH = 1280
# ffmpeg -q:v for MJPEG: 2 is best, 31 worst. 5 keeps small text readable at a moderate size.
FALLBACK_QUALITY = 5
# The wire header of a phone screen message: 4-byte big-endian length, 1 kind byte.
HEADER = struct.Struct(">IB")
PICTURE_KINDS = frozenset({MessageKind.KEY, MessageKind.DELTA})
FRAME_WAIT = timedelta(seconds=5)
H264_FORMAT = "h264"
STDIN = "pipe:0"


@dataclass(frozen=True)
class TranscodeOptions:
    fps: int = FALLBACK_FPS
    max_width: int = FALLBACK_MAX_WIDTH
    quality: int = FALLBACK_QUALITY


FALLBACK_OPTIONS = TranscodeOptions()


class PipedProcess(StreamProcess, Protocol):
    """A child with stdin: ffmpeg reads the H.264 stream there."""

    @property
    def stdin(self) -> asyncio.StreamWriter | None: ...


type PipedSpawner = Callable[[Sequence[str]], Awaitable[PipedProcess]]


def transcode_args(ffmpeg_path: str, options: TranscodeOptions = FALLBACK_OPTIONS) -> list[str]:
    scale = f"fps={options.fps},scale='min({options.max_width},iw)':-2"
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        stream.LOG_LEVEL,
        # Show each picture at once: no input buffering for a live stream.
        "-fflags",
        "nobuffer",
        "-f",
        H264_FORMAT,
        "-i",
        STDIN,
        "-vf",
        scale,
        "-c:v",
        stream.CODEC,
        "-q:v",
        str(options.quality),
        "-f",
        stream.OUTPUT_FORMAT,
        stream.STDOUT,
    ]


async def spawn_piped(args: Sequence[str]) -> PipedProcess:
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


@dataclass
class Frame:
    seq: int
    jpeg: bytes


class ScreenTranscoder:
    def __init__(
        self,
        screen: PhoneScreen,
        ffmpeg_path: str,
        spawner: PipedSpawner = spawn_piped,
        options: TranscodeOptions = FALLBACK_OPTIONS,
    ) -> None:
        self._screen = screen
        self._ffmpeg_path = ffmpeg_path
        self._options = options
        self._spawner = spawner
        self._viewers = 0
        self._task: asyncio.Task[None] | None = None
        self._latest: Frame | None = None
        self._changed = asyncio.Condition()
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def viewers(self) -> int:
        return self._viewers

    @contextlib.asynccontextmanager
    async def viewer(self) -> AsyncIterator[None]:
        """A fallback page watches: start ffmpeg for the first one, stop it after the last one."""
        async with self._lock:
            self._viewers += 1
            if not self.running:
                self._task = asyncio.create_task(self._run(), name="phone-screen-mjpeg")
        try:
            yield
        finally:
            async with self._lock:
                self._viewers -= 1
                if self._viewers == 0:
                    await self.stop()

    async def frames(self) -> AsyncIterator[bytes]:
        seq = self._latest.seq if self._latest is not None else 0
        while True:
            async with self._changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._changed.wait_for(functools.partial(self._newer_than, seq)),
                        FRAME_WAIT.total_seconds(),
                    )
            if self._latest is not None and self._latest.seq > seq:
                seq = self._latest.seq
                yield self._latest.jpeg

    def _newer_than(self, seq: int) -> bool:
        return self._latest is not None and self._latest.seq > seq

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        process = await self._spawner(transcode_args(self._ffmpeg_path, self._options))
        feeder = asyncio.create_task(self._feed(process), name="phone-screen-mjpeg-feed")
        try:
            assert process.stdout is not None
            async for jpeg in read_mpjpeg(process.stdout):
                async with self._changed:
                    self._latest = Frame(seq=(self._latest.seq if self._latest else 0) + 1, jpeg=jpeg)
                    self._changed.notify_all()
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder
            if process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), defaults.PROCESS_STOP_TIMEOUT.total_seconds())

    async def _feed(self, process: PipedProcess) -> None:
        """Write the pictures of the phone screen to ffmpeg. The subscription starts at a key frame."""
        stdin = process.stdin
        assert stdin is not None
        try:
            with self._screen.subscribe() as subscriber:
                while True:
                    if subscriber.resync:
                        self._screen.resync(subscriber)
                    message = await subscriber.queue.get()
                    _, kind = HEADER.unpack_from(message)
                    if kind in PICTURE_KINDS:
                        stdin.write(message[HEADER.size :])
                        await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.info("the phone screen MJPEG ffmpeg stopped reading: %s", exc)
        finally:
            with contextlib.suppress(OSError):
                stdin.close()
