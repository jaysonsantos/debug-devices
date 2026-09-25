"""The macro autofocus mode: client, tool, persistence, sync after connect and app restart, and the page route."""

import json
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import ValidationError
from starlette.testclient import TestClient

from debug_devices_mcp.camera_choice import AF_MODE_NOT_ON_PHONE, AfModeChoice
from debug_devices_mcp.config import Settings
from debug_devices_mcp.phone_api import CameraSettingsRequest
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore

from .test_preview_sync import client_for
from .test_server import FakePhone, make_services, no_vision

CAMERA = "/v1/camera"
START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"


class AfPhone(FakePhone):
    """The httpx fake phone with af_mode in POST /v1/camera.

    `mode`: "supported"; "no_macro" (200, stays continuous); "old" (an app with /v1/camera but without af_mode:
    400 for the unknown field, and no af_mode in the status).
    """

    def __init__(self, mode: str = "supported") -> None:
        super().__init__()
        self.mode = mode
        if mode != "old":
            self.status["af_mode"] = "continuous"
        self.bodies: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != CAMERA:
            return super().handle(request)
        self.requests.append((request.method, CAMERA))
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.mode == "old" and "af_mode" in body:
            return httpx.Response(400, json={"error": "bad_request", "message": "unknown field af_mode"})
        if "af_mode" in body:
            self.status["af_mode"] = "continuous" if self.mode == "no_macro" else body["af_mode"]
        return httpx.Response(200, json=self.status)

    def restart_app(self) -> None:
        self.status["af_mode"] = "continuous"


def services_for(settings: Settings, phone: FakePhone, tmp_path: Path, mode: str = "continuous"):
    services = make_services(settings, phone, no_vision())
    services.af_mode = AfModeChoice(SettingsStore.in_dir(tmp_path))
    services.af_mode.set(mode)  # type: ignore[arg-type]
    services.__post_init__()
    return services


def text(result) -> str:
    return next(block.text for block in result.content if isinstance(block, TextContent))


async def test_request_sends_only_the_given_fields() -> None:
    phone = AfPhone()
    await client_for(phone).camera(CameraSettingsRequest(af_mode="macro"))
    await client_for(phone).camera(CameraSettingsRequest(in_sensor_zoom=True))
    assert phone.bodies == [{"af_mode": "macro"}, {"in_sensor_zoom": True}]
    with pytest.raises(ValidationError):
        CameraSettingsRequest()


async def test_tool_sets_saves_and_reports(settings: Settings, tmp_path: Path) -> None:
    phone = AfPhone()
    services = services_for(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("phone_af_mode", {"mode": "macro"})
        wrong = await client.call_tool("phone_af_mode", {"mode": "manual"})
    assert result.structured_content is not None
    assert result.structured_content["af_mode"] == "macro"
    assert phone.bodies == [{"af_mode": "macro"}]
    assert SettingsStore.in_dir(tmp_path).load().af_mode == "macro"
    assert AfModeChoice(SettingsStore.in_dir(tmp_path)).mode == "macro"  # a new server start reads it
    assert wrong.is_error


async def test_a_phone_without_macro_says_so_and_does_not_repeat(settings: Settings, tmp_path: Path) -> None:
    phone = AfPhone("no_macro")
    services = services_for(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("phone_af_mode", {"mode": "macro"})
        for _ in range(3):
            await client.call_tool("phone_status", {})
    assert result.structured_content["af_mode"] == "continuous"
    assert AF_MODE_NOT_ON_PHONE in result.structured_content["advice_text"]
    assert len(phone.bodies) == 1  # the status polls do not send it again


async def test_an_app_without_af_mode_says_update(settings: Settings, tmp_path: Path) -> None:
    services = services_for(settings, AfPhone("old"), tmp_path)
    async with Client(build_server(services)) as client:
        result = await client.call_tool("phone_af_mode", {"mode": "macro"})
    assert result.is_error
    assert "update the phone app for the macro focus" in text(result)


async def test_sync_after_connect_and_app_restart(settings: Settings, tmp_path: Path) -> None:
    phone = AfPhone()
    services = services_for(settings, phone, tmp_path, mode="macro")
    async with Client(build_server(services)) as client:
        connected = await client.call_tool("phone_connect", {})
        assert connected.structured_content["status"]["af_mode"] == "macro"
        await client.call_tool("phone_status", {})
        assert phone.bodies == [{"af_mode": "macro"}]  # no new request while the phone matches
        phone.restart_app()
        status = await client.call_tool("phone_status", {})
        assert status.structured_content["af_mode"] == "macro"
        assert phone.bodies == [{"af_mode": "macro"}, {"af_mode": "macro"}]


async def test_continuous_choice_sends_nothing_to_a_normal_phone(settings: Settings, tmp_path: Path) -> None:
    phone = AfPhone()
    services = services_for(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_connect", {})
        await client.call_tool("phone_status", {})
    assert phone.bodies == []


def test_page_route_runs_the_tool(settings: Settings, tmp_path: Path) -> None:
    phone = AfPhone()
    services = services_for(settings, phone, tmp_path / "choice")
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path / "page"),
        MonitorOptions(open_browser=False, port=0),
        MonitorParts(af_mode=services.af_mode),
    )
    services.af_mode.add_listener(monitor.af_mode_changed)
    monitor.instrument(build_server(services))
    client = TestClient(create_app(monitor), base_url=BASE_URL)
    response = client.post("/api/phone/af-mode", json={"mode": "macro"})
    assert response.status_code == 200, response.text
    assert response.json()["af_mode_choice"] == "macro"
    assert response.json()["status"]["af_mode"] == "macro"
    assert client.post("/api/phone/af-mode", json={"mode": "MACRO"}).status_code == 400
    # A save of the other settings keeps the choice.
    client.put("/api/settings", json={"vision_model": "x/model"})
    assert monitor.saved.af_mode == "macro"
