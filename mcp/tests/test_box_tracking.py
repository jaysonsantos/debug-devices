"""Plain phone_highlight boxes follow the board: their own live tracker from the snapshot at phone_highlight, no
board registration. Synthetic frames (test_tracking) and the fake phone only."""

import io
from pathlib import Path

import numpy as np
import pytest
from mcp import Client
from mcp.types import CallToolResult
from PIL import Image

from debug_devices_mcp.config import Settings
from debug_devices_mcp.pointing import BOXES_TRACKING_ON, NO_FRAMES, TRACKING_LOST_NOTE
from debug_devices_mcp.server import SCENE_CLEARED_NOTE, Services, build_server
from debug_devices_mcp.tracking import TrackingState, apply
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import SettingsStore
from debug_devices_mcp.ui.setup import connect_services

from .test_highlight import OverlayPhone
from .test_markings import START, VisiblePhone, services_for
from .test_pointing import (
    SNAPSHOT,
    START_VIEW,
    TOLERANCE_PX,
    board_file,  # noqa: F401
    box_center,
    frame_of,
    register,
    registered_services,
    snapshot_view,
)
from .test_server import make_services, no_vision
from .test_tracking import FRAME_HEIGHT, FRAME_WIDTH, jpeg

# A box around the middle of the snapshot (pixels of the 1200 x 1000 px snapshot), and one near its left edge.
MIDDLE = {"x": 560, "y": 470, "width": 80, "height": 60, "label": "U1 middle"}
LEFT = {"x": 100, "y": 470, "width": 80, "height": 60, "label": "C9 left"}
# The phone slides to the right: the board moves left in a snapshot taken now.
SHIFTED = snapshot_view((1100, 750), 0.5, 0)
# Far to the right: the left box leaves the view.
FAR_STEPS = (snapshot_view((1250, 750), 0.5, 0), snapshot_view((1500, 750), 0.5, 0))


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("debug_devices_mcp.pointing.MIN_POST_INTERVAL_S", 0.0)


def snapshot_jpeg() -> bytes:
    output = io.BytesIO()
    Image.fromarray(SNAPSHOT).save(output, format="JPEG", quality=92)
    return output.getvalue()


def black_frame() -> bytes:
    return jpeg(np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8))


def center_of(box: dict) -> np.ndarray:
    return np.array([box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])


def moved_center(box: dict, view: np.ndarray) -> np.ndarray:
    """Where the center of a box of the start snapshot is in a snapshot taken from `view`."""
    return apply(np.linalg.inv(START_VIEW) @ view, np.array([center_of(box)]))[0]


def texts(result: CallToolResult) -> list[str]:
    return [block.text for block in result.content if block.type == "text"]


def tracked_services(settings: Settings, phone: OverlayPhone | None = None) -> tuple[Services, OverlayPhone]:
    phone = phone or OverlayPhone()
    phone.snapshot = snapshot_jpeg()
    services = make_services(settings, phone, no_vision())
    services.scene.latest_frame = frame_of(START_VIEW)
    return services, phone


async def highlight(client: Client, *boxes: dict) -> dict:
    await client.call_tool("phone_snapshot", {"max_side": 0})
    result = await client.call_tool("phone_highlight", {"boxes": list(boxes)})
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def test_a_shifted_frame_moves_the_boxes(settings: Settings, tmp_path: Path) -> None:
    services, phone = tracked_services(settings)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    connect_services(monitor, services)
    async with Client(build_server(services)) as client:
        result = await highlight(client, MIDDLE, LEFT)
        assert result["tracking"] == BOXES_TRACKING_ON
        assert services.pointing.state is TrackingState.FOLLOWING
        assert monitor.bus.phone.tracking == TrackingState.FOLLOWING
        tags = [box["tag"] for box in phone.sent[-1]]

        await services.scene.frame(frame_of(SHIFTED))
    # On the phone and in the page state: moved by the shift, with the same labels and tags.
    sent = phone.sent[-1]
    assert [box["label"] for box in sent] == [MIDDLE["label"], LEFT["label"]]
    assert [box["tag"] for box in sent] == tags
    for box, given in zip(sent, (MIDDLE, LEFT), strict=True):
        assert np.abs(box_center(box) - moved_center(given, SHIFTED)).max() < TOLERANCE_PX
    page = [box.model_dump() for box in monitor.bus.phone.highlights]
    assert np.abs(box_center(page[0]) - moved_center(MIDDLE, SHIFTED)).max() < TOLERANCE_PX
    # The shift is about 60 px to the left (the phone moved 100 board px = 60 snapshot px to the right).
    assert moved_center(MIDDLE, SHIFTED)[0] == pytest.approx(center_of(MIDDLE)[0] - 60, abs=1)


async def test_a_box_outside_the_view_becomes_an_arrow(settings: Settings) -> None:
    services, phone = tracked_services(settings)
    async with Client(build_server(services)) as client:
        await highlight(client, MIDDLE, LEFT)
        left_tag = phone.sent[-1][1]["tag"]
        for step in FAR_STEPS:
            await services.scene.frame(frame_of(step))
    assert moved_center(LEFT, FAR_STEPS[-1])[0] < 0
    assert [box["label"] for box in phone.sent[-1]] == [MIDDLE["label"]]
    [arrow] = phone.arrows_sent[-1]
    assert arrow["label"] == LEFT["label"]
    # The arrow keeps the tag of its box (C3): the same tag and colour on the phone and the page.
    assert arrow["tag"] == left_tag
    # Left in the image: about 180 degrees on the still (no turn, no flips in the fake).
    assert abs(arrow["angle_deg"] - 180) < 5
    assert [arrow.label for arrow in services.arrows] == [LEFT["label"]]


async def test_a_frame_without_a_match_clears_the_boxes_with_a_note(settings: Settings) -> None:
    services, phone = tracked_services(settings)
    async with Client(build_server(services)) as client:
        await highlight(client, MIDDLE)
        for _ in range(3):
            await services.scene.frame(black_frame())
        assert phone.sent[-1] == []
        assert services.highlights == []
        assert services.pointing.boxes is None
        assert services.pointing.state is TrackingState.OFF
        # The next phone tool result says so one time.
        first = await client.call_tool("phone_status", {})
        second = await client.call_tool("phone_status", {})
    assert texts(first)[-1] == TRACKING_LOST_NOTE
    assert TRACKING_LOST_NOTE not in texts(second)


async def test_a_scene_change_keeps_the_followed_boxes(settings: Settings) -> None:
    services, phone = tracked_services(settings)
    async with Client(build_server(services)) as client:
        await highlight(client, MIDDLE)
        await services.scene.frame(frame_of(SHIFTED))
        before = list(services.highlights)
        await services.scene.mark_changed()
        assert services.highlights == before
        assert len(phone.sent[-1]) == 1
        # New boxes still need a fresh snapshot: they are pixels of a photo from before the move.
        refused = await client.call_tool("phone_highlight", {"boxes": [MIDDLE]})
        assert refused.is_error
        status = await client.call_tool("phone_status", {})
    assert services.pointing.boxes is not None
    assert SCENE_CLEARED_NOTE not in texts(status)


async def test_no_stream_means_no_tracking(settings: Settings) -> None:
    services, phone = tracked_services(settings)
    services.scene.latest_frame = None
    async with Client(build_server(services)) as client:
        result = await highlight(client, MIDDLE)
        assert result["tracking"] == NO_FRAMES
        assert result["tracking"].startswith("no live tracking")
        # Without tracking, a move clears the boxes as before, and the next phone tool result says so.
        await services.scene.mark_changed()
        status = await client.call_tool("phone_status", {})
    assert phone.sent[-1] == []
    assert texts(status)[-1] == SCENE_CLEARED_NOTE


async def test_a_snapshot_that_does_not_match_the_stream_gives_the_reason(settings: Settings) -> None:
    services, _ = tracked_services(settings)
    services.scene.latest_frame = black_frame()
    async with Client(build_server(services)) as client:
        result = await highlight(client, MIDDLE)
    assert result["tracking"].startswith("no live tracking: the snapshot does not match the live screen")
    assert services.pointing.boxes is None


async def test_new_boxes_and_clear_replace_the_followed_boxes(settings: Settings) -> None:
    services, phone = tracked_services(settings)
    async with Client(build_server(services)) as client:
        await highlight(client, MIDDLE)
        first = services.pointing.boxes
        await highlight(client, LEFT)
        assert services.pointing.boxes is not first
        assert services.pointing.boxes is not None
        await services.scene.frame(frame_of(SHIFTED))
        assert [box["label"] for box in phone.sent[-1]] == [LEFT["label"]]
        await client.call_tool("phone_highlight", {"clear": True})
        await services.scene.frame(frame_of(START_VIEW))
    assert services.pointing.boxes is None
    assert phone.sent[-1] == []
    assert services.pointing.state is TrackingState.OFF


async def test_point_to_replaces_the_boxes_and_the_registration_tracker_works(
    settings: Settings,
    board_file: Path,  # noqa: F811
) -> None:
    services, phone = registered_services(settings, snapshot_jpeg())
    services.scene.latest_frame = frame_of(START_VIEW)
    async with Client(build_server(services)) as client:
        registration = await register(client, board_file)
        assert registration["tracking"].startswith("live tracking on"), registration["tracking"]
        result = await client.call_tool("phone_highlight", {"boxes": [MIDDLE]})
        assert result.structured_content["tracking"] == BOXES_TRACKING_ON
        # Both trackers follow the same frame; the registration stays valid after the move.
        await services.scene.frame(frame_of(SHIFTED))
        assert services.pointing.tracked(registration["registration_id"])
        await services.scene.mark_changed()
        services.guard_registration(registration["registration_id"])
        assert services.board.registrations[registration["registration_id"]].stale is False

        pointed = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        assert not pointed.is_error, pointed.content
        assert services.pointing.boxes is None
        assert pointed.structured_content["tracking"] is True
        before = box_center(phone.sent[-1][0])
        await services.scene.frame(frame_of(START_VIEW))
    # The pointing box follows the move back (the plain box is gone).
    [box] = phone.sent[-1]
    assert box["label"] == "U7301"
    expected = apply(np.linalg.inv(SHIFTED) @ START_VIEW, np.array([before]))[0]
    assert np.abs(box_center(box) - expected).max() < TOLERANCE_PX


async def test_an_old_app_with_hidden_markings_gets_no_tracked_boxes(settings: Settings, tmp_path: Path) -> None:
    phone = VisiblePhone(old_visible=True)
    phone.snapshot = snapshot_jpeg()
    services = services_for(settings, phone, tmp_path)
    services.scene.latest_frame = frame_of(START_VIEW)
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_status", {})
        await services.set_markings(False)
        result = await highlight(client, MIDDLE)
        assert result["tracking"] == BOXES_TRACKING_ON
        await services.scene.frame(frame_of(SHIFTED))
    # The server keeps the moved boxes for the page and for later; the old app gets none.
    assert phone.sent[-1] == []
    [kept] = services.highlights
    assert np.abs(box_center(kept.model_dump()) - moved_center(MIDDLE, SHIFTED)).max() < TOLERANCE_PX
    assert phone.arrows_sent[-1] == []
