"""phone_point_to: boxes in view, arrows outside, the other side, and live tracking through the MCP server."""

import io
from datetime import timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from mcp import Client
from PIL import Image

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.model import Box, Part, Point
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.pointer import OTHER_SIDE_MESSAGE, plan, true_angle
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.tracking import apply

from .conftest import FakeRunner
from .test_board import fixture
from .test_board_marking import board, to_photo
from .test_highlight import OverlayPhone
from .test_server import make_services, no_vision
from .test_tracking import BOARD, FRAME_HEIGHT, FRAME_WIDTH, jpeg, view

# The synthetic registration: the markings.json board on a 1200 x 1000 px photo (test_board_marking.to_photo).
PHOTO_WIDTH, PHOTO_HEIGHT = 1200, 1000
REFERENCE_PARTS = ("J4", "Q12", "TP9", "C8850", "R10")
TOLERANCE_PX = 8.0
MM = 10.0


def part(name: str, x: float, y: float, side: Side = Side.TOP, size: float = 4.0) -> Part:
    half = size / 2
    return Part(
        name=name,
        side=side,
        mounting="smd",
        center=Point(x=x, y=y),
        box=Box(min_x=x - half, min_y=y - half, max_x=x + half, max_y=y + half),
        box_from_pins=False,
        rotation_deg=None,
        mfgcode="",
        pin_count=2,
        nets=[],
    )


# region: plan


def test_plan_boxes_arrows_and_the_other_side() -> None:
    # 10 px per mm; the view is 1000 x 800 px = 100 x 80 mm.
    matrix = np.diag([MM, MM, 1.0])
    parts = [
        part("C1", 50, 40),
        part("J4", 150, 40),  # 50 mm right of the view edge
        part("U2", 50, -30),  # 30 mm above the view
        part("R9", 20, 20, Side.BOTTOM),
    ]
    result = plan(parts, Side.TOP, matrix, (1000, 800), SnapshotOrientation())
    [box] = result.boxes
    assert box.label == "C1"
    assert (box.x, box.y, box.width, box.height) == pytest.approx((480, 380, 40, 40))
    right, up = result.arrows
    assert right.angle_deg == pytest.approx(0)
    assert right.label == "J4 ~5 cm"
    assert up.angle_deg == pytest.approx(270)
    assert up.label == "U2 ~3 cm"
    states = {target.refdes: target for target in result.targets}
    assert states["C1"].in_view is True
    assert states["J4"].in_view is False
    assert states["J4"].distance_cm == pytest.approx(5.0)
    assert states["R9"].in_view is None
    assert states["R9"].message == OTHER_SIDE_MESSAGE
    # The messages give the distance and the arrow, never left or right words.
    assert all("left" not in target.message and "right" not in target.message for target in result.targets)


@pytest.mark.parametrize(
    ("flip_horizontal", "flip_vertical", "shown", "expected"),
    [(False, False, 30, 30), (True, False, 30, 150), (False, True, 30, 330), (True, True, 30, 210)],
)
def test_arrow_angles_go_to_the_true_orientation(
    flip_horizontal: bool, flip_vertical: bool, shown: float, expected: float
) -> None:
    orientation = SnapshotOrientation(flip_horizontal=flip_horizontal, flip_vertical=flip_vertical)
    assert true_angle(shown, orientation) == pytest.approx(expected)


def test_a_close_part_has_a_decimal_distance() -> None:
    result = plan([part("C7", 104, 40)], Side.TOP, np.diag([MM, MM, 1.0]), (1000, 800), SnapshotOrientation())
    assert result.arrows[0].label == "C7 ~0.4 cm"


# endregion: plan

# region: tool


def registered_services(settings: Settings, snapshot: bytes) -> tuple[Services, OverlayPhone]:
    phone = OverlayPhone()
    phone.snapshot = snapshot
    services = make_services(settings, phone, no_vision())
    runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture("markings.json"), stderr=b""))
    services.board = BoardSession.create(runner, LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5)))
    return services, phone


def pairs(shift_x: float = 0.0) -> list[dict]:
    parts = board().parts
    result = []
    for name in REFERENCE_PARTS:
        x, y = to_photo(parts[name].center.x, parts[name].center.y)
        result.append({"refdes": name, "x_px": x + shift_x, "y_px": y})
    return result


async def register(client: Client, board_file: Path, shift_x: float = 0.0) -> dict:
    await client.call_tool("board_open", {"path": str(board_file)})
    await client.call_tool("phone_snapshot", {})
    registered = await client.call_tool(
        "board_register_photo",
        {"side": "top", "photo_width_px": PHOTO_WIDTH, "photo_height_px": PHOTO_HEIGHT, "pairs": pairs(shift_x)},
    )
    assert not registered.is_error, registered.content
    assert registered.structured_content is not None
    return registered.structured_content


@pytest.fixture
def board_file(tmp_path: Path) -> Path:
    path = tmp_path / "markings.cad"
    path.write_text("the fake obv-dump does not read this")
    return path


def blank_photo() -> bytes:
    return jpeg(np.full((PHOTO_HEIGHT, PHOTO_WIDTH, 3), 90, dtype=np.uint8))


async def test_point_to_without_tracking(settings: Settings, board_file: Path) -> None:
    services, _ = registered_services(settings, blank_photo())
    async with Client(build_server(services)) as client:
        early = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        # The board is 700 px further right in this photo: the parts at the left are outside it.
        registration = await register(client, board_file, shift_x=-700)
        in_view = await client.call_tool("phone_point_to", {"refdes": ["TP9"]})
        outside = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        other = await client.call_tool("phone_point_to", {"refdes": "U7303"})
        unknown = await client.call_tool("phone_point_to", {"refdes": "X99"})

    assert early.is_error
    assert "no photo registration" in early.content[0].text or "open" in early.content[0].text
    assert registration["tracking"].startswith("no live tracking")
    assert in_view.structured_content["count"] == 1
    result = outside.structured_content
    assert result is not None
    assert result["count"] == 0
    [arrow] = result["arrows"]
    assert 90 < arrow["angle_deg"] < 270  # toward the left of the image
    assert arrow["label"].startswith("U7301 ~")
    assert result["tracking"] is False
    target = result["targets"][0]
    assert target["in_view"] is False
    assert target["distance_cm"] > 0
    assert other.structured_content["targets"][0]["message"] == OTHER_SIDE_MESSAGE
    assert other.structured_content["arrows"] == []
    assert unknown.is_error


# endregion: tool

# region: live tracking


def snapshot_image() -> np.ndarray:
    """The synthetic board texture as the agent's 1200 x 1000 snapshot."""
    return cv2.resize(BOARD, (PHOTO_WIDTH, PHOTO_HEIGHT), interpolation=cv2.INTER_AREA)


SNAPSHOT = snapshot_image()


def frame_of(snapshot_to_frame: np.ndarray) -> bytes:
    pixels = cv2.warpPerspective(SNAPSHOT, snapshot_to_frame, (FRAME_WIDTH, FRAME_HEIGHT))
    return jpeg(pixels)


def snapshot_view(center: tuple[float, float], scale: float, angle: float) -> np.ndarray:
    """Snapshot pixels -> frame pixels (the test_tracking camera, on the snapshot image)."""
    to_board = np.diag([BOARD.shape[1] / PHOTO_WIDTH, BOARD.shape[0] / PHOTO_HEIGHT, 1.0])
    return view(center, scale, angle) @ to_board


START_VIEW = snapshot_view((1000, 750), 0.5, 0)


def box_center(box: dict) -> np.ndarray:
    return np.array(
        [(box["snapshot_x"] + box["width"] / 2) * PHOTO_WIDTH, (box["snapshot_y"] + box["height"] / 2) * PHOTO_HEIGHT]
    )


async def test_boxes_and_arrows_follow_the_phone(
    settings: Settings, board_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("debug_devices_mcp.pointing.MIN_POST_INTERVAL_S", 0.0)
    output = io.BytesIO()
    Image.fromarray(SNAPSHOT).save(output, format="JPEG", quality=92)
    services, phone = registered_services(settings, output.getvalue())
    parts = board().parts
    u7301 = np.array(to_photo(parts["U7301"].center.x, parts["U7301"].center.y))
    async with Client(build_server(services)) as client:
        services.scene.latest_frame = frame_of(START_VIEW)
        registration = await register(client, board_file)
        assert registration["tracking"].startswith("live tracking on"), registration["tracking"]
        first = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        assert first.structured_content["tracking"] is True
        assert np.abs(box_center(first.structured_content["boxes"][0]) - u7301).max() < TOLERANCE_PX

        # The phone slides and turns: the box follows to where U7301 is in a snapshot taken now.
        moved = snapshot_view((1100, 650), 0.6, 12)
        await services.scene.frame(frame_of(moved))
        [box] = phone.sent[-1]
        expected = apply(np.linalg.inv(START_VIEW) @ moved, np.array([u7301]))[0]
        assert np.abs(box_center(box) - expected).max() < TOLERANCE_PX

        # A scene change that the tracker follows keeps the registration: pointing still works.
        await services.scene.mark_changed()
        assert services.board.registrations[registration["registration_id"]].stale is False
        again = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        assert not again.is_error, again.content

        # The phone moves far to the right: U7301 leaves the view, and an arrow points back toward it.
        far = snapshot_view((1500, 750), 0.9, 0)
        for step in (snapshot_view((1250, 700), 0.7, 0), far):
            await services.scene.frame(frame_of(step))
        pointed = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        state = pointed.structured_content
        assert state["tracking"] is True
        # U7301 is now at about x -383 px of a snapshot taken now: left of the view, so the arrow points left
        # in the image (angle about 180 degrees; slightly down, because it is also below the center).
        assert state["targets"][0]["in_view"] is False
        [arrow] = state["arrows"]
        assert arrow["label"].startswith("U7301 ~")
        expected_angle = np.degrees(np.arctan2(762.8 - PHOTO_HEIGHT / 2, -382.8 - PHOTO_WIDTH / 2)) % 360
        assert abs(arrow["angle_deg"] - expected_angle) < 3
        assert phone.arrows_sent[-1][0]["label"].startswith("U7301 ~")

        # The camera is covered: the tracking is lost, the registration is stale, and the overlay is cleared.
        black = jpeg(np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8))
        for _ in range(3):
            await services.scene.frame(black)
        assert services.board.registrations[registration["registration_id"]].stale is True
        assert services.pointing.state == "lost"
        assert phone.sent[-1] == []
        refused = await client.call_tool("phone_point_to", {"refdes": "U7301"})
    assert refused.is_error
    text = refused.content[0].text
    assert "moved since your last phone_snapshot" in text or "register" in text


# endregion: live tracking
