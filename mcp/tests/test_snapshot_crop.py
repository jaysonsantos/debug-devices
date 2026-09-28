"""phone_snapshot with `crop`: an extra full-resolution image of an area, enlarged, from the same turned still."""

import base64
import io
import json

import pytest
from mcp import Client
from PIL import Image
from pydantic import ValidationError

from debug_devices_mcp.config import Settings
from debug_devices_mcp.server import build_server
from debug_devices_mcp.snapshot_crop import SnapshotCrop

from .test_orientation import quadrants
from .test_server import FakePhone, make_services, no_vision

# The still is 4000 x 3000; the agent gets 1568 x 1176 (the default max_side).
FULL_WIDTH, FULL_HEIGHT = 4000, 3000
SHOWN_WIDTH = 1568
RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)


def images_of(result) -> list[Image.Image]:
    blocks = [block for block in result.content if getattr(block, "type", "") == "image"]
    return [Image.open(io.BytesIO(base64.b64decode(block.data))).convert("RGB") for block in blocks]


def near(pixel: tuple[int, int, int], colour: tuple[int, int, int]) -> bool:
    return all(abs(a - b) < 40 for a, b in zip(pixel, colour, strict=True))


def test_the_request_needs_one_area() -> None:
    assert SnapshotCrop(x=10, y=20).area().width == 120
    assert SnapshotCrop(box={"x": 1, "y": 2, "width": 30, "height": 40}).area().height == 40
    for bad in (
        {},
        {"x": 10},
        {"x": 1, "y": 2, "box": {"x": 1, "y": 2, "width": 3, "height": 4}},
        {"x": 1, "y": 1, "zoom": 9},
    ):
        with pytest.raises(ValidationError):
            SnapshotCrop.model_validate(bad)


async def test_a_crop_at_full_resolution(settings: Settings) -> None:
    phone = FakePhone()
    phone.snapshot = quadrants(FULL_WIDTH, FULL_HEIGHT)
    async with Client(build_server(make_services(settings, phone, no_vision()))) as client:
        # Around the photo centre: all four quadrants meet there.
        result = await client.call_tool("phone_snapshot", {"crop": {"x": 784, "y": 588, "radius": 40, "zoom": 3}})
        boxed = await client.call_tool(
            "phone_snapshot", {"crop": {"box": {"x": 1500, "y": 1100, "width": 200, "height": 200}, "zoom": 1}}
        )
        outside = await client.call_tool("phone_snapshot", {"crop": {"x": 5000, "y": 10}})
    info = json.loads(result.content[0].text)
    shown, crop = images_of(result)
    assert shown.width == SHOWN_WIDTH
    scale = FULL_WIDTH / SHOWN_WIDTH
    assert info["crop"]["full_resolution_box_px"]["width"] == pytest.approx(80 * scale, abs=1)
    assert info["crop"]["zoom"] == 3
    assert crop.size == (info["crop"]["width"], info["crop"]["height"])
    assert crop.width == pytest.approx(80 * scale * 3, abs=3)
    # The crop shows the four colours in their places (red top left, white bottom right).
    assert near(crop.getpixel((10, 10)), RED)
    assert near(crop.getpixel((crop.width - 10, 10)), GREEN)
    assert near(crop.getpixel((10, crop.height - 10)), BLUE)
    assert near(crop.getpixel((crop.width - 10, crop.height - 10)), WHITE)
    # A box at the bottom right edge is cut at the image edge.
    boxed_info = json.loads(boxed.content[0].text)["crop"]
    assert boxed_info["box_px"]["width"] == pytest.approx(SHOWN_WIDTH - 1500)
    assert near(images_of(boxed)[1].getpixel((5, 5)), WHITE)
    assert outside.is_error
    assert "outside the image" in outside.content[0].text
