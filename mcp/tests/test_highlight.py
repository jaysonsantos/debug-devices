"""Highlight boxes: the mapping, the annotated image, phone_highlight, board_locate_in_photo highlight, the page."""

import base64
import io
import json
from datetime import timedelta
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image
from starlette.testclient import TestClient

from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.focus import SnapshotGeometry
from debug_devices_mcp.highlight import (
    BOX_COLOR,
    BoxOutsideError,
    PixelBox,
    draw_boxes,
    overlay_box,
    scale_boxes,
)
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore

from .conftest import FakeRunner, make_jpeg
from .test_board import fixture
from .test_board_marking import PX_PER_MM, board, to_photo
from .test_orientation import quadrants
from .test_server import FakePhone, make_services, no_vision

OVERLAY = "/v1/overlay"
START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"
# Larger than the default max_side of phone_snapshot (1568): the agent sees 1568x1176.
PHOTO_WIDTH, PHOTO_HEIGHT = 4000, 3000
AGENT_WIDTH, AGENT_HEIGHT = 1568, 1176


class OverlayPhone(FakePhone):
    """The httpx fake phone with POST /v1/overlay. `old`: an app from before it (404)."""

    def __init__(self, old: bool = False) -> None:
        super().__init__()
        self.old = old
        self.status["overlay_boxes"] = 0
        self.sent: list[list[dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != OVERLAY:
            return super().handle(request)
        self.requests.append((request.method, OVERLAY))
        if self.old:
            return httpx.Response(404, json={"error": "not_found", "message": "no route"})
        boxes = json.loads(request.content)["boxes"]
        self.sent.append(boxes)
        self.status["overlay_boxes"] = len(boxes)
        return httpx.Response(200, json=self.status)


def text(result: CallToolResult) -> str:
    return " ".join(block.text for block in result.content if isinstance(block, TextContent))


def image_of(result: CallToolResult) -> Image.Image:
    [block] = [block for block in result.content if isinstance(block, ImageContent)]
    return Image.open(io.BytesIO(base64.b64decode(block.data)))


def is_green(pixel: tuple[int, int, int]) -> bool:
    return pixel[1] > 180 and pixel[0] < 90 and pixel[2] < 120


def geometry(flip_horizontal: bool = False, flip_vertical: bool = False) -> SnapshotGeometry:
    orientation = SnapshotOrientation(flip_horizontal=flip_horizontal, flip_vertical=flip_vertical)
    return SnapshotGeometry(width=800, height=600, orientation=orientation)


# region: mapping


@pytest.mark.parametrize(
    ("flip_horizontal", "flip_vertical", "expected"),
    [
        (False, False, (0.1, 0.2)),
        (True, False, (0.65, 0.2)),
        (False, True, (0.1, 0.5)),
        (True, True, (0.65, 0.5)),
    ],
)
def test_boxes_map_back_through_the_flips(
    flip_horizontal: bool, flip_vertical: bool, expected: tuple[float, float]
) -> None:
    box = PixelBox(x=80, y=120, width=200, height=180, label="U730")
    mapped = overlay_box(box, geometry(flip_horizontal, flip_vertical))
    assert (mapped.snapshot_x, mapped.snapshot_y) == pytest.approx(expected)
    assert (mapped.width, mapped.height) == pytest.approx((0.25, 0.3))
    assert mapped.label == "U730"


def test_boxes_are_cut_at_the_image_edge_and_refused_outside() -> None:
    mapped = overlay_box(PixelBox(x=700, y=-50, width=200, height=100), geometry(flip_horizontal=True))
    # Cut to x 700-800, y 0-50; the left-right flip puts it at the left edge.
    assert (mapped.snapshot_x, mapped.snapshot_y, mapped.width, mapped.height) == pytest.approx((0, 0, 0.125, 50 / 600))
    with pytest.raises(BoxOutsideError):
        overlay_box(PixelBox(x=801, y=10, width=5, height=5), geometry())


def test_boxes_of_the_same_photo_at_another_size_are_scaled() -> None:
    [scaled] = scale_boxes([PixelBox(x=400, y=300, width=40, height=30)], (4000, 3000), (800, 600))
    assert (scaled.x, scaled.y, scaled.width, scaled.height) == pytest.approx((80, 60, 8, 6))
    with pytest.raises(BoxOutsideError, match="another shape"):
        scale_boxes([PixelBox(x=1, y=1, width=1, height=1)], (4000, 3000), (800, 800))


def test_annotated_image_has_the_green_box() -> None:
    annotated = draw_boxes(make_jpeg(400, 300), [PixelBox(x=100, y=100, width=120, height=80, label="C12")])
    with Image.open(io.BytesIO(annotated)) as image:
        assert image.size == (400, 300)
        rgb = image.convert("RGB")
        assert is_green(rgb.getpixel((101, 140)))  # the left edge
        assert not is_green(rgb.getpixel((160, 140)))  # the inside stays
    assert BOX_COLOR[1] > 200


# endregion: mapping

# region: tool


async def test_phone_highlight_maps_the_boxes_and_returns_the_annotated_snapshot(settings: Settings) -> None:
    phone = OverlayPhone()
    phone.snapshot = quadrants(PHOTO_WIDTH, PHOTO_HEIGHT)
    async with Client(build_server(make_services(settings, phone, no_vision()))) as client:
        early = await client.call_tool("phone_highlight", {"boxes": [{"x": 1, "y": 1, "width": 5, "height": 5}]})
        await client.call_tool("phone_snapshot_orientation", {"flip_horizontal": True})
        shot = await client.call_tool("phone_snapshot", {})
        assert json.loads(shot.content[0].text)["width"] == AGENT_WIDTH
        box = {"x": 0, "y": 0, "width": AGENT_WIDTH / 4, "height": AGENT_HEIGHT / 4, "label": "U730"}
        shown = await client.call_tool("phone_highlight", {"boxes": [box]})
        too_many = await client.call_tool("phone_highlight", {"boxes": [box] * 9})
        both = await client.call_tool("phone_highlight", {"boxes": [box], "clear": True})
        neither = await client.call_tool("phone_highlight", {})
        cleared = await client.call_tool("phone_highlight", {"clear": True})

    assert early.is_error
    assert "take a phone_snapshot first" in text(early)
    assert not shown.is_error, shown.content
    # Flip H: the top left quarter of the agent's image is the top right quarter of the true snapshot.
    [sent] = phone.sent[0]
    assert (sent["snapshot_x"], sent["snapshot_y"], sent["width"], sent["height"]) == pytest.approx(
        (0.75, 0, 0.25, 0.25)
    )
    assert sent["label"] == "U730"
    result = shown.structured_content
    assert result is not None
    assert (result["count"], result["overlay_boxes"]) == (1, 1)
    assert "your estimate" in result["note"]
    with image_of(shown) as annotated:
        assert annotated.size == (AGENT_WIDTH, AGENT_HEIGHT)
        assert is_green(annotated.convert("RGB").getpixel((1, AGENT_HEIGHT // 8)))
    assert too_many.is_error
    assert both.is_error
    assert neither.is_error
    assert not cleared.is_error
    assert phone.sent[-1] == []
    assert cleared.structured_content is not None
    assert cleared.structured_content["count"] == 0


async def test_old_app_says_update(settings: Settings) -> None:
    phone = OverlayPhone(old=True)
    async with Client(build_server(make_services(settings, phone, no_vision()))) as client:
        await client.call_tool("phone_snapshot", {})
        result = await client.call_tool("phone_highlight", {"boxes": [{"x": 1, "y": 1, "width": 5, "height": 5}]})
    assert result.is_error
    assert "update the phone app" in text(result)


# endregion: tool

# region: scene change


async def test_a_scene_change_refuses_old_positions_until_a_new_snapshot(settings: Settings) -> None:
    phone = OverlayPhone()
    services = make_services(settings, phone, no_vision())
    box = {"x": 10, "y": 10, "width": 20, "height": 20, "label": "R1"}
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {})
        assert not (await client.call_tool("phone_highlight", {"boxes": [box]})).is_error
        await services.scene.mark_changed()
        # The server cleared the phone boxes.
        assert phone.sent[-1] == []
        status = await client.call_tool("phone_status", {})
        refused = [
            await client.call_tool("phone_highlight", {"boxes": [box]}),
            await client.call_tool("phone_focus", {"x": 10, "y": 10}),
        ]
        screen_focus = await client.call_tool("phone_focus", {"x": 0.5, "y": 0.5, "source": "screen"})
        await client.call_tool("phone_snapshot", {})
        again = await client.call_tool("phone_highlight", {"boxes": [box]})

    assert status.structured_content is not None
    assert status.structured_content["scene_changed"] is True
    assert status.structured_content["scene_changed_at"] is not None
    for result in refused:
        assert result.is_error
        assert "the board or the phone moved since your last phone_snapshot" in text(result)
    # A screen point does not refer to a photo. (The httpx fake phone has no /v1/focus: 404, not a scene error.)
    assert "moved" not in text(screen_focus)
    assert not again.is_error, again.content


async def test_own_commands_ask_for_a_new_reference(settings: Settings) -> None:
    services = make_services(settings, OverlayPhone(), no_vision())
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {})
        services.scene.reference_wanted = False
        generation = services.scene.generation
        await client.call_tool("phone_zoom", {"step": "in"})
    assert services.scene.reference_wanted
    assert services.scene.generation > generation
    assert services.scene.settle_until > 0


# endregion: scene change

# region: board


async def test_locate_in_photo_highlights_the_parts_and_goes_stale(settings: Settings, tmp_path: Path) -> None:
    marking_file = tmp_path / "markings.cad"
    marking_file.write_text("the fake obv-dump does not read this")
    phone = OverlayPhone()
    services = make_services(settings, phone, no_vision())
    runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture("markings.json"), stderr=b""))
    services.board = BoardSession.create(runner, LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5)))
    server = build_server(services)
    parts = board().parts
    pairs = []
    for name in ("J4", "Q12", "TP9", "C8850", "R10"):
        x, y = to_photo(parts[name].center.x, parts[name].center.y)
        pairs.append({"refdes": name, "x_px": x, "y_px": y})
    phone.snapshot = make_jpeg(1200, 1000)
    async with Client(server) as client:
        await client.call_tool("board_open", {"path": str(marking_file)})
        await client.call_tool("phone_snapshot", {})
        registered = await client.call_tool(
            "board_register_photo", {"side": "top", "photo_width_px": 1200, "photo_height_px": 1000, "pairs": pairs}
        )
        registration_id = registered.structured_content["registration_id"]
        args = {"registration_id": registration_id, "refdes": ["U7301"], "highlight": True}
        located = await client.call_tool("board_locate_in_photo", args)
        await services.scene.mark_changed()
        stale = await client.call_tool("board_locate_in_photo", {**args, "highlight": False})
        await client.call_tool("phone_snapshot", {})
        still_stale = await client.call_tool("board_locate_in_photo", {**args, "highlight": False})

    assert not located.is_error, located.content
    result = located.structured_content
    assert result is not None
    assert result["highlight"]["count"] == 1
    [sent] = phone.sent[0]
    assert sent["label"] == "U7301"
    center_x, center_y = to_photo(parts["U7301"].center.x, parts["U7301"].center.y)
    assert sent["snapshot_x"] + sent["width"] / 2 == pytest.approx(center_x / 1200, abs=2 / 1200)
    assert sent["snapshot_y"] + sent["height"] / 2 == pytest.approx(center_y / 1000, abs=2 / 1000)
    box = parts["U7301"].box
    assert sent["width"] * 1200 == pytest.approx((box.max_x - box.min_x) * PX_PER_MM, abs=1)
    assert stale.is_error
    assert "moved since your last phone_snapshot" in text(stale)
    # After a new snapshot the registration stays stale: the agent must register the new photo.
    assert still_stale.is_error
    assert "board_register_photo again" in text(still_stale)


# endregion: board

# region: page


def test_the_page_gets_the_boxes_and_clears_them(settings: Settings, tmp_path: Path) -> None:
    phone = OverlayPhone()
    services = make_services(settings, phone, no_vision())
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    server = build_server(services)
    monitor.instrument(server)
    services.scene.add_listener(monitor.scene_changed)
    client = TestClient(create_app(monitor), base_url=BASE_URL)
    assert client.post("/api/phone/snapshot").status_code == 200
    box = {"x": 10, "y": 10, "width": 20, "height": 20, "label": "R1"}

    async def highlight() -> None:
        await monitor.call_from_ui("phone_highlight", {"boxes": [box]})

    anyio.run(highlight)
    assert len(monitor.bus.phone.highlights) == 1
    cleared = client.post("/api/phone/highlight/clear")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["highlights"] == []
    assert phone.sent[-1] == []

    anyio.run(highlight)
    anyio.run(services.scene.mark_changed)
    assert monitor.bus.phone.highlights == []
    assert monitor.bus.phone.scene_changed_at is not None


# endregion: page
