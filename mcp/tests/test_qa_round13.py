"""QA round 13, dd-ui part: the primary lookup uses only the own page port (N50), frames of another monitor only with
its crop box (N51), and a short probe for a stale page port. Fakes only, except one local socket that accepts and
never answers (the stale port)."""

import asyncio
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.remote_webcam import RemoteCropMissingError, RemoteMonitor, SharedWebcam
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.constants import defaults, remote
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import SettingsStore

from .conftest import free_port
from .test_markings import START
from .test_remote_ports import DEVICE, PAGE_PORT, BusyWebcam, Monitors, whoami
from .test_server import FakePhone, make_services

TEST_PORT = 40111
TIMEOUT = timedelta(seconds=1)

# region: N50 the primary is the page on the own port only


def monitor_for(own_port: int, other_ports: list[int], monitors: Monitors, tmp_path: Path) -> Monitor:
    """A monitor as setup builds it: the webcam lookup with the other ports, the primary lookup with the own port."""
    transport = httpx.MockTransport(monitors.handle)
    webcam_owner = RemoteMonitor(own_port, TIMEOUT, transport=transport, other_ports=lambda: other_ports)
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(open_browser=False, port=0),
        MonitorParts(shared=SharedWebcam(BusyWebcam(), webcam_owner)),
    )
    monitor.primary_lookup = RemoteMonitor(own_port, TIMEOUT, transport=transport)
    return monitor


async def test_a_test_server_on_another_port_serves_its_own_page(tmp_path: Path) -> None:
    # A server with --ui-port 40111 while the user's monitor runs on 18766: not a secondary of 18766.
    monitors = Monitors({defaults.PORT: whoami(defaults.PORT)})
    monitor = monitor_for(TEST_PORT, [defaults.PORT], monitors, tmp_path)
    try:
        await monitor.ensure_page(auto_open=False)
        assert monitor.url is not None
        assert monitor.primary_url is None
        assert not monitor.is_secondary()
        # The webcam sharing still finds the running monitor on 18766.
        assert monitor.shared is not None
        assert await monitor.shared.use_remote_if_present()
    finally:
        await monitor.stop()


async def test_the_users_server_serves_its_page_while_another_page_runs(tmp_path: Path) -> None:
    # The user's server on 18766 while a test page runs on another port: 18766 is the primary.
    monitors = Monitors({PAGE_PORT: whoami(PAGE_PORT)})
    monitor = monitor_for(defaults.PORT, [defaults.PORT, PAGE_PORT], monitors, tmp_path)
    try:
        await monitor.ensure_page(auto_open=False)
        assert monitor.url is not None
        assert monitor.primary_url is None
        assert not monitor.is_secondary()
    finally:
        await monitor.stop()


# endregion: N50 the primary is the page on the own port only

# region: N51 frames of another monitor only with its crop


async def test_frames_of_a_monitor_without_a_crop_are_refused() -> None:
    monitors = Monitors({defaults.PORT: whoami(defaults.PORT, crop=None)})
    remote_monitor = RemoteMonitor(
        TEST_PORT, TIMEOUT, transport=httpx.MockTransport(monitors.handle), other_ports=lambda: [defaults.PORT]
    )
    shared = SharedWebcam(BusyWebcam(), remote_monitor)
    with pytest.raises(RemoteCropMissingError, match="set the crop box on the page that owns the webcam"):
        await shared.capture_jpeg()
    # No whole frame was asked from the owner.
    assert remote.FRAME_PATH not in monitors.paths_asked


async def test_multimeter_read_sends_no_frame_without_the_owners_crop(settings: Settings) -> None:
    monitors = Monitors({defaults.PORT: whoami(defaults.PORT, crop=None)})
    vision_calls: list[httpx.Request] = []

    def vision(request: httpx.Request) -> httpx.Response:
        vision_calls.append(request)
        return httpx.Response(500)

    services = make_services(settings, FakePhone(), httpx.MockTransport(vision))
    owner = RemoteMonitor(
        TEST_PORT, TIMEOUT, transport=httpx.MockTransport(monitors.handle), other_ports=lambda: [defaults.PORT]
    )
    services.webcam = SharedWebcam(BusyWebcam(), owner)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("multimeter_read", {})
    assert result.is_error
    assert "set the crop box on the page that owns the webcam" in result.content[0].text
    # No frame went to OpenRouter, and no frame was asked from the other monitor.
    assert vision_calls == []
    assert remote.FRAME_PATH not in monitors.paths_asked


# endregion: N51 frames of another monitor only with its crop

# region: a stale page port


async def test_a_stale_page_port_does_not_make_the_lookup_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "PROBE_TIMEOUT", timedelta(milliseconds=300))

    async def never_answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)

    stale = await asyncio.start_server(never_answer, "127.0.0.1", 0)
    stale_port = stale.sockets[0].getsockname()[1]
    # The webcam timeout is long (20 s by default): only the probe timeout limits the wait.
    lookup = RemoteMonitor(free_port(), timedelta(seconds=20), other_ports=lambda: [stale_port])
    try:
        started = time.monotonic()
        assert await lookup.identify(DEVICE) is None
        assert time.monotonic() - started < 2
    finally:
        await lookup.aclose()
        stale.close()


# endregion: a stale page port
