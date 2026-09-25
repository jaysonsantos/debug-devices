"""Focus on a point: the pixel mapping of phone_focus, the tool, and the page route (POST /api/phone/focus)."""

import json
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent
from starlette.testclient import TestClient

from debug_devices_mcp.config import Settings
from debug_devices_mcp.focus import PointOutsideError, SnapshotGeometry, snapshot_focus_request
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore

from .test_orientation import quadrants
from .test_server import FakePhone, make_services, no_vision

FOCUS = "/v1/focus"
START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"
# Larger than the default max_side of phone_snapshot: the agent sees a scaled image.
PHOTO_WIDTH, PHOTO_HEIGHT = 4000, 3000


class FocusPhone(FakePhone):
    """The httpx fake phone with POST /v1/focus. `mode` "old": an app from before it (404); "outside": 400."""

    def __init__(self, mode: str = "ok") -> None:
        super().__init__()
        self.mode = mode
        self.status["focus"] = {"distance_diopters": 3.4, "state": "focused", "calibration": "approximate"}
        self.sent: list[dict[str, float]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != FOCUS:
            return super().handle(request)
        self.requests.append((request.method, FOCUS))
        if self.mode == "old":
            return httpx.Response(404, json={"error": "not_found", "message": "no route"})
        if self.mode == "outside":
            return httpx.Response(400, json={"error": "bad_request", "message": "outside the preview"})
        self.sent.append(json.loads(request.content))
        self.status["focus"] = {**self.status["focus"], "state": "scanning"}
        return httpx.Response(200, json=self.status)


def text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


# region: mapping


@pytest.mark.parametrize(
    ("flip_horizontal", "flip_vertical", "expected"),
    [
        (False, False, (0.25, 0.75)),
        (True, False, (0.75, 0.75)),
        (False, True, (0.25, 0.25)),
        (True, True, (0.75, 0.25)),
    ],
)
def test_snapshot_pixels_map_back_through_the_flips(
    flip_horizontal: bool, flip_vertical: bool, expected: tuple[float, float]
) -> None:
    orientation = SnapshotOrientation(flip_horizontal=flip_horizontal, flip_vertical=flip_vertical)
    geometry = SnapshotGeometry(width=800, height=600, orientation=orientation)
    request = snapshot_focus_request(200, 450, geometry)
    assert (request.snapshot_x, request.snapshot_y) == pytest.approx(expected)


def test_a_point_outside_the_snapshot_is_refused() -> None:
    geometry = SnapshotGeometry(width=800, height=600, orientation=SnapshotOrientation())
    with pytest.raises(PointOutsideError):
        snapshot_focus_request(801, 10, geometry)
    with pytest.raises(PointOutsideError):
        snapshot_focus_request(10, -1, geometry)
    # The edges are on the image.
    assert snapshot_focus_request(800, 600, geometry).snapshot_x == 1


# endregion: mapping

# region: tool


async def test_phone_focus_uses_the_last_snapshot_of_the_agent(settings: Settings) -> None:
    phone = FocusPhone()
    phone.snapshot = quadrants(PHOTO_WIDTH, PHOTO_HEIGHT)
    async with Client(build_server(make_services(settings, phone, no_vision()))) as client:
        early = await client.call_tool("phone_focus", {"x": 10, "y": 10})
        assert early.is_error
        assert "take a phone_snapshot first" in text(early)

        await client.call_tool("phone_snapshot_orientation", {"flip_horizontal": True})
        shot = await client.call_tool("phone_snapshot", {})
        info = json.loads(shot.content[0].text)
        assert info["width"] < PHOTO_WIDTH  # the agent sees a scaled image
        focused = await client.call_tool("phone_focus", {"x": info["width"] / 4, "y": info["height"] / 2})
        assert not focused.is_error, focused.content
        assert focused.structured_content is not None
        assert focused.structured_content["focus_state"] == "scanning"

        outside = await client.call_tool("phone_focus", {"x": info["width"] + 5, "y": 1})
        screen = await client.call_tool("phone_focus", {"x": 0.3, "y": 0.6, "source": "screen"})
        assert not screen.is_error, screen.content
        not_unit = await client.call_tool("phone_focus", {"x": 30, "y": 0.6, "source": "screen"})

    # The image was flipped left-right: a point at 1/4 of its width is at 3/4 of the true snapshot.
    assert phone.sent[0] == pytest.approx({"snapshot_x": 0.75, "snapshot_y": 0.5})
    assert phone.sent[1] == {"screen_x": 0.3, "screen_y": 0.6}
    assert len(phone.sent) == 2
    assert outside.is_error
    assert "outside the last snapshot" in text(outside)
    assert not_unit.is_error


@pytest.mark.parametrize(("mode", "message"), [("old", "update the phone app"), ("outside", "outside the preview")])
async def test_phone_focus_errors_of_the_app(settings: Settings, mode: str, message: str) -> None:
    async with Client(build_server(make_services(settings, FocusPhone(mode), no_vision()))) as client:
        result = await client.call_tool("phone_focus", {"x": 0.5, "y": 0.05, "source": "screen"})
    assert result.is_error
    assert message in text(result)


# endregion: tool

# region: page route


def page_client(settings: Settings, tmp_path: Path, phone: FakePhone) -> tuple[TestClient, Monitor]:
    services = make_services(settings, phone, no_vision())
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    server = build_server(services)
    monitor.instrument(server)
    return TestClient(create_app(monitor), base_url=BASE_URL), monitor


def test_page_click_runs_phone_focus_with_a_screen_point(settings: Settings, tmp_path: Path) -> None:
    phone = FocusPhone()
    client, monitor = page_client(settings, tmp_path, phone)
    response = client.post("/api/phone/focus", json={"screen_x": 0.4, "screen_y": 0.7})
    assert response.status_code == 200, response.text
    assert response.json()["status"]["focus"]["state"] == "scanning"
    assert phone.sent == [{"screen_x": 0.4, "screen_y": 0.7}]
    [call] = monitor.bus.calls()
    assert (call.tool, call.source) == ("phone_focus", "ui")
    for body in ({"screen_x": "0.4", "screen_y": 0.7}, {"screen_x": 1.2, "screen_y": 0.7}, {"screen_x": 0.4}):
        assert client.post("/api/phone/focus", json=body).status_code == 400
    assert len(phone.sent) == 1


def test_page_click_outside_the_preview_is_502_with_the_message(settings: Settings, tmp_path: Path) -> None:
    client, _ = page_client(settings, tmp_path, FocusPhone("outside"))
    response = client.post("/api/phone/focus", json={"screen_x": 0.5, "screen_y": 0.02})
    assert response.status_code == 502
    assert "outside the preview" in response.json()["error"]


# endregion: page route
