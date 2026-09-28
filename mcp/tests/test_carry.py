"""Automatic re-registration: after the board or the phone moved, a new phone_snapshot carries the registration over
by image features (no new part centres by eye), or says why not. It works without the live stream."""

import io
import json
import math
from pathlib import Path

import cv2
import numpy as np
from mcp import Client
from PIL import Image

from debug_devices_mcp.config import Settings
from debug_devices_mcp.pointing import NO_MATCH
from debug_devices_mcp.server import build_server
from debug_devices_mcp.tracking import apply

from .test_board_marking import board, to_photo
from .test_pointing import PHOTO_HEIGHT, PHOTO_WIDTH, board_file, register, registered_services  # noqa: F401
from .test_tracking import BOARD

# The move of the board between the two photos: turned 12 degrees and shifted (pixels of the photo).
TURN_DEG = 12.0
SHIFT = (60.0, -40.0)
TOLERANCE_PX = 8.0


def jpeg(pixels: np.ndarray) -> bytes:
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="JPEG", quality=92)
    return output.getvalue()


def first_photo() -> np.ndarray:
    return cv2.resize(BOARD, (PHOTO_WIDTH, PHOTO_HEIGHT), interpolation=cv2.INTER_AREA)


def move() -> np.ndarray:
    """The known move: a turn around the photo centre, then a shift."""
    angle = math.radians(TURN_DEG)
    cx, cy = PHOTO_WIDTH / 2, PHOTO_HEIGHT / 2
    cos, sin = math.cos(angle), math.sin(angle)
    turn = np.array([[cos, -sin, cx - cos * cx + sin * cy], [sin, cos, cy - sin * cx - cos * cy], [0, 0, 1]])
    return np.array([[1, 0, SHIFT[0]], [0, 1, SHIFT[1]], [0, 0, 1]]) @ turn


def moved_photo() -> np.ndarray:
    return cv2.warpPerspective(first_photo(), move(), (PHOTO_WIDTH, PHOTO_HEIGHT), borderValue=(20, 70, 40))


def box_center(box: dict) -> np.ndarray:
    return np.array(
        [(box["snapshot_x"] + box["width"] / 2) * PHOTO_WIDTH, (box["snapshot_y"] + box["height"] / 2) * PHOTO_HEIGHT]
    )


async def test_a_moved_board_carries_the_registration(settings: Settings, board_file: Path) -> None:  # noqa: F811
    services, phone = registered_services(settings, jpeg(first_photo()))
    parts = board().parts
    u7301 = np.array(to_photo(parts["U7301"].center.x, parts["U7301"].center.y))
    async with Client(build_server(services)) as client:
        registration = await register(client, board_file)
        # Without a move, a new snapshot keeps the registration as it is (nothing to carry).
        same = json.loads((await client.call_tool("phone_snapshot", {"max_side": 0})).content[0].text)
        assert same["registration"] is None

        await services.scene.mark_changed()  # the user moved the board
        phone.snapshot = jpeg(moved_photo())
        info = json.loads((await client.call_tool("phone_snapshot", {"max_side": 0})).content[0].text)
        carry = info["registration"]
        assert carry["carried"] is True, carry
        assert carry["carried_from"] == registration["registration_id"]
        assert carry["registration_id"] != registration["registration_id"]
        assert carry["quality"]["inliers"] >= 40
        assert carry["quality"]["error_px"] < 6
        carried = services.board.registrations[carry["registration_id"]]
        assert carried.stale is False
        assert carried.photo_id == info["capture_id"]
        # The carried registration puts U7301 where it is in the moved photo.
        pointed = await client.call_tool("phone_point_to", {"refdes": "U7301"})
    assert not pointed.is_error, pointed.content
    expected = apply(move(), np.array([u7301]))[0]
    assert np.abs(box_center(pointed.structured_content["boxes"][0]) - expected).max() < TOLERANCE_PX


async def test_another_scene_stays_stale_with_the_reason(settings: Settings, board_file: Path) -> None:  # noqa: F811
    services, phone = registered_services(settings, jpeg(first_photo()))
    async with Client(build_server(services)) as client:
        registration = await register(client, board_file)
        await services.scene.mark_changed()
        phone.snapshot = jpeg(np.random.default_rng(5).integers(0, 255, (PHOTO_HEIGHT, PHOTO_WIDTH, 3), dtype=np.uint8))
        info = json.loads((await client.call_tool("phone_snapshot", {"max_side": 0})).content[0].text)
        refused = await client.call_tool("phone_point_to", {"refdes": "U7301"})
    carry = info["registration"]
    assert carry["carried"] is False
    assert carry["registration_id"] == registration["registration_id"]
    assert carry["note"] == NO_MATCH
    assert services.board.registrations[registration["registration_id"]].stale is True
    assert refused.is_error
