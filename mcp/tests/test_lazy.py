"""Lazy start: nothing at process start, the page at the first tool call, the webcam at its first use."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult

from debug_devices_mcp.config import Settings
from debug_devices_mcp.remote_webcam import RemoteMonitor, SharedWebcam
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.ui.constants import UiStart
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore
from debug_devices_mcp.webcam_stream import StreamOptions, WebcamStream

from .conftest import make_jpeg
from .test_server import FakePhone, make_services, no_vision
from .test_webcam_stream import FakeStreamProcess, mpjpeg

START = EffectiveSettings(vision_model="openai/gpt-6-luna", webcam_warmup_frames=0, webcam_crop=None)
IDLE = timedelta(minutes=5)
FRAME_INTERVAL = 0.01


class RecordingOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def open(self, url: str) -> str:
        self.urls.append(url)
        return "firefox"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Bench:
    """A monitor with fake parts. `spawned` lists every ffmpeg start; `removed` every adb forward removal."""

    monitor: Monitor
    services: Services
    stream: WebcamStream
    opener: RecordingOpener
    clock: FakeClock
    spawned: list[list[str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    feeders: list[asyncio.Task[None]] = field(default_factory=list)


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def feed(process: FakeStreamProcess) -> None:
    while True:
        process.stdout.feed_data(mpjpeg(make_jpeg(64, 48)))
        await asyncio.sleep(FRAME_INTERVAL)


def make_bench(settings: Settings, tmp_path: Path, start: UiStart, open_browser: bool = True) -> Bench:
    services = make_services(settings, FakePhone(), no_vision())
    clock = FakeClock()
    opener = RecordingOpener()
    bench: Bench

    async def spawner(args: Sequence[str]) -> FakeStreamProcess:
        bench.spawned.append(list(args))
        process = FakeStreamProcess(b"", eof=False)
        bench.feeders.append(asyncio.create_task(feed(process)))
        return process

    async def remove_forward(serial: str) -> None:
        bench.removed.append(serial)

    stream = WebcamStream(
        StreamOptions(ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=2)),
        spawner=spawner,
    )
    remote = RemoteMonitor(0, timedelta(seconds=1), transport=httpx.MockTransport(refused))
    shared = SharedWebcam(stream, remote)
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(port=0, open_browser=open_browser, start=start, webcam_idle_timeout=IDLE),
        MonitorParts(stream=stream, opener=opener, shared=shared, forward_remover=remove_forward, clock=clock),
    )
    shared.set_start_local(monitor.start_stream)
    services.webcam = monitor.frame_source(shared)
    bench = Bench(monitor=monitor, services=services, stream=stream, opener=opener, clock=clock)
    return bench


@pytest.fixture
async def lazy(settings: Settings, tmp_path: Path) -> AsyncIterator[Bench]:
    bench = make_bench(settings, tmp_path, UiStart.LAZY)
    yield bench
    await bench.monitor.stop()
    for task in bench.feeders:
        task.cancel()


def runner_calls(bench: Bench) -> list[list[str]]:
    return bench.services.adb._runner.calls  # type: ignore[attr-defined]


def structured(result: CallToolResult) -> dict:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def test_lazy_start_does_nothing_until_a_tool_call(lazy: Bench) -> None:
    async with Client(build_server(lazy.services, lazy.monitor)) as client:
        await client.list_tools()
        # No page, no port, no browser, no ffmpeg, no adb.
        assert lazy.monitor.url is None
        assert lazy.opener.urls == []
        assert lazy.spawned == []
        assert runner_calls(lazy) == []

        await client.call_tool("phone_status", {})
        url = lazy.monitor.url
        assert url is not None
        assert url.startswith("http://127.0.0.1:")
        async with httpx.AsyncClient() as http:
            assert (await http.get(url)).status_code == 200
        await client.call_tool("phone_status", {})
        # The browser opens one time, at the first page start. The webcam is still off.
        assert lazy.opener.urls == [url]
        assert lazy.spawned == []


async def test_webcam_starts_on_first_use_and_stops_when_idle(lazy: Bench) -> None:
    async with Client(build_server(lazy.services, lazy.monitor)) as client:
        result = await client.call_tool("webcam_snapshot", {})
        assert not result.is_error, result.content
        assert len(lazy.spawned) == 1
        assert lazy.stream.active

        lazy.clock.now += IDLE.total_seconds() - 1
        assert not await lazy.monitor.release_idle_webcam()
        async with lazy.monitor.webcam_viewer():
            lazy.clock.now += IDLE.total_seconds() + 1
            assert not await lazy.monitor.release_idle_webcam()  # a page watches
        lazy.clock.now += IDLE.total_seconds() + 1
        assert await lazy.monitor.release_idle_webcam()
        assert not lazy.stream.active

        # The next use starts it again.
        assert not (await client.call_tool("webcam_snapshot", {})).is_error
        assert len(lazy.spawned) == 2


async def test_monitor_open_returns_the_real_url(lazy: Bench) -> None:
    async with Client(build_server(lazy.services, lazy.monitor)) as client:
        quiet = structured(await client.call_tool("monitor_open", {"open_browser": False}))
        assert quiet["url"] == lazy.monitor.url
        assert quiet["opened_browser"] is False
        # monitor_open decides about the browser itself: no automatic window at its page start.
        assert lazy.opener.urls == []
        opened = structured(await client.call_tool("monitor_open", {}))
        assert opened == {"url": lazy.monitor.url, "opened_browser": True, "browser": "firefox"}
        assert lazy.opener.urls == [lazy.monitor.url]


async def test_bench_start_runs_every_step_and_bench_stop_frees_everything(lazy: Bench) -> None:
    async with Client(build_server(lazy.services, lazy.monitor)) as client:
        started = structured(
            await client.call_tool("bench_start", {"open_browser": False, "board_path": "/missing/board.brd"})
        )
        steps = {step["name"]: step for step in started["steps"]}
        assert started["url"] == lazy.monitor.url
        assert started["opened_browser"] is False
        assert steps["page"]["status"] == "ok"
        assert steps["webcam"]["status"] == "ok"
        assert steps["webcam"]["detail"] == "/dev/video0 64x48"
        assert steps["phone"]["status"] == "ok"
        # A failing step does not stop the others; its error is in the result.
        assert steps["board"]["status"] == "error"
        assert lazy.opener.urls == []
        logged = [call.tool for call in lazy.monitor.bus.calls()]
        assert logged == ["bench_start", "phone_connect", "board_open"]

        stopped = structured(await client.call_tool("bench_stop", {}))
        assert [step["status"] for step in stopped["steps"]] == ["ok", "ok", "ok"]
        assert not lazy.stream.active
        assert lazy.removed == ["R5CT1234567"]
        await asyncio.sleep(0.7)
        assert lazy.monitor.url is None
        # The log stays in memory, and the next tool starts the page again.
        await client.call_tool("phone_status", {})
        assert lazy.monitor.url is not None
        assert [call.tool for call in lazy.monitor.bus.calls()][-2:] == ["bench_stop", "phone_status"]


async def test_bench_start_skips_what_is_not_asked(lazy: Bench) -> None:
    async with Client(build_server(lazy.services, lazy.monitor)) as client:
        started = structured(await client.call_tool("bench_start", {"phone": False, "webcam": False}))
    statuses = {step["name"]: step["status"] for step in started["steps"]}
    assert statuses == {"page": "ok", "webcam": "skipped", "phone": "skipped", "board": "skipped"}
    assert started["opened_browser"] is True
    assert lazy.spawned == []


async def test_eager_mode_starts_the_page_and_the_webcam_at_once(settings: Settings, tmp_path: Path) -> None:
    bench = make_bench(settings, tmp_path, UiStart.EAGER, open_browser=False)
    try:
        async with Client(build_server(bench.services, bench.monitor)) as client:
            await client.list_tools()
            assert bench.monitor.url is not None
            assert len(bench.spawned) == 1
            bench.clock.now += IDLE.total_seconds() * 2
            assert not await bench.monitor.release_idle_webcam()  # no idle release in eager mode
    finally:
        for task in bench.feeders:
            task.cancel()
