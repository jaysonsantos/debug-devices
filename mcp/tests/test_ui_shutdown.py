"""The page stops fast and with no uvicorn error, also while pages hold the SSE and MJPEG streams open."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from debug_devices_mcp.ui.monitor import GRACEFUL_SHUTDOWN_SECONDS, Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.routes import until_closing
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore
from debug_devices_mcp.webcam_stream import StreamOptions, WebcamStream

from .test_webcam_stream import FakeStreamProcess

START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
# Well below the graceful time of uvicorn: the streams end at once, uvicorn never has to cancel them.
FAST_STOP_SECONDS = GRACEFUL_SHUTDOWN_SECONDS / 2


async def test_until_closing_ends_a_waiting_stream_and_closes_it() -> None:
    closing = asyncio.Event()
    closed: list[bool] = []

    async def forever() -> AsyncGenerator[int]:
        try:
            yield 1
            await asyncio.Event().wait()
            yield 2
        finally:
            closed.append(True)

    items = []

    async def read() -> None:
        async for item in until_closing(forever(), closing):
            items.append(item)

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.05)
    closing.set()
    await asyncio.wait_for(reader, 1)
    assert items == [1]
    assert closed == [True]


async def test_until_closing_passes_a_finite_stream_through() -> None:
    async def three() -> AsyncGenerator[int]:
        for item in range(3):
            yield item

    assert [item async for item in until_closing(three(), asyncio.Event())] == [0, 1, 2]


async def read_some(url: str, opened: asyncio.Event) -> None:
    """A page that keeps a stream open: it reads until the server ends the response."""
    async with httpx.AsyncClient(timeout=None) as http, http.stream("GET", url) as response:
        opened.set()
        async for _ in response.aiter_raw():
            pass


async def test_stop_page_ends_open_streams_without_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def silent_camera(args: object) -> FakeStreamProcess:
        return FakeStreamProcess(b"", eof=False)  # a webcam that never sends a frame

    stream = WebcamStream(
        StreamOptions(ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=5)),
        spawner=silent_camera,
    )
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(port=0), MonitorParts(stream=stream))
    url = await monitor.ensure_page(auto_open=False)
    opened = [asyncio.Event(), asyncio.Event()]
    readers = [
        asyncio.create_task(read_some(f"{url}api/events", opened[0])),
        asyncio.create_task(read_some(f"{url}api/webcam/stream.mjpg", opened[1])),
    ]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in opened)), 5)
    await asyncio.sleep(0.1)

    caplog.set_level(logging.INFO)
    started = time.monotonic()
    await monitor.stop_page()
    elapsed = time.monotonic() - started
    await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), 2)
    await stream.stop()

    assert elapsed < FAST_STOP_SECONDS, f"stop took {elapsed:.2f} s"
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == [], [record.getMessage() for record in errors]
    assert not any("graceful shutdown" in record.getMessage() for record in caplog.records)


async def test_until_closing_closes_the_source_when_the_client_leaves() -> None:
    """A client that leaves cancels the response task. The source must still run its cleanup (viewer counts)."""
    closed: list[bool] = []

    async def waiting() -> AsyncGenerator[int]:
        try:
            yield 1
            await asyncio.Event().wait()
            yield 2
        finally:
            closed.append(True)

    async def read() -> None:
        async for _ in until_closing(waiting(), asyncio.Event()):
            pass

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.05)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert closed == [True]
