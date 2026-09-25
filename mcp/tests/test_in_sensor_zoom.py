"""The in-sensor zoom: client, tool, persistence, sync after connect and app restart, and the detail note."""

import json
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.types import TextContent
from starlette.testclient import TestClient

from debug_devices_mcp.camera_choice import InSensorZoomChoice
from debug_devices_mcp.config import Settings
from debug_devices_mcp.focus import focus_report
from debug_devices_mcp.phone_api import CameraSettingsNotSupportedError, CameraSettingsRequest, CameraStatus
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore, UiSettings

from .test_phone_api import STATUS
from .test_preview_sync import client_for
from .test_server import FakePhone, make_services, no_vision

CAMERA = "/v1/camera"
START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"


class ZoomModePhone(FakePhone):
    """The httpx fake phone with POST /v1/camera. `mode` "old" is an app from before it (404)."""

    def __init__(self, mode: str = "supported") -> None:
        super().__init__()
        self.mode = mode
        if mode != "old":
            self.status["in_sensor_zoom"] = "off"
        self.sent: list[bool] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != CAMERA:
            return super().handle(request)
        self.requests.append((request.method, CAMERA))
        if self.mode == "old":
            return httpx.Response(404, json={"error": "not_found", "message": "no route"})
        enabled = json.loads(request.content)["in_sensor_zoom"]
        self.sent.append(enabled)
        self.status["in_sensor_zoom"] = {"unsupported": "unsupported", "fallback": "fallback"}.get(
            self.mode, "on" if enabled else "off"
        )
        return httpx.Response(200, json=self.status)

    def restart_app(self) -> None:
        self.status["in_sensor_zoom"] = "off"


def services_for(settings: Settings, phone: FakePhone, tmp_path: Path, enabled: bool = False):
    services = make_services(settings, phone, no_vision())
    services.in_sensor_zoom = InSensorZoomChoice(SettingsStore.in_dir(tmp_path))
    services.in_sensor_zoom.set(enabled)
    services.__post_init__()
    return services


async def test_client_call_and_old_app() -> None:
    phone = ZoomModePhone()
    status = await client_for(phone).camera(CameraSettingsRequest(in_sensor_zoom=True))
    assert (status.in_sensor_zoom, phone.sent) == ("on", [True])
    with pytest.raises(CameraSettingsNotSupportedError, match="update the phone app"):
        await client_for(ZoomModePhone("old")).camera(CameraSettingsRequest(in_sensor_zoom=True))
    assert CameraStatus.model_validate(STATUS).in_sensor_zoom is None  # an old app status


async def test_tool_sets_saves_and_reports(settings: Settings, tmp_path: Path) -> None:
    phone = ZoomModePhone()
    services = services_for(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("phone_in_sensor_zoom", {"enabled": True})
    assert result.structured_content is not None
    assert result.structured_content["in_sensor_zoom"] == "on"
    assert SettingsStore.in_dir(tmp_path).load().in_sensor_zoom is True
    assert InSensorZoomChoice(SettingsStore.in_dir(tmp_path)).enabled  # a new server start reads it


async def test_tool_says_update_the_app_for_an_old_app(settings: Settings, tmp_path: Path) -> None:
    services = services_for(settings, ZoomModePhone("old"), tmp_path)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("phone_in_sensor_zoom", {"enabled": True})
    assert result.is_error
    text = next(block.text for block in result.content if isinstance(block, TextContent))
    assert "update the phone app" in text


async def test_sync_after_connect_and_app_restart(settings: Settings, tmp_path: Path) -> None:
    phone = ZoomModePhone()
    services = services_for(settings, phone, tmp_path, enabled=True)
    async with Client(build_server(services)) as client:
        connected = await client.call_tool("phone_connect", {})
        assert connected.structured_content["status"]["in_sensor_zoom"] == "on"
        await client.call_tool("phone_status", {})
        assert phone.sent == [True]  # no new rebind while the phone matches
        phone.restart_app()
        status = await client.call_tool("phone_status", {})
        assert status.structured_content["in_sensor_zoom"] == "on"
        assert phone.sent == [True, True]


async def test_no_send_for_unsupported_or_fallback(settings: Settings, tmp_path: Path) -> None:
    for mode in ("unsupported", "fallback"):
        phone = ZoomModePhone(mode)
        services = services_for(settings, phone, tmp_path, enabled=True)
        async with Client(build_server(services)) as client:
            await client.call_tool("phone_connect", {})  # sends once: the status was off
            for _ in range(3):
                await client.call_tool("phone_status", {})
        assert phone.sent == [True], mode  # never again: each send rebinds the camera


async def test_choice_off_turns_a_restarted_on_back_off(settings: Settings, tmp_path: Path) -> None:
    phone = ZoomModePhone()
    phone.status["in_sensor_zoom"] = "on"
    services = services_for(settings, phone, tmp_path, enabled=False)
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_connect", {})
    assert phone.sent == [False]


def test_detail_note_only_when_on_and_zoomed() -> None:
    base = STATUS | {"in_sensor_zoom": "on", "zoom_ratio": 2.0}
    assert focus_report(CameraStatus.model_validate(base)).sensor_zoom_boost
    assert not focus_report(CameraStatus.model_validate(base | {"zoom_ratio": 1.5})).sensor_zoom_boost
    assert not focus_report(CameraStatus.model_validate(base | {"in_sensor_zoom": "fallback"})).sensor_zoom_boost


def test_page_toggle_runs_the_tool_and_keeps_the_choice(settings: Settings, tmp_path: Path) -> None:
    phone = ZoomModePhone()
    services = services_for(settings, phone, tmp_path)
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(port=0),
        MonitorParts(in_sensor_zoom=services.in_sensor_zoom),
    )
    services.in_sensor_zoom.add_listener(monitor.in_sensor_zoom_changed)
    monitor.instrument(build_server(services))
    client = TestClient(create_app(monitor), base_url=BASE_URL)
    state = client.post("/api/phone/in-sensor-zoom", json={"enabled": True}).json()
    assert state["in_sensor_zoom_choice"] is True
    assert state["status"]["in_sensor_zoom"] == "on"
    assert client.post("/api/phone/in-sensor-zoom", json={"enabled": "yes"}).status_code == 400
    # A page save of other settings keeps the choice.
    monitor.update_settings(UiSettings(vision_model="y/model", in_sensor_zoom=False))
    assert SettingsStore.in_dir(tmp_path).load().in_sensor_zoom is True
