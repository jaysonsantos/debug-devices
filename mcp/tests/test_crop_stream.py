"""The crop-only webcam stream (`/api/webcam/crop.mjpg`): the meter picture of the full-screen phone view. Only the
crop box of each frame leaves the server, it follows a crop change, it ends when the crop box is cleared, and a
webcam of another monitor gives its cropped frames or nothing (never a whole frame, never the local webcam). Fakes
only."""

import asyncio
import io
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import httpx
from PIL import Image

from debug_devices_mcp.remote_webcam import SharedWebcam
from debug_devices_mcp.ui.events import EventKind
from debug_devices_mcp.ui.monitor import Monitor, MonitorParts
from debug_devices_mcp.ui.routes.webcam import NO_CROP, crop_body
from debug_devices_mcp.ui.settings import SettingsStore, SettingsView, UiSettings
from debug_devices_mcp.webcam import Crop
from debug_devices_mcp.webcam_stream import StreamOptions, WebcamStream

from .test_markings import START
from .test_remote_webcam import CROPPED, FakeMonitor, identity, remote_for
from .test_staged_capture import BASE_URL, page_client
from .test_ui_app import feed_frames
from .test_webcam_stream import FakeStreamProcess

WAIT_SECONDS = 5
OPTIONS = StreamOptions(ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=1))


def jpeg_size(part: bytes) -> tuple[int, int]:
    start = part.index(b"\xff\xd8")
    with Image.open(io.BytesIO(part[start:])) as image:
        return image.size


async def next_part(body: AsyncGenerator[bytes]) -> bytes:
    async with asyncio.timeout(WAIT_SECONDS):
        return await anext(body)


def fed_monitor(tmp_path: Path, crop: Crop | None) -> tuple[Monitor, WebcamStream, FakeStreamProcess]:
    process = FakeStreamProcess(b"", eof=False)

    async def spawner(args: object) -> FakeStreamProcess:
        return process

    stream = WebcamStream(OPTIONS, spawner=spawner)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), parts=MonitorParts(stream=stream))
    stream.set_crop_provider(monitor.crop)
    monitor.update_settings(UiSettings(webcam_crop=crop))
    return monitor, stream, process


async def test_only_the_crop_box_leaves_and_it_follows_the_crop(tmp_path: Path) -> None:
    monitor, stream, process = fed_monitor(tmp_path, Crop(x=0, y=0, width=10, height=6))
    feeder = asyncio.create_task(feed_frames(process))  # whole frames: 64 x 48
    body = crop_body(monitor, stream)
    try:
        # The page watches: the webcam starts (lazy start) and stays on.
        assert [jpeg_size(await next_part(body)) for _ in range(3)] == [(10, 6)] * 3
        assert stream.active
        monitor.update_settings(UiSettings(webcam_crop=Crop(x=5, y=5, width=20, height=10)))
        sizes = [jpeg_size(await next_part(body)) for _ in range(3)]
        assert sizes[-1] == (20, 10)
        # The crop box is cleared: the stream ends, and no whole frame goes out.
        monitor.update_settings(UiSettings(webcam_crop=None))
        rest = []
        async with asyncio.timeout(WAIT_SECONDS):
            rest = [jpeg_size(part) async for part in body]
        assert all(size != (64, 48) for size in rest)
    finally:
        await body.aclose()
        feeder.cancel()
        await stream.stop()


async def test_the_route_refuses_without_a_stream_or_a_crop_box(tmp_path: Path) -> None:
    async with page_client(Monitor(START, SettingsStore.in_dir(tmp_path / "none"))) as client:
        assert (await client.get("/api/webcam/crop.mjpg")).status_code == 404
    monitor, stream, _ = fed_monitor(tmp_path, None)
    async with page_client(monitor) as client:
        response = await client.get("/api/webcam/crop.mjpg")
    assert (response.status_code, response.json()["error"]) == (409, NO_CROP)
    # Nothing started for it.
    assert not stream.active


def owner_monitor(tmp_path: Path, owner: FakeMonitor) -> tuple[Monitor, WebcamStream]:
    stream = WebcamStream(OPTIONS)
    shared = SharedWebcam(stream, remote_for(owner))
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), parts=MonitorParts(stream=stream, shared=shared))
    monitor.webcam_owner = BASE_URL
    return monitor, stream


async def test_the_webcam_of_another_monitor_gives_its_cropped_frames(tmp_path: Path) -> None:
    owner = FakeMonitor()
    monitor, stream = owner_monitor(tmp_path, owner)
    body = crop_body(monitor, stream)
    try:
        parts = [await next_part(body) for _ in range(2)]
    finally:
        await body.aclose()
    assert all(CROPPED in part for part in parts)
    # Always the owner's cropped frame, never its whole frame.
    assert owner.frame_requests == ["cropped=true", "cropped=true"]


async def test_an_owner_without_a_crop_box_gives_nothing(tmp_path: Path) -> None:
    owner = FakeMonitor(whoami=identity(webcam_crop=None))
    monitor, stream = owner_monitor(tmp_path, owner)
    async with asyncio.timeout(WAIT_SECONDS):
        parts = [part async for part in crop_body(monitor, stream)]
    assert parts == []
    assert owner.frame_requests == []
    # And the local webcam was not read instead.
    assert not stream.active


async def test_the_route_streams_for_the_page(tmp_path: Path) -> None:
    # Through the app: the owner's cropped frame comes as MJPEG (the owner then stops answering: the stream ends).
    owner = FakeMonitor()
    monitor, _ = owner_monitor(tmp_path, owner)
    original = owner.handle

    def once(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/api/webcam/frame.jpg":
            owner.up = False
        return response

    owner.handle = once  # type: ignore[method-assign]
    assert monitor.shared is not None
    monitor.shared.remote = remote_for(owner)
    async with page_client(monitor) as client:
        response = await client.get("/api/webcam/crop.mjpg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert response.content.count(CROPPED) == 1


async def test_a_crop_change_goes_to_every_page_as_a_settings_event(tmp_path: Path) -> None:
    # N100: a crop box cleared on another page (or by a tool) reaches this page, so its meter picture stops.
    monitor, _, _ = fed_monitor(tmp_path, Crop(x=0, y=0, width=10, height=6))
    with monitor.bus.subscribe() as queue:
        await monitor.clear_crop()
        monitor.update_settings(UiSettings(webcam_crop=Crop(x=1, y=1, width=4, height=2)))
        crops = []
        while not queue.empty():
            message = queue.get_nowait()
            if message.kind is EventKind.SETTINGS:
                assert isinstance(message.data, SettingsView)
                crops.append(message.data.effective.webcam_crop)
    assert crops == [None, Crop(x=1, y=1, width=4, height=2)]
