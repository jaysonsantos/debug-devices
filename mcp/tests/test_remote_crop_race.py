"""N99: a server that uses the webcam of another monitor never gets or passes on a whole webcam frame, also when the
owner's crop box is cleared during the request (dd-qa's race). The owner answers `cropped=true` with 409 then; the
requester refuses a frame larger than the crop box that it expects and never reads the local webcam instead. 50 trials
each for the crop stream of the page, multimeter_read, and a staged capture. Fakes only (the owner is the real app
in this process, seen as another process)."""

import asyncio
import base64
import io
import json
import random
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image

from debug_devices_mcp.config import Settings
from debug_devices_mcp.multimeter import MeterSource
from debug_devices_mcp.remote_webcam import RemoteCropMissingError, RemoteMonitor, SharedWebcam
from debug_devices_mcp.server import read_meter
from debug_devices_mcp.staged import StagedStore
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorParts
from debug_devices_mcp.ui.routes.webcam import crop_body
from debug_devices_mcp.ui.settings import SettingsStore, UiSettings
from debug_devices_mcp.ui.staged_capture import StagedCapturer
from debug_devices_mcp.webcam import Crop
from debug_devices_mcp.webcam_stream import StreamOptions, WebcamStream

from .conftest import make_jpeg
from .test_markings import START
from .test_multimeter import READING, completion
from .test_remote_webcam import BUSY, FakeLocal, FakeMonitor
from .test_server import FakePhone, make_services
from .test_staged_capture import BASE_URL
from .test_ui_app import feed_frames
from .test_webcam_stream import FakeStreamProcess

TRIALS = 50
WHOLE = (64, 48)  # the owner's frames (feed_frames)
CROP = Crop(x=0, y=0, width=10, height=6)
DEVICE = Path("/dev/video0")
OPTIONS = StreamOptions(ffmpeg_path="ffmpeg", device=DEVICE, warmup_frames=0, timeout=timedelta(seconds=1))
# The clear comes this long (at random) after the request starts: the owner feeds a frame every 10 ms.
CLEAR_DELAY_S = 0.02
WAIT_SECONDS = 5


def size_of(jpeg: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(jpeg[jpeg.index(b"\xff\xd8") :])) as image:
        return image.size


class OtherProcess(httpx.AsyncBaseTransport):
    """The owner's app, seen as another process (its whoami names another pid)."""

    def __init__(self, monitor: Monitor) -> None:
        self.inner = httpx.ASGITransport(app=create_app(monitor))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.url.path != "/api/whoami":
            return response
        await response.aread()
        identity = json.loads(response.content)
        identity["pid"] += 1
        return httpx.Response(response.status_code, json=identity)


class Owner:
    """The monitor that owns the webcam: a fake ffmpeg with 64 x 48 frames every 10 ms, and a crop box."""

    def __init__(self, tmp_path: Path) -> None:
        process = FakeStreamProcess(b"", eof=False)

        async def spawner(args: object) -> FakeStreamProcess:
            return process

        self.stream = WebcamStream(OPTIONS, spawner=spawner)
        self.monitor = Monitor(START, SettingsStore.in_dir(tmp_path / "owner"), parts=MonitorParts(stream=self.stream))
        self.stream.set_crop_provider(self.monitor.crop)
        self.process = process
        self.set_crop(CROP)

    def set_crop(self, crop: Crop | None) -> None:
        self.monitor.update_settings(UiSettings(webcam_crop=crop))

    async def start_trial(self, shared: SharedWebcam | None = None) -> asyncio.Task[None]:
        """Set the crop box, let the requester see it (as after its first busy capture), and clear it soon."""
        self.set_crop(CROP)
        if shared is not None:
            assert await shared.use_remote_if_present()
        return asyncio.create_task(self.clear_soon())

    async def clear_soon(self) -> None:
        await asyncio.sleep(random.uniform(0, CLEAR_DELAY_S))
        self.set_crop(None)

    def remote(self) -> RemoteMonitor:
        return RemoteMonitor(18766, timedelta(seconds=2), transport=OtherProcess(self.monitor))


@pytest.fixture
async def owner(tmp_path: Path) -> Iterator[Owner]:
    owner = Owner(tmp_path)
    owner.stream.start()
    feeder = asyncio.create_task(feed_frames(owner.process))
    yield owner
    feeder.cancel()
    await owner.stream.stop()


class SeenImages:
    """The vision model: records the size of each image that it gets."""

    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    def transport(self) -> httpx.MockTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            for message in json.loads(request.content)["messages"]:
                for part in message["content"] if isinstance(message["content"], list) else []:
                    if part.get("type") == "image_url":
                        data = part["image_url"]["url"].split(",", 1)[1]
                        self.sizes.append(size_of(base64.b64decode(data)))
            return completion(json.dumps(READING))

        return httpx.MockTransport(answer)


def requester(settings: Settings, owner: Owner, seen: SeenImages) -> tuple[SharedWebcam, object]:
    """Another MCP server whose own webcam is busy: it uses the owner's frames."""
    fast = settings.model_copy(update={"meter_frame_interval": timedelta(0)})
    services = make_services(fast, FakePhone(), seen.transport())
    shared = SharedWebcam(FakeLocal(BUSY), owner.remote())
    services.webcam = shared
    return shared, services


# region: the owner side


async def test_the_owner_never_answers_a_cropped_request_with_a_whole_frame(owner: Owner) -> None:
    outcomes: dict[str, int] = {"crop": 0, "refused": 0, "whole": 0}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(owner.monitor)), base_url=BASE_URL
    ) as client:
        for _ in range(TRIALS):
            owner.set_crop(CROP)
            clear = asyncio.create_task(owner.clear_soon())
            response = await client.get("/api/webcam/frame.jpg", params={"cropped": "true"})
            await clear
            if response.status_code == 409:
                outcomes["refused"] += 1
            else:
                outcomes["whole" if size_of(response.content) == WHOLE else "crop"] += 1
    assert outcomes["whole"] == 0, outcomes
    # The race was hit: some requests came after the clear.
    assert outcomes["refused"] > 0, outcomes


# endregion: the owner side

# region: the requester side, with the real owner


async def test_the_crop_stream_of_a_requester_never_passes_a_whole_frame(
    owner: Owner, settings: Settings, tmp_path: Path
) -> None:
    shared, _ = requester(settings, owner, SeenImages())
    page = Monitor(START, SettingsStore.in_dir(tmp_path / "page"), parts=MonitorParts(stream=WebcamStream(OPTIONS)))
    page.shared = shared
    page.webcam_owner = BASE_URL
    sizes: list[tuple[int, int]] = []
    for _ in range(TRIALS):
        clear = await owner.start_trial(shared)
        async with asyncio.timeout(WAIT_SECONDS):
            sizes += [size_of(part) async for part in crop_body(page, page.stream)]
        await clear
    assert WHOLE not in sizes
    assert set(sizes) <= {(CROP.width, CROP.height)}
    assert sizes


async def test_multimeter_read_of_a_requester_never_sends_a_whole_frame(owner: Owner, settings: Settings) -> None:
    seen = SeenImages()
    shared, services = requester(settings, owner, seen)
    errors = 0
    for _ in range(TRIALS):
        clear = await owner.start_trial(shared)
        try:
            await read_meter(services, MeterSource.WEBCAM, None, 2, require_crop=True)
        # A refusal of any kind is right; only a whole frame that was sent fails the test.
        except Exception:
            errors += 1
        await clear
    assert WHOLE not in seen.sizes
    assert set(seen.sizes) <= {(CROP.width, CROP.height)}
    # The race was hit (some reads refused), and some reads worked (the requester got crops).
    assert 0 < errors < TRIALS
    assert seen.sizes


async def test_a_staged_capture_of_a_requester_never_keeps_a_whole_frame(
    owner: Owner, settings: Settings, tmp_path: Path
) -> None:
    seen = SeenImages()
    shared, services = requester(settings, owner, seen)
    store = StagedStore(tmp_path / "staged")
    capturer = StagedCapturer(services, store, lambda: "other 2")
    kept: list[tuple[int, int]] = []
    without_meter = 0
    for _ in range(TRIALS):
        clear = await owner.start_trial(shared)
        await capturer.capture()
        await capturer.wait()
        await clear
        # Pop each capture: the queue keeps at most 10.
        for item in await store.pop_all():
            kept += [size_of(frame) for frame in item.frames if frame is not None]
            without_meter += item.capture.meter is None
    assert WHOLE not in kept + seen.sizes
    assert set(kept + seen.sizes) <= {(CROP.width, CROP.height)}
    # The race was hit (some captures have no meter part), and some captures have one.
    assert 0 < without_meter < TRIALS
    assert kept


# endregion: the requester side, with the real owner

# region: the requester side, with an owner that sends whole frames


async def test_a_requester_refuses_a_whole_frame_and_does_not_read_its_own_webcam() -> None:
    # An owner with old code (or a bug) that answers cropped=true with its whole frame.
    owner = FakeMonitor(frame=make_jpeg(*WHOLE))
    local = FakeLocal(BUSY)
    shared = SharedWebcam(
        local, RemoteMonitor(18766, timedelta(seconds=1), transport=httpx.MockTransport(owner.handle))
    )
    for _ in range(3):
        with pytest.raises(RemoteCropMissingError, match="larger than its crop box"):
            await shared.capture_jpeg()
    # The local webcam was asked one time (busy, before the owner was known) and never again.
    assert local.calls == 1


async def test_a_409_of_the_owner_is_no_reason_to_read_the_own_webcam() -> None:
    owner = FakeMonitor(frame_status=409)
    local = FakeLocal(BUSY)
    shared = SharedWebcam(
        local, RemoteMonitor(18766, timedelta(seconds=1), transport=httpx.MockTransport(owner.handle))
    )
    with pytest.raises(RemoteCropMissingError, match="was cleared during the frame request"):
        await shared.capture_jpeg()
    assert local.calls == 1


# endregion: the requester side, with an owner that sends whole frames
