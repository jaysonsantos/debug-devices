"""Live tracking with a synthetic board and a camera that moves (the frames are warps with known homographies)."""

import io
import math

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from debug_devices_mcp.tracking import TRACK_WIDTH, LiveTracker, TrackingState, apply

# The synthetic board: invented parts on a board 200 x 150 mm, drawn at 10 px/mm.
BOARD_PX_PER_MM = 10
BOARD_WIDTH, BOARD_HEIGHT = 2000, 1500
SEED = 11
# The agent's snapshot shows the whole board at this size.
SNAPSHOT_WIDTH, SNAPSHOT_HEIGHT = 1568, 1176
# The phone screen in its natural portrait orientation; the preview shows the camera image turned by 90 degrees.
FRAME_WIDTH, FRAME_HEIGHT = 480, 1040
# The projected position must be this close to the truth (snapshot pixels).
TOLERANCE_PX = 6.0


def board_image() -> np.ndarray:
    rng = np.random.default_rng(SEED)
    image = Image.new("RGB", (BOARD_WIDTH, BOARD_HEIGHT), (20, 70, 40))
    draw = ImageDraw.Draw(image)
    for _ in range(420):
        x, y = rng.integers(0, BOARD_WIDTH - 60), rng.integers(0, BOARD_HEIGHT - 60)
        width, height = rng.integers(12, 90), rng.integers(8, 70)
        shade = int(rng.integers(40, 250))
        draw.rectangle((x, y, x + width, y + height), fill=(shade, shade, shade), outline=(230, 230, 200))
        if rng.random() < 0.5:
            draw.text((x + 2, y + 2), f"U{rng.integers(1, 999)}", fill=(255, 255, 255))
    for _ in range(160):
        x, y = rng.integers(0, BOARD_WIDTH), rng.integers(0, BOARD_HEIGHT)
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(200, 170, 60))
    return np.asarray(image)


BOARD = board_image()


def jpeg(pixels: np.ndarray) -> bytes:
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="JPEG", quality=90)
    return output.getvalue()


def snapshot() -> bytes:
    return jpeg(cv2.resize(BOARD, (SNAPSHOT_WIDTH, SNAPSHOT_HEIGHT), interpolation=cv2.INTER_AREA))


# Board pixels -> snapshot pixels (the agent's image).
BOARD_TO_SNAPSHOT = np.diag([SNAPSHOT_WIDTH / BOARD_WIDTH, SNAPSHOT_HEIGHT / BOARD_HEIGHT, 1.0])
# Board mm -> board pixels.
MM = np.diag([BOARD_PX_PER_MM, BOARD_PX_PER_MM, 1.0])


def view(center: tuple[float, float], scale: float, angle_deg: float) -> np.ndarray:
    """Board pixels -> frame pixels for a camera over `center`, `scale` frame px per board px, turned by the preview
    rotation (90 degrees) plus `angle_deg`."""
    angle = math.radians(90 + angle_deg)
    cos, sin = math.cos(angle) * scale, math.sin(angle) * scale
    to_origin = np.array([[1, 0, -center[0]], [0, 1, -center[1]], [0, 0, 1]])
    turn = np.array([[cos, -sin, 0], [sin, cos, 0], [0, 0, 1]])
    to_frame = np.array([[1, 0, FRAME_WIDTH / 2], [0, 1, FRAME_HEIGHT * 0.55], [0, 0, 1]])
    return to_frame @ turn @ to_origin


def frame(board_to_frame: np.ndarray) -> bytes:
    pixels = cv2.warpPerspective(BOARD, board_to_frame, (FRAME_WIDTH, FRAME_HEIGHT), borderValue=(0, 0, 0))
    # The app status text at the top left of the screen: it does not move with the board.
    cv2.putText(pixels, "zoom 1.0x torch off", (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    return jpeg(pixels)


# The whole board in view at registration: 0.5 frame px per board px.
START = view((1000, 750), 0.5, 0)


def truth(board_to_frame: np.ndarray, point_mm: tuple[float, float]) -> np.ndarray:
    """Where a board point is in a snapshot taken now: frame -> snapshot is fixed (the same camera)."""
    snapshot_to_frame = START @ np.linalg.inv(BOARD_TO_SNAPSHOT)
    board_px = apply(MM, np.array([point_mm]))
    return apply(np.linalg.inv(snapshot_to_frame) @ board_to_frame, board_px)[0]


def start() -> LiveTracker:
    tracker = LiveTracker.start("reg-1", BOARD_TO_SNAPSHOT @ MM, snapshot(), frame(START))
    assert isinstance(tracker, LiveTracker), tracker
    return tracker


POINTS_MM = [(30.0, 20.0), (100.0, 75.0), (170.0, 130.0)]


def test_start_finds_the_snapshot_in_the_frame() -> None:
    tracker = start()
    corners = np.array([(100.0, 100.0), (1400.0, 200.0), (800.0, 1000.0)])
    # The tracker works on frames scaled to TRACK_WIDTH.
    true_map = np.diag([TRACK_WIDTH / FRAME_WIDTH] * 2 + [1.0]) @ START @ np.linalg.inv(BOARD_TO_SNAPSHOT)
    error = np.abs(apply(tracker.snapshot_to_frame, corners) - apply(true_map, corners)).max()
    # In tracking frame pixels (1.5 per frame pixel at 720 px).
    assert error < TOLERANCE_PX


@pytest.mark.parametrize(
    ("name", "center", "scale", "angle"),
    [
        ("slide", (1080, 700), 0.5, 0),
        ("closer", (900, 800), 0.8, 0),
        ("turned", (1000, 750), 0.5, 25),
        ("closer and turned", (700, 600), 0.9, -15),
    ],
)
def test_tracking_follows_the_camera(name: str, center: tuple[float, float], scale: float, angle: float) -> None:
    tracker = start()
    moved = view(center, scale, angle)
    assert not tracker.update(frame(moved))
    assert tracker.following, name
    projected = apply(tracker.board_to_current(), np.array(POINTS_MM))
    expected = np.array([truth(moved, point) for point in POINTS_MM])
    assert np.abs(projected - expected).max() < TOLERANCE_PX, name


def test_far_moves_chain_through_the_last_frame() -> None:
    tracker = start()
    for step in range(1, 7):
        moved = view((1000 - 60 * step, 750), 0.5 + 0.12 * step, 3 * step)
        assert not tracker.update(frame(moved))
    assert tracker.following
    projected = apply(tracker.board_to_current(), np.array([POINTS_MM[0]]))[0]
    assert np.abs(projected - truth(moved, POINTS_MM[0])).max() < TOLERANCE_PX * 2


def test_a_covered_camera_loses_the_tracking() -> None:
    tracker = start()
    black = jpeg(np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8))
    lost = [tracker.update(black) for _ in range(3)]
    assert lost == [False, False, True]
    assert tracker.state is TrackingState.LOST


def test_another_scene_does_not_start() -> None:
    other = np.random.default_rng(3).integers(0, 255, (FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    result = LiveTracker.start("reg-2", BOARD_TO_SNAPSHOT @ MM, snapshot(), jpeg(other))
    assert isinstance(result, str)
    assert "does not match" in result
