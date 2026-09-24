"""The monitor web API with the real MCP tools, a fake phone, and a fake webcam."""

import asyncio
import io
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from PIL import Image
from starlette.testclient import TestClient

from debug_devices_mcp.config import Settings
from debug_devices_mcp.multimeter import VisionClient
from debug_devices_mcp.phone_screen import ScreenState, ScreenStatus
from debug_devices_mcp.remote_webcam import RemoteMonitor, SharedWebcam
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts, bind_socket
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore, UiSettings
from debug_devices_mcp.ui.setup import build_monitor
from debug_devices_mcp.webcam import Crop
from debug_devices_mcp.webcam_stream import StreamOptions, WebcamStream

from .conftest import JPEG, make_jpeg
from .test_remote_webcam import identity
from .test_server import FakePhone, make_services, no_vision
from .test_webcam_stream import FakeStreamProcess, mpjpeg

BASE_URL = "http://127.0.0.1:18766"
START = EffectiveSettings(vision_model="openai/gpt-6-luna", webcam_warmup_frames=10, webcam_crop=None)


@pytest.fixture
def server_and_monitor(settings: Settings, tmp_path: Path) -> tuple[MCPServer, Monitor]:
    services = make_services(settings, FakePhone(), no_vision())
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    services.webcam = monitor.frame_source(services.webcam)
    server = build_server(services)
    monitor.instrument(server)
    return server, monitor


@pytest.fixture
def monitor(server_and_monitor: tuple[MCPServer, Monitor]) -> Monitor:
    return server_and_monitor[1]


@pytest.fixture
def client(monitor: Monitor) -> TestClient:
    return TestClient(create_app(monitor), base_url=BASE_URL)


def test_page_and_static_files(client: TestClient) -> None:
    assert "debug-devices monitor" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_other_hosts_and_origins_are_refused(client: TestClient) -> None:
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    response = client.post("/api/phone/connect", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.post("/api/phone/status", headers={"origin": BASE_URL}).status_code == 200


def test_phone_controls_run_the_tools_and_update_the_state(client: TestClient, monitor: Monitor) -> None:
    connected = client.post("/api/phone/connect").json()
    assert connected["serial"] == "R5CT1234567"
    zoomed = client.post("/api/phone/zoom", json={"step": "in"}).json()
    assert zoomed["status"]["zoom_ratio"] == 1.5
    torch = client.post("/api/phone/torch", json={"enabled": True}).json()
    assert torch["status"]["torch_enabled"] is True
    assert client.post("/api/phone/snapshot").json()["has_snapshot"] is True
    assert client.get("/api/phone/snapshot.jpg").content == JPEG

    calls = client.get("/api/state").json()["calls"]
    assert [call["tool"] for call in calls] == ["phone_connect", "phone_zoom", "phone_torch", "phone_snapshot"]
    assert {call["source"] for call in calls} == {"ui"}
    assert all(call["status"] == "ok" for call in calls)
    snapshot = calls[-1]
    assert snapshot["images"] == [{"index": 0, "label": "result image"}]
    assert client.get(f"/api/calls/{snapshot['id']}/images/0").content == JPEG


def test_phone_errors_are_502_with_the_tool_message(client: TestClient) -> None:
    response = client.post("/api/phone/zoom", json={})
    assert response.status_code == 502
    assert "exactly one" in response.json()["error"]
    assert client.get("/api/state").json()["calls"][-1]["status"] == "error"
    assert client.post("/api/phone/zoom", content=b"{bad").status_code == 400


def test_settings_persist_and_notify_listeners(client: TestClient, monitor: Monitor, tmp_path: Path) -> None:
    seen: list[EffectiveSettings] = []
    monitor.add_settings_listener(seen.append)
    crop = {"x": 1, "y": 2, "width": 30, "height": 40}
    body = {"vision_model": "x/model", "webcam_warmup_frames": 2, "webcam_crop": crop}
    view = client.put("/api/settings", json=body).json()
    assert view["effective"]["vision_model"] == "x/model"
    assert view["start"]["vision_model"] == "openai/gpt-6-luna"
    assert seen[-1].webcam_warmup_frames == 2
    assert SettingsStore.in_dir(tmp_path).load().webcam_crop is not None

    cleared = client.delete("/api/settings/crop").json()
    assert cleared["effective"]["webcam_crop"] is None
    assert cleared["saved"]["vision_model"] == "x/model"
    assert client.put("/api/settings", json={"webcam_warmup_frames": -1}).status_code == 400
    assert "OPENROUTER" not in client.get("/api/settings").text


def test_webcam_routes_without_stream(client: TestClient) -> None:
    assert client.get("/api/webcam/info").status_code == 404
    assert client.get("/api/state").json()["webcam"] is None


def test_events_stream_ends_when_the_monitor_closes(client: TestClient, monitor: Monitor) -> None:
    monitor.closing.set()
    response = client.get("/api/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == ": keepalive\n\n"


async def test_mcp_calls_are_recorded_with_the_model_input(server_and_monitor: tuple[MCPServer, Monitor]) -> None:
    server, monitor = server_and_monitor
    async with Client(server) as mcp_client:
        result = await mcp_client.call_tool("multimeter_read", {})
        status = await mcp_client.call_tool("phone_status", {})
    assert not result.is_error, result.content
    assert not status.is_error
    multimeter, phone_status = monitor.bus.calls()
    assert multimeter.source == "mcp"
    assert multimeter.images[0].label == "image sent to the model"
    assert multimeter.images[0].jpeg == JPEG
    assert multimeter.details["reading"]["unit"]
    assert phone_status.status == "ok"
    assert monitor.bus.phone.status is not None


def test_bind_socket_falls_back_to_a_free_port() -> None:
    first = bind_socket("127.0.0.1", 0)
    try:
        busy_port = first.getsockname()[1]
        first.listen()
        second = bind_socket("127.0.0.1", busy_port)
        assert second.getsockname()[1] != busy_port
        second.close()
    finally:
        first.close()


def test_build_monitor_takes_over_the_webcam_and_the_model(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    services = make_services(settings, FakePhone(), no_vision())
    monitor = build_monitor(settings, services)
    assert monitor.stream is not None
    assert monitor.scrcpy is None  # the separate scrcpy window is off by default
    assert monitor.screen is not None
    assert services.webcam.device == settings.webcam
    assert monitor.settings_path == tmp_path / "debug-devices" / "ui-settings.json"
    monitor.update_settings(UiSettings(vision_model="other/model"))
    assert services.vision.model == "other/model"


# region: webcam sharing and the multimeter button


def test_whoami_names_the_app_and_the_process(client: TestClient) -> None:
    whoami = client.get("/api/whoami").json()
    assert whoami["app"] == "debug-devices-monitor"
    assert whoami["pid"] == os.getpid()
    assert whoami["webcam_running"] is False


def test_read_multimeter_button(client: TestClient, monitor: Monitor) -> None:
    call = client.post("/api/multimeter/read").json()
    assert call["tool"] == "multimeter_read"
    assert call["source"] == "ui"
    assert call["details"]["reading"]["unit"]
    assert call["images"] == [{"index": 0, "label": "image sent to the model"}]
    assert client.get(f"/api/calls/{call['id']}/images/0").content == JPEG


def test_read_multimeter_button_error_is_502(settings: Settings, tmp_path: Path) -> None:
    services = make_services(settings, FakePhone(), no_vision())
    services.vision = VisionClient(None, "m", "https://openrouter.test/api/v1", timedelta(seconds=1))
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.instrument(build_server(services))
    response = TestClient(create_app(monitor), base_url=BASE_URL).post("/api/multimeter/read")
    assert response.status_code == 502
    assert "OPENROUTER_API_KEY" in response.json()["error"]
    [call] = monitor.bus.calls()
    assert call.status == "error"
    assert call.source == "ui"


async def feed_frames(process: FakeStreamProcess) -> None:
    """A webcam that sends a frame every 10 ms."""
    while True:
        process.stdout.feed_data(mpjpeg(make_jpeg(64, 48)))
        await asyncio.sleep(0.01)


async def test_cropped_frame_for_other_processes(tmp_path: Path) -> None:
    process = FakeStreamProcess(b"", eof=False)

    async def spawner(args):
        return process

    options = StreamOptions(
        ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=1)
    )
    stream = WebcamStream(options, spawner=spawner)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), parts=MonitorParts(stream=stream))
    stream.set_crop_provider(monitor.crop)
    monitor.update_settings(UiSettings(webcam_crop=Crop(x=0, y=0, width=10, height=6)))
    stream.start()
    feeder = asyncio.create_task(feed_frames(process))
    await stream.next_frame(0, timedelta(seconds=1))
    transport = httpx.ASGITransport(app=create_app(monitor))
    try:
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            whoami = (await http.get("/api/whoami")).json()
            assert whoami["webcam_running"] is True
            assert whoami["webcam_crop"] == {"x": 0, "y": 0, "width": 10, "height": 6}
            cropped = await http.get("/api/webcam/frame.jpg", params={"cropped": "true"})
            full = await http.get("/api/webcam/frame.jpg")
            monitor.webcam_owner = "http://127.0.0.1:18766/"
            owned = await http.get("/api/webcam/frame.jpg")
            info = (await http.get("/api/webcam/info")).json()
    finally:
        feeder.cancel()
        await stream.stop()
    assert cropped.status_code == 200, cropped.text
    with Image.open(io.BytesIO(cropped.content)) as image:
        assert image.size == (10, 6)
    with Image.open(io.BytesIO(full.content)) as image:
        assert image.size == (64, 48)
    assert owned.status_code == 503
    assert "owns the webcam" in info["error"]


class RecordingOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def open(self, url: str) -> str:
        self.urls.append(url)
        return "firefox"


async def test_second_monitor_uses_the_owner_and_opens_no_browser(tmp_path: Path) -> None:
    owner = identity()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=owner)

    remote = RemoteMonitor(18766, timedelta(seconds=1), transport=httpx.MockTransport(handle))
    stream = WebcamStream(
        StreamOptions(ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=1))
    )
    opener = RecordingOpener()
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(port=0, open_browser=True),
        MonitorParts(stream=stream, opener=opener, shared=SharedWebcam(stream, remote)),
    )
    await monitor.start()
    try:
        assert monitor.webcam_owner == owner["url"]
        assert opener.urls == []
        assert not stream.info().running
        assert monitor.identity().webcam_running is False
    finally:
        await monitor.stop()


async def test_first_monitor_owns_the_webcam_and_opens_the_browser(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    remote = RemoteMonitor(18766, timedelta(seconds=1), transport=httpx.MockTransport(handle))
    process = FakeStreamProcess(b"", eof=False)

    async def spawner(args):
        return process

    stream = WebcamStream(
        StreamOptions(ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=1)),
        spawner=spawner,
    )
    opener = RecordingOpener()
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(port=0, open_browser=True),
        MonitorParts(stream=stream, opener=opener, shared=SharedWebcam(stream, remote)),
    )
    await monitor.start()
    try:
        assert monitor.webcam_owner is None
        assert opener.urls == [monitor.url]
        await asyncio.sleep(0)
        assert stream.info().running
    finally:
        await monitor.stop()


# endregion: webcam sharing and the multimeter button


def test_phone_screen_route_is_404_when_off(client: TestClient, monitor: Monitor) -> None:
    assert client.get("/api/phone/screen").status_code == 404
    monitor.screen_changed(ScreenState(status=ScreenStatus.ERROR, error="no phone"))
    phone = client.get("/api/state").json()["phone"]
    assert (phone["screen"], phone["screen_error"]) == ("error", "no phone")


def test_screen_rotation_setting_through_the_api(client: TestClient) -> None:
    view = client.put("/api/settings", json={"screen_rotation": "90"}).json()
    assert view["effective"]["screen_rotation"] == "90"
    assert view["start"]["screen_rotation"] == "auto"
    assert client.put("/api/settings", json={"screen_rotation": "45"}).status_code == 400
    assert client.put("/api/settings", json={}).json()["effective"]["screen_rotation"] == "auto"


def test_snapshot_rotation_lock_runs_the_tool(client: TestClient) -> None:
    locked = client.post("/api/phone/rotation", json={"degrees": 270}).json()
    assert (locked["status"]["rotation_degrees"], locked["status"]["rotation_locked"]) == (270, True)
    auto = client.post("/api/phone/rotation", json={"auto": True}).json()
    assert auto["status"]["rotation_locked"] is False
    assert client.post("/api/phone/rotation", json={"degrees": 45}).status_code == 400
    calls = client.get("/api/state").json()["calls"]
    assert [call["tool"] for call in calls] == ["phone_rotation", "phone_rotation"]


async def test_status_poll_follows_the_phone_without_log_rows(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("debug_devices_mcp.ui.monitor.STATUS_POLL_SECONDS", 0.01)
    fake_phone = FakePhone()
    services = make_services(settings, fake_phone, no_vision())
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(open_browser=False, port=0),
        MonitorParts(status_reader=services.phone.status),
    )
    monitor.instrument(build_server(services))
    await monitor.call_from_ui("phone_connect", {})
    fake_phone.status["rotation_degrees"] = 90
    for _ in range(100):
        if monitor.bus.phone.status is not None and monitor.bus.phone.status.rotation_degrees == 90:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()
    assert monitor.bus.phone.status is not None
    assert monitor.bus.phone.status.rotation_degrees == 90
    assert [call.tool for call in monitor.bus.calls()] == ["phone_connect"]


def test_read_multimeter_button_with_the_phone(client: TestClient) -> None:
    call = client.post("/api/multimeter/read", json={"source": "phone"}).json()
    assert call["arguments"] == {"source": "phone", "include_image": True}
    assert call["images"] == [{"index": 0, "label": "image sent to the model"}]
    assert client.get(f"/api/calls/{call['id']}/images/0").content == JPEG
    assert client.post("/api/multimeter/read", json={"source": "ceiling"}).status_code == 400


def test_second_monitor_page_shows_the_webcam_of_the_owner(tmp_path: Path) -> None:
    owner_info = {"device": "/dev/video0", "running": True, "width": 1920, "height": 1080, "frames": 7, "error": None}
    mjpeg = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/webcam/info":
            return httpx.Response(200, json=owner_info)
        return httpx.Response(200, content=mjpeg, headers={"content-type": "multipart/x-mixed-replace"})

    remote = RemoteMonitor(18766, timedelta(seconds=1), transport=httpx.MockTransport(handle))
    stream = WebcamStream(
        StreamOptions(ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=1))
    )
    monitor = Monitor(
        START, SettingsStore.in_dir(tmp_path), parts=MonitorParts(stream=stream, shared=SharedWebcam(stream, remote))
    )
    monitor.webcam_owner = "http://127.0.0.1:18766/"
    client = TestClient(create_app(monitor), base_url=BASE_URL)
    assert client.get("/api/webcam/info").json() == owner_info
    assert client.get("/api/webcam/stream.mjpg").content == mjpeg


def test_full_screen_snapshot_is_the_full_image(settings: Settings, tmp_path: Path) -> None:
    fake_phone = FakePhone()
    fake_phone.snapshot = make_jpeg(3000, 2000)
    services = make_services(settings, fake_phone, no_vision())
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    services.phone.snapshot = monitor.phone_snapshot_recorder(services.phone.snapshot)  # type: ignore[method-assign]
    monitor.instrument(build_server(services))
    client = TestClient(create_app(monitor), base_url=BASE_URL)
    assert client.get("/api/phone/snapshot.jpg", params={"full": "true"}).status_code == 404

    assert client.post("/api/phone/snapshot").json()["has_snapshot"] is True
    panel = client.get("/api/phone/snapshot.jpg").content
    full = client.get("/api/phone/snapshot.jpg", params={"full": "true"}).content
    assert full == fake_phone.snapshot
    with Image.open(io.BytesIO(panel)) as image:
        assert max(image.size) == 1568  # the scaled tool result
    assert client.get("/api/phone/snapshot.jpg", params={"full": "maybe"}).status_code == 400
    # Only the running call keeps its full image; nothing stays behind.
    assert monitor._full_snapshots == {}


def test_full_screen_snapshot_falls_back_to_the_scaled_image(client: TestClient) -> None:
    # Without the recorder (for example a --no-ui style setup), the full view gets the tool result image.
    client.post("/api/phone/snapshot")
    assert client.get("/api/phone/snapshot.jpg", params={"full": "true"}).content == JPEG
