import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from debug_devices_mcp.remote_webcam import MonitorIdentity, RemoteMonitor, SharedWebcam, is_busy
from debug_devices_mcp.webcam import Crop, WebcamError

from .conftest import JPEG

DEVICE = Path("/dev/video0")
CROP = Crop(x=1, y=2, width=3, height=4)
BUSY = WebcamError("ffmpeg could not read /dev/video0 (exit 1): Device or resource busy")


def identity(**changes: object) -> dict:
    base = MonitorIdentity(
        app="debug-devices-monitor",
        pid=os.getpid() + 1,
        url="http://127.0.0.1:18766/",
        webcam=str(DEVICE),
        webcam_running=True,
        webcam_crop=CROP,
    )
    return base.model_copy(update=changes).model_dump(mode="json")


class FakeMonitor:
    """The HTTP side of another monitor."""

    def __init__(self, whoami: dict | None = None, frame_status: int = 200) -> None:
        self.whoami = whoami if whoami is not None else identity()
        self.frame_status = frame_status
        self.up = True
        self.frame_requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json=self.whoami)
        if request.url.path == "/api/webcam/frame.jpg":
            self.frame_requests.append(str(request.url.query, "ascii"))
            if self.frame_status != 200:
                return httpx.Response(self.frame_status, json={"error": "no frame"})
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)


class FakeLocal:
    def __init__(self, error: WebcamError | None = None) -> None:
        self.error = error
        self.calls = 0

    @property
    def device(self) -> Path:
        return DEVICE

    @property
    def crop(self) -> Crop | None:
        return None

    async def capture_jpeg(self) -> bytes:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return b"\xff\xd8local"


def remote_for(fake: FakeMonitor) -> RemoteMonitor:
    return RemoteMonitor(18766, timedelta(seconds=1), transport=httpx.MockTransport(fake.handle))


@pytest.mark.parametrize(
    "whoami",
    [
        identity(app="some-other-service"),
        identity(pid=os.getpid()),
        identity(webcam="/dev/video2"),
        identity(webcam_running=False),
        {"unexpected": True},
    ],
)
async def test_identify_refuses_other_services_and_itself(whoami: dict) -> None:
    assert await remote_for(FakeMonitor(whoami)).identify(DEVICE) is None


async def test_identify_accepts_another_monitor_and_survives_no_answer() -> None:
    fake = FakeMonitor()
    remote = remote_for(fake)
    found = await remote.identify(DEVICE)
    assert found is not None
    assert found.webcam_crop == CROP
    fake.up = False
    assert await remote.identify(DEVICE) is None


def test_is_busy() -> None:
    assert is_busy(BUSY)
    assert not is_busy(WebcamError("no such device"))


async def test_local_webcam_first() -> None:
    fake = FakeMonitor()
    shared = SharedWebcam(FakeLocal(), remote_for(fake))
    assert await shared.capture_jpeg() == b"\xff\xd8local"
    assert fake.frame_requests == []


async def test_busy_webcam_uses_the_other_monitor_and_its_crop() -> None:
    fake = FakeMonitor()
    local = FakeLocal(BUSY)
    shared = SharedWebcam(local, remote_for(fake))
    assert await shared.capture_jpeg() == JPEG
    assert fake.frame_requests == ["cropped=true"]
    assert shared.crop == CROP
    # The next call goes to the other monitor at once.
    assert await shared.capture_jpeg() == JPEG
    assert local.calls == 1


async def test_busy_webcam_without_monitor_explains_the_problem() -> None:
    fake = FakeMonitor(identity(app="other"))
    shared = SharedWebcam(FakeLocal(BUSY), remote_for(fake))
    with pytest.raises(WebcamError, match=r"no debug-devices monitor answers on http://127\.0\.0\.1:18766"):
        await shared.capture_jpeg()


async def test_other_errors_do_not_try_the_other_monitor() -> None:
    fake = FakeMonitor()
    shared = SharedWebcam(FakeLocal(WebcamError("no such device")), remote_for(fake))
    with pytest.raises(WebcamError, match="no such device"):
        await shared.capture_jpeg()
    assert shared.remote_identity is None


async def test_lost_monitor_starts_the_local_webcam_again() -> None:
    fake = FakeMonitor()
    started: list[bool] = []
    shared = SharedWebcam(FakeLocal(), remote_for(fake), start_local=lambda: started.append(True))
    assert await shared.use_remote_if_present()
    fake.up = False
    assert await shared.capture_jpeg() == b"\xff\xd8local"
    assert started == [True]
    assert shared.remote_identity is None


async def test_remote_frame_error_is_a_webcam_error() -> None:
    fake = FakeMonitor(frame_status=503)
    shared = SharedWebcam(FakeLocal(BUSY), remote_for(fake))
    with pytest.raises(WebcamError, match="gave no frame"):
        await shared.capture_jpeg()
