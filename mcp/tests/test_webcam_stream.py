import asyncio
import io
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from debug_devices_mcp.webcam import Crop, WebcamError
from debug_devices_mcp.webcam_stream import (
    StreamOptions,
    WebcamStream,
    clamp_crop,
    crop_jpeg,
    read_mpjpeg,
    stream_args,
)

from .conftest import make_jpeg


def mpjpeg(*parts: bytes) -> bytes:
    return b"".join(
        b"--ffmpeg\r\nContent-type: image/jpeg\r\nContent-length: %d\r\n\r\n%s\r\n" % (len(part), part)
        for part in parts
    )


def reader_with(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


class FakeStreamProcess:
    def __init__(self, data: bytes, eof: bool = True) -> None:
        self.stdout = reader_with(data, eof)
        self.stderr = reader_with(b"device busy")
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = -15
        self.stdout.feed_eof()

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 1
        return self.returncode


def options(**changes: object) -> StreamOptions:
    base = StreamOptions(
        ffmpeg_path="ffmpeg",
        device=Path("/dev/video0"),
        warmup_frames=0,
        timeout=timedelta(seconds=1),
        restart_delay=timedelta(milliseconds=10),
    )
    return base.model_copy(update=changes)


# region: crop


def test_clamp_crop_keeps_the_crop_in_the_frame() -> None:
    assert clamp_crop(Crop(x=10, y=10, width=20, height=20), 100, 100) == Crop(x=10, y=10, width=20, height=20)
    assert clamp_crop(Crop(x=90, y=95, width=20, height=20), 100, 100) == Crop(x=90, y=95, width=10, height=5)
    assert clamp_crop(Crop(x=500, y=500, width=20, height=20), 100, 100) == Crop(x=99, y=99, width=1, height=1)


def test_crop_jpeg_cuts_the_rectangle() -> None:
    cropped = crop_jpeg(make_jpeg(64, 48), Crop(x=8, y=4, width=16, height=10))
    with Image.open(io.BytesIO(cropped)) as image:
        assert image.size == (16, 10)


# endregion: crop


def test_stream_args() -> None:
    args = stream_args("ffmpeg", Path("/dev/video0"), 10, 2)
    assert args[args.index("-i") + 1] == "/dev/video0"
    assert args[args.index("-vf") + 1] == "fps=10"
    assert args[args.index("-f", args.index("-i")) + 1] == "mpjpeg"
    assert args[-1] == "pipe:1"


async def test_read_mpjpeg_splits_parts_by_length() -> None:
    # A part that contains a line break and a boundary-like text must stay whole.
    tricky = b"\xff\xd8\r\n--ffmpeg\r\n\r\n\xff\xd9"
    parts = [part async for part in read_mpjpeg(reader_with(mpjpeg(b"\xff\xd8one\xff\xd9", tricky)))]
    assert parts == [b"\xff\xd8one\xff\xd9", tricky]


async def test_read_mpjpeg_stops_at_a_cut_part() -> None:
    data = mpjpeg(b"whole") + b"--ffmpeg\r\nContent-length: 100\r\n\r\nshort"
    assert [part async for part in read_mpjpeg(reader_with(data))] == [b"whole"]


async def test_stream_skips_warmup_and_crops_frames() -> None:
    frames = [make_jpeg(64, 48) for _ in range(3)]
    process = FakeStreamProcess(mpjpeg(*frames), eof=False)

    async def spawner(args):
        return process

    stream = WebcamStream(options(warmup_frames=2), crop=lambda: Crop(x=0, y=0, width=8, height=8), spawner=spawner)
    stream.start()
    try:
        frame = await stream.next_frame(0, timedelta(seconds=1))
        assert frame.seq == 1  # the two warm-up frames were skipped
        info = stream.info()
        assert (info.width, info.height, info.running) == (64, 48, True)
        process.stdout.feed_data(mpjpeg(make_jpeg(64, 48)))
        jpeg = await stream.capture_jpeg()
        with Image.open(io.BytesIO(jpeg)) as image:
            assert image.size == (8, 8)
    finally:
        await stream.stop()
    assert process.returncode == -15


async def test_stream_reports_ffmpeg_errors_and_restarts() -> None:
    spawned: list[FakeStreamProcess] = []

    async def spawner(args):
        process = FakeStreamProcess(b"")
        spawned.append(process)
        return process

    stream = WebcamStream(options(timeout=timedelta(seconds=5)), spawner=spawner)
    stream.start()
    try:
        with pytest.raises(WebcamError, match="device busy"):
            await stream.capture_jpeg()
        assert len(spawned) == 1  # the error comes at once, not after the timeout
        for _ in range(100):
            if len(spawned) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(spawned) >= 2
    finally:
        await stream.stop()


async def test_missing_ffmpeg_is_an_error_message() -> None:
    async def spawner(args):
        raise FileNotFoundError("ffmpeg")

    stream = WebcamStream(options(timeout=timedelta(milliseconds=50)), spawner=spawner)
    stream.start()
    try:
        with pytest.raises(WebcamError, match="cannot start ffmpeg"):
            await stream.capture_jpeg()
    finally:
        await stream.stop()
