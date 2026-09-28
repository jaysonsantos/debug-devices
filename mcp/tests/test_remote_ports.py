"""A server started with another --ui-port still shares the webcam of the running monitor (QA round 11): the lookup
tries its own port, then the default page port and the ports of the running pages (their token files in the
runtime dir). Fakes only: an httpx transport answers per port."""

import os
from datetime import timedelta
from pathlib import Path

import httpx

from debug_devices_mcp.remote_webcam import RemoteMonitor, SharedWebcam
from debug_devices_mcp.ui.constants import APP_NAME, defaults, remote
from debug_devices_mcp.ui.forward import other_monitor_ports, page_ports
from debug_devices_mcp.webcam import WebcamError

from .conftest import JPEG

DEVICE = Path("/dev/video0")
OWN_PORT = 40111
PAGE_PORT = 40123
OTHER_PID = os.getpid() + 1


# The owner's crop box: frames of another monitor are taken only with it (N51 of QA round 12).
OWNER_CROP = {"x": 10, "y": 20, "width": 300, "height": 120}


def whoami(
    port: int, *, pid: int = OTHER_PID, streaming: bool = True, device: Path = DEVICE, crop: dict | None = OWNER_CROP
) -> dict:
    return {
        "app": APP_NAME,
        "pid": pid,
        "url": f"http://127.0.0.1:{port}/",
        "webcam": str(device),
        "webcam_running": streaming,
        "webcam_crop": crop,
    }


class Monitors:
    """Answers per port: `identities` for /api/whoami, a JPEG for the frame route; a refused connection elsewhere."""

    def __init__(self, identities: dict[int, dict]) -> None:
        self.identities = identities
        self.ports_asked: list[int] = []
        self.paths_asked: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        port = request.url.port
        self.ports_asked.append(port)
        self.paths_asked.append(request.url.path)
        if port not in self.identities:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == remote.WHOAMI_PATH:
            return httpx.Response(200, json=self.identities[port])
        if request.url.path == remote.FRAME_PATH:
            return httpx.Response(200, content=JPEG)
        return httpx.Response(404)


def monitor_client(monitors: Monitors, other_ports: list[int] | None) -> RemoteMonitor:
    return RemoteMonitor(
        OWN_PORT,
        timedelta(seconds=1),
        transport=httpx.MockTransport(monitors.handle),
        other_ports=(lambda: other_ports) if other_ports is not None else None,
    )


async def test_the_monitor_on_the_default_port_is_found() -> None:
    monitors = Monitors({defaults.PORT: whoami(defaults.PORT)})
    client = monitor_client(monitors, [defaults.PORT])
    identity = await client.identify(DEVICE)
    assert identity is not None
    assert client.base_url.endswith(f":{defaults.PORT}")
    # The frames come from that monitor too.
    assert await client.capture_jpeg() == JPEG
    assert monitors.ports_asked[-1] == defaults.PORT


async def test_the_monitor_that_streams_the_device_wins() -> None:
    monitors = Monitors(
        {
            defaults.PORT: whoami(defaults.PORT, streaming=False),
            PAGE_PORT: whoami(PAGE_PORT),
        }
    )
    client = monitor_client(monitors, [defaults.PORT, PAGE_PORT])
    identity = await client.identify(DEVICE)
    assert identity is not None
    assert identity.url.endswith(f":{PAGE_PORT}/")


async def test_this_process_and_other_apps_are_not_a_monitor() -> None:
    other_app = {**whoami(PAGE_PORT), "app": "something-else"}
    monitors = Monitors({defaults.PORT: whoami(defaults.PORT, pid=os.getpid()), PAGE_PORT: other_app})
    assert await monitor_client(monitors, [defaults.PORT, PAGE_PORT]).identify(DEVICE) is None


async def test_without_other_ports_only_the_own_port_is_asked() -> None:
    # The call forwarder and the secondary screen stream keep their own port: they never reach another monitor.
    monitors = Monitors({defaults.PORT: whoami(defaults.PORT)})
    assert await monitor_client(monitors, None).find_monitor() is None
    assert set(monitors.ports_asked) == {OWN_PORT}


def test_the_page_ports_come_from_the_token_file_names(tmp_path: Path) -> None:
    (tmp_path / f"ingest-{PAGE_PORT}.token").write_text("secret")
    (tmp_path / "ingest-abc.token").write_text("x")
    (tmp_path / "notes.txt").write_text("x")
    assert page_ports(tmp_path) == [PAGE_PORT]
    assert other_monitor_ports(tmp_path) == [defaults.PORT, PAGE_PORT]
    assert page_ports(tmp_path / "missing") == []


class BusyWebcam:
    device = DEVICE
    crop = None

    async def capture_jpeg(self) -> bytes:
        raise WebcamError(f"ffmpeg could not read {DEVICE}: Device or resource busy")


async def test_a_busy_webcam_uses_the_running_monitor_on_the_default_port() -> None:
    monitors = Monitors({defaults.PORT: whoami(defaults.PORT)})
    shared = SharedWebcam(BusyWebcam(), monitor_client(monitors, [defaults.PORT]))
    assert await shared.capture_jpeg() == JPEG
    assert shared.remote_identity is not None
