"""The Markings toggle: hide or show the page's drawings and the phone's own boxes (they are kept)."""

import json
from pathlib import Path
from uuid import uuid7

import httpx
import pytest
from mcp import Client
from starlette.testclient import TestClient

from debug_devices_mcp.camera_choice import MarkingsChoice
from debug_devices_mcp.config import Settings
from debug_devices_mcp.phone_api import OverlayArrow
from debug_devices_mcp.server import MARKINGS_HIDDEN, OLD_APP_HIDDEN, Services, build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore
from debug_devices_mcp.ui.setup import connect_services

from .conftest import make_jpeg
from .test_highlight import OverlayPhone
from .test_server import make_services, no_vision

START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"
OVERLAY = "/v1/overlay"
BOX = {"x": 10, "y": 10, "width": 40, "height": 30, "label": "R1"}


class VisiblePhone(OverlayPhone):
    """The overlay fake with `visible` (hide and show, keep the boxes). `old_visible`: an app without it (400), and
    also without box tags (400), like the app 0.1.0 on the Redmi Note 15 Pro+."""

    def __init__(self, old_visible: bool = False) -> None:
        super().__init__()
        self.old_visible = old_visible
        if not old_visible:
            self.status["overlay_visible"] = True
        self.visible_sent: list[bool] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == OVERLAY:
            body = json.loads(request.content)
            old_request = "visible" in body or any("tag" in box for box in body.get("boxes", []))
            if self.old_visible and old_request:
                return httpx.Response(400, json={"error": "bad_request", "message": "Send boxes"})
            if "visible" in body:
                self.visible_sent.append(body["visible"])
                self.status["overlay_visible"] = body["visible"]
                return httpx.Response(200, json=self.status)
        return super().handle(request)

    def restart_app(self) -> None:
        self.status.update(app_start_id=str(uuid7()), overlay_visible=True, overlay_boxes=0)


def services_for(settings: Settings, phone: VisiblePhone, directory: Path) -> Services:
    services = make_services(settings, phone, no_vision())
    services.markings = MarkingsChoice(SettingsStore.in_dir(directory))
    services.__post_init__()
    return services


def test_the_choice_is_saved_and_shared(tmp_path: Path) -> None:
    mine, other = MarkingsChoice(SettingsStore.in_dir(tmp_path)), MarkingsChoice(SettingsStore.in_dir(tmp_path))
    heard: list[bool] = []
    other.add_listener(heard.append)
    assert mine.visible is True  # shown by default
    mine.set(False)
    assert other.visible is False
    other.refresh()
    assert heard == [False]


async def test_hide_keeps_the_boxes_and_the_tool_says_so(settings: Settings, tmp_path: Path) -> None:
    phone = VisiblePhone()
    phone.snapshot = make_jpeg(400, 300)
    services = services_for(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {})
        await client.call_tool("phone_highlight", {"boxes": [BOX]})
        hidden = await services.set_markings(False)
        highlighted = await client.call_tool("phone_highlight", {"boxes": [BOX, {**BOX, "x": 100, "label": "R2"}]})
        shown = await services.set_markings(True)
        again = await client.call_tool("phone_highlight", {"boxes": [BOX]})
    assert (hidden.visible, hidden.phone) == (False, "hidden")
    assert phone.visible_sent == [False, True]
    # The new boxes while hidden reached the phone (kept, not shown); the result says that they are hidden.
    assert len(phone.sent[-2]) == 2
    assert highlighted.structured_content["markings"] == MARKINGS_HIDDEN
    assert (shown.visible, shown.phone) == (True, "shown")
    assert again.structured_content["markings"] is None


async def test_the_server_hides_the_boxes_of_an_old_app(settings: Settings, tmp_path: Path) -> None:
    phone = VisiblePhone(old_visible=True)
    phone.snapshot = make_jpeg(400, 300)
    services = services_for(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {})
        await client.call_tool("phone_highlight", {"boxes": [BOX]})
        assert len(phone.sent[-1]) == 1
        hidden = await services.set_markings(False)
        # The app cannot hide its boxes: the server removed them from the phone, and keeps them.
        assert (hidden.visible, hidden.phone) == (False, OLD_APP_HIDDEN)
        assert (phone.sent[-1], len(services.highlights)) == ([], 1)
        highlighted = await client.call_tool("phone_highlight", {"boxes": [BOX, {**BOX, "x": 100, "label": "R2"}]})
        # New boxes while hidden stay off the phone; the page and the tool result have them.
        assert (phone.sent[-1], len(services.highlights)) == ([], 2)
        assert highlighted.structured_content["markings"] == MARKINGS_HIDDEN
        shown = await services.set_markings(True)
        again = await client.call_tool("phone_highlight", {"boxes": [BOX]})
    assert (shown.visible, shown.phone) == (True, "shown")
    # Sent again (without the tags: the old app does not take them), then the next call as usual.
    assert [[box["label"] for box in boxes] for boxes in phone.sent[-2:]] == [["R1", "R2"], ["R1"]]
    assert again.structured_content["markings"] is None
    assert services.markings.visible is True


async def test_an_old_app_while_hidden_gets_no_pointing_arrows(settings: Settings, tmp_path: Path) -> None:
    phone = VisiblePhone(old_visible=True)
    services = services_for(settings, phone, tmp_path)
    await services.set_markings(False)
    status = await services.send_overlay([], [OverlayArrow(angle_deg=90, label="C1")])
    assert (phone.arrows_sent[-1], status.overlay_arrows) == ([], 0)
    assert [arrow.label for arrow in services.arrows] == ["C1"]
    await services.set_markings(True)
    assert [arrow["label"] for arrow in phone.arrows_sent[-1]] == ["C1"]


async def test_the_old_app_note_goes_to_the_page(settings: Settings, tmp_path: Path) -> None:
    services = services_for(settings, VisiblePhone(old_visible=True), tmp_path)
    result = await services.set_markings(False)
    # The page shows its note for an answer that starts with this marker (app.js OLD_APP_MARKER).
    assert result.phone.startswith("the phone app is old")
    assert services.markings.visible is False  # the page hides its own drawings too


async def test_an_app_restart_gets_the_hidden_state_once(settings: Settings, tmp_path: Path) -> None:
    phone = VisiblePhone()
    services = services_for(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_connect", {})
        await services.set_markings(False)
        phone.restart_app()  # the app starts with its overlay shown
        for _ in range(3):
            await client.call_tool("phone_status", {})
    assert phone.visible_sent == [False, False]
    assert phone.status["overlay_visible"] is False


def test_page_route(settings: Settings, tmp_path: Path) -> None:
    phone = VisiblePhone()
    services = services_for(settings, phone, tmp_path / "choice")
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path / "page"), MonitorOptions(open_browser=False, port=0))
    monitor.instrument(build_server(services))
    connect_services(monitor, services)
    client = TestClient(create_app(monitor), base_url=BASE_URL)
    response = client.post("/api/phone/markings", json={"visible": False})
    assert response.status_code == 200, response.text
    assert (response.json()["markings_visible"], response.json()["markings_phone"]) == (False, "hidden")
    assert client.post("/api/phone/markings", json={"visible": "no"}).status_code == 400
    [row] = [call for call in monitor.bus.calls() if call.tool == "markings"]
    assert row.source == "ui"
    assert row.summary == "hidden; phone: hidden"
    # A save of the other settings keeps the choice.
    client.put("/api/settings", json={"vision_model": "x/model"})
    assert monitor.saved.markings_visible is False


@pytest.mark.parametrize("visible", [True, False])
def test_the_page_state_follows_a_change_through_another_server(tmp_path: Path, visible: bool) -> None:
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path / "page"), MonitorOptions(open_browser=False, port=0))
    choice = MarkingsChoice(SettingsStore.in_dir(tmp_path / "shared"))
    choice.add_listener(monitor.markings_changed)
    MarkingsChoice(SettingsStore.in_dir(tmp_path / "shared")).set(not visible)
    choice.refresh()
    MarkingsChoice(SettingsStore.in_dir(tmp_path / "shared")).set(visible)
    choice.refresh()
    assert monitor.bus.phone.markings_visible is visible
