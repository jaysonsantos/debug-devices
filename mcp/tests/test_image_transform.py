"""P0: phone_snapshot must show the same picture as the monitor preview (turn and flips).

A synthetic target (written for these tests, same license as this repository): an arrow marker and four shapes of
different colors. The fake camera has a landscape sensor image (sensor orientation 90, like most phones):

- the phone screen shows N = the sensor image turned 90° clockwise (portrait), with the preview flips of the app;
- the page shows the screen turned clockwise by V (the Screen view choice, or auto);
- the app's still is the sensor image turned clockwise by (90 - rotation_degrees) (CameraX).

For every Screen view choice, phone rotation, and flip, phone_snapshot must equal the page view, and positions (focus,
boxes, arrows) must map back to the same spot on the still.
"""

import base64
import io
import itertools
import json
from uuid import uuid7

import httpx
import numpy as np
import pytest
from mcp import Client
from PIL import Image, ImageDraw

from debug_devices_mcp.config import Settings
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.orientation import (
    ImageTransform,
    OrientationState,
    phone_preview_flips,
    remaining_turn,
    still_transform,
    view_rotation,
)
from debug_devices_mcp.server import build_server

from .test_server import FakePhone, make_services, no_vision

SENSOR_WIDTH, SENSOR_HEIGHT = 480, 360
SENSOR_ORIENTATION = 90
TURNS = (0, 90, 180, 270)
SCREEN_CHOICES = ("auto", "0", "90", "180", "270")
FLIPS = [(False, False), (True, False), (False, True), (True, True)]
# Mean absolute difference per pixel channel (0-255) for "the same picture" after JPEG and scaling.
SAME_PICTURE = 6.0
RED = (220, 30, 30)
SHAPES = (
    ("circle", RED),
    ("square", (30, 160, 50)),
    ("triangle", (30, 60, 220)),
    ("diamond", (240, 200, 20)),
)
EXIF_ORIENTATION_TAG = 0x0112
DEGREES_TO_EXIF = {0: 1, 90: 6, 180: 3, 270: 8}
CLOCKWISE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def target() -> Image.Image:
    """The sensor image: an arrow (pointing right, with a notch at its top) and four shapes in a row."""
    image = Image.new("RGB", (SENSOR_WIDTH, SENSOR_HEIGHT), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    draw.polygon([(20, 150), (90, 150), (90, 120), (140, 180), (90, 240), (90, 210), (20, 210)], fill=(0, 0, 0))
    draw.rectangle((20, 130, 40, 150), fill=(0, 0, 0))  # the notch: the arrow is not mirror-symmetric
    for index, (shape, color) in enumerate(SHAPES):
        x, y = 190 + index * 72, 150 + (index % 2) * 60
        if shape == "circle":
            draw.ellipse((x, y, x + 50, y + 50), fill=color)
        elif shape == "square":
            draw.rectangle((x, y, x + 50, y + 50), fill=color)
        elif shape == "triangle":
            draw.polygon([(x, y + 50), (x + 25, y), (x + 50, y + 50)], fill=color)
        else:
            draw.polygon([(x + 25, y), (x + 50, y + 25), (x + 25, y + 50), (x, y + 25)], fill=color)
    return image


SENSOR = target()


def turn(image: Image.Image, degrees: int) -> Image.Image:
    degrees %= 360
    return image.transpose(CLOCKWISE[degrees]) if degrees else image


def flip(image: Image.Image, flips: SnapshotOrientation) -> Image.Image:
    if flips.flip_horizontal:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flips.flip_vertical:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return image


def page_view(screen_choice: str, rotation: int, flips: SnapshotOrientation) -> Image.Image:
    """What the monitor shows: the phone screen (N with the app's preview flips) turned by V."""
    view = view_rotation(screen_choice, rotation)
    natural = turn(SENSOR, SENSOR_ORIENTATION)
    return turn(flip(natural, phone_preview_flips(flips, view)), view)


def still_jpeg(rotation: int) -> bytes:
    """The app's still: the sensor image with the EXIF orientation of CameraX for this rotation."""
    exif = Image.Exif()
    exif[EXIF_ORIENTATION_TAG] = DEGREES_TO_EXIF[(SENSOR_ORIENTATION - rotation) % 360]
    output = io.BytesIO()
    SENSOR.save(output, format="JPEG", quality=95, exif=exif)
    return output.getvalue()


def still(rotation: int) -> Image.Image:
    return turn(SENSOR, (SENSOR_ORIENTATION - rotation) % 360)


def difference(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return float("inf")
    return float(np.abs(np.asarray(a, dtype=float) - np.asarray(b.convert("RGB"), dtype=float)).mean())


def red_center(image: Image.Image) -> tuple[float, float]:
    """The center of the red circle, from 0 to 1."""
    pixels = np.asarray(image.convert("RGB"), dtype=int)
    mask = (pixels[..., 0] > 180) & (pixels[..., 1] < 90) & (pixels[..., 2] < 90)
    ys, xs = np.nonzero(mask)
    return (xs.mean() + 0.5) / image.width, (ys.mean() + 0.5) / image.height


# region: pure functions


@pytest.mark.parametrize(("choice", "rotation"), list(itertools.product(SCREEN_CHOICES, TURNS)))
def test_remaining_turn_table(choice: str, rotation: int) -> None:
    view = view_rotation(choice, rotation)
    expected_view = (360 - rotation) % 360 if choice == "auto" else int(choice)
    assert view == expected_view
    assert remaining_turn(view, rotation) == (0 if choice == "auto" else (int(choice) + rotation) % 360)


@pytest.mark.parametrize(("choice", "rotation", "flips"), list(itertools.product(SCREEN_CHOICES, TURNS, FLIPS)))
def test_the_transformed_still_is_the_page_view(choice: str, rotation: int, flips: tuple[bool, bool]) -> None:
    chosen = SnapshotOrientation(flip_horizontal=flips[0], flip_vertical=flips[1])
    transform = still_transform(choice, rotation, chosen)
    shown = flip(turn(still(rotation), transform.turn_degrees), chosen)
    expected = page_view(choice, rotation, chosen)
    assert difference(shown, expected) == 0, (choice, rotation, flips, transform)
    # The phone still rotation is used once: without the remaining turn, a manual choice would not match.
    if transform.turn_degrees:
        assert difference(flip(still(rotation), chosen), expected) > SAME_PICTURE


@pytest.mark.parametrize(("turn_degrees", "flips"), list(itertools.product(TURNS, FLIPS)))
def test_positions_map_back_to_the_still(turn_degrees: int, flips: tuple[bool, bool]) -> None:
    transform = ImageTransform(turn_degrees=turn_degrees, flip_horizontal=flips[0], flip_vertical=flips[1])
    base = still(0)
    shown = flip(turn(base, turn_degrees), transform)
    # A point: the red circle center.
    assert transform.point_to_true(*red_center(shown)) == pytest.approx(red_center(base), abs=0.01)
    assert transform.point_from_true(*red_center(base)) == pytest.approx(red_center(shown), abs=0.01)
    # A box: the red circle's box (the same on both, after the mapping).
    x, y = red_center(shown)
    box = transform.box_to_true(x - 0.02, y - 0.03, 0.04, 0.06)
    assert (box[0] + box[2] / 2, box[1] + box[3] / 2) == pytest.approx(red_center(base), abs=0.01)

    # An angle in pixels: from the image center to the red circle.
    def angle(image: Image.Image) -> float:
        cx, cy = red_center(image)
        return float(np.degrees(np.arctan2((cy - 0.5) * image.height, (cx - 0.5) * image.width)) % 360)

    assert transform.angle_to_true(angle(shown)) == pytest.approx(angle(base), abs=1.0)
    assert transform.angle_from_true(angle(base)) == pytest.approx(angle(shown), abs=1.0)


@pytest.mark.parametrize("view", TURNS)
def test_preview_flips_swap_on_a_sideways_view(view: int) -> None:
    flips = SnapshotOrientation(flip_horizontal=True, flip_vertical=False)
    sent = phone_preview_flips(flips, view)
    assert (sent.flip_horizontal, sent.flip_vertical) == ((False, True) if view % 180 else (True, False))


# endregion: pure functions

# region: through the MCP server


class ScenePhone(FakePhone):
    """The httpx fake phone with the target as its camera: the still turns with rotation_degrees (CameraX)."""

    def __init__(self, rotation: int = 0) -> None:
        super().__init__()
        self.status.update(rotation_degrees=rotation, preview_flip_horizontal=False, preview_flip_vertical=False)
        self.snapshot = still_jpeg(rotation)
        self.focus_points: list[dict] = []
        self.overlays: list[dict] = []
        self.previews: list[dict] = []

    def restart_app(self) -> None:
        """A new app run: a new app_start_id, no preview flips, and the rotation back to auto."""
        self.status.update(app_start_id=str(uuid7()), preview_flip_horizontal=False, preview_flip_vertical=False)
        self.status["rotation_locked"] = False

    def rotate(self, rotation: int) -> None:
        self.status["rotation_degrees"] = rotation
        self.snapshot = still_jpeg(rotation)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in ("/v1/focus", "/v1/overlay", "/v1/preview"):
            body = json.loads(request.content)
            {"/v1/focus": self.focus_points, "/v1/overlay": self.overlays, "/v1/preview": self.previews}[path].append(
                body
            )
            if path == "/v1/preview":
                self.status.update(
                    preview_flip_horizontal=body["flip_horizontal"], preview_flip_vertical=body["flip_vertical"]
                )
            return httpx.Response(200, json=self.status)
        return super().handle(request)


def image_of(result) -> Image.Image:
    block = next(block for block in result.content if getattr(block, "type", "") == "image")
    return Image.open(io.BytesIO(base64.b64decode(block.data))).convert("RGB")


CASES = [(choice, rotation, flips) for choice in SCREEN_CHOICES for rotation in (0, 90) for flips in FLIPS[:2]]


@pytest.mark.parametrize(("choice", "rotation", "flips"), CASES)
async def test_phone_snapshot_matches_the_page_view(
    settings: Settings, choice: str, rotation: int, flips: tuple[bool, bool]
) -> None:
    phone = ScenePhone(rotation)
    services = make_services(settings, phone, no_vision())
    services.orientation = OrientationState()
    services.orientation.set_screen_rotation(choice)
    services.__post_init__()
    chosen = SnapshotOrientation(flip_horizontal=flips[0], flip_vertical=flips[1])
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot_orientation", {"flip_horizontal": flips[0], "flip_vertical": flips[1]})
        result = await client.call_tool("phone_snapshot", {"max_side": 0})
        info = json.loads(result.content[0].text)
        shown = image_of(result)
        expected = page_view(choice, rotation, chosen)
        assert (info["width"], info["height"]) == expected.size
        assert (info["turn_degrees"], info["flip_horizontal"], info["flip_vertical"]) == (
            still_transform(choice, rotation, chosen).turn_degrees,
            flips[0],
            flips[1],
        )
        assert difference(shown, expected) < SAME_PICTURE, (choice, rotation, flips)

        # The agent points at the red circle in its image: the phone gets the red circle of its own still.
        x, y = red_center(shown)
        await client.call_tool("phone_focus", {"x": x * shown.width, "y": y * shown.height})
        box = {"x": x * shown.width - 20, "y": y * shown.height - 20, "width": 40, "height": 40, "label": "C1"}
        await client.call_tool("phone_highlight", {"boxes": [box]})
    red = red_center(still(rotation))
    assert (phone.focus_points[-1]["snapshot_x"], phone.focus_points[-1]["snapshot_y"]) == pytest.approx(red, abs=0.01)
    [sent] = phone.overlays[-1]["boxes"]
    center = (sent["snapshot_x"] + sent["width"] / 2, sent["snapshot_y"] + sent["height"] / 2)
    assert center == pytest.approx(red, abs=0.01)


async def test_after_a_phone_rotation_and_an_app_restart(settings: Settings) -> None:
    phone = ScenePhone(0)
    services = make_services(settings, phone, no_vision())
    services.orientation = OrientationState()
    services.orientation.set_screen_rotation("90")
    services.__post_init__()
    async with Client(build_server(services)) as client:
        for rotation in (0, 270, 180):
            phone.rotate(rotation)
            result = await client.call_tool("phone_snapshot", {"max_side": 0})
            assert difference(image_of(result), page_view("90", rotation, SnapshotOrientation())) < SAME_PICTURE
        phone.restart_app()
        phone.rotate(0)
        result = await client.call_tool("phone_snapshot", {"max_side": 0})
    assert difference(image_of(result), page_view("90", 0, SnapshotOrientation())) < SAME_PICTURE


async def test_preview_flips_follow_the_view_turn(settings: Settings) -> None:
    phone = ScenePhone(0)
    services = make_services(settings, phone, no_vision())
    services.orientation = OrientationState()
    services.__post_init__()
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_connect", {})
        await client.call_tool("phone_snapshot_orientation", {"flip_horizontal": True})
        assert phone.previews[-1] == {"flip_horizontal": True, "flip_vertical": False}  # auto, rotation 0: V = 0
        # The user turns the page view by 90 degrees: the phone must mirror the other axis of its natural frame.
        services.orientation.set_screen_rotation("90")
        await client.call_tool("phone_status", {})
        assert phone.previews[-1] == {"flip_horizontal": False, "flip_vertical": True}
        sent = len(phone.previews)
        for _ in range(3):
            await client.call_tool("phone_status", {})
        assert len(phone.previews) == sent  # no fight: sent once per change


# endregion: through the MCP server
