"""Snapshot flips: one helper, one state, the same orientation for the tools, the files, and the monitor page."""

import base64
import io
import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent
from PIL import Image
from starlette.testclient import TestClient

from debug_devices_mcp.config import Settings
from debug_devices_mcp.images import SnapshotOrientation, orient_jpeg
from debug_devices_mcp.orientation import OrientationState
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore, UiSettings

from .test_server import FakePhone, make_services, no_vision

START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"
RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)
SIDE = 64
EXIF_ORIENTATION_TAG = 0x0112
EXIF_ROTATE_90_CW = 6
TOLERANCE = 60


def quadrants(width: int = SIDE, height: int = SIDE, exif: Image.Exif | None = None) -> bytes:
    """Top left red, top right green, bottom left blue, bottom right white."""
    image = Image.new("RGB", (width, height))
    half_w, half_h = width // 2, height // 2
    for box, color in (
        ((0, 0, half_w, half_h), RED),
        ((half_w, 0, width, half_h), GREEN),
        ((0, half_h, half_w, height), BLUE),
        ((half_w, half_h, width, height), WHITE),
    ):
        image.paste(color, box)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95, exif=exif or Image.Exif())
    return output.getvalue()


def corners(jpeg: bytes) -> list[tuple[int, int, int]]:
    """The colors at the centers of the four quadrants: top left, top right, bottom left, bottom right."""
    with Image.open(io.BytesIO(jpeg)) as image:
        rgb = image.convert("RGB")
        w, h = rgb.size
        return [
            rgb.getpixel((x, y))
            for x, y in ((w // 4, h // 4), (3 * w // 4, h // 4), (w // 4, 3 * h // 4), (3 * w // 4, 3 * h // 4))
        ]


def assert_colors(jpeg: bytes, expected: list[tuple[int, int, int]]) -> None:
    for got, want in zip(corners(jpeg), expected, strict=True):
        assert all(abs(a - b) <= TOLERANCE for a, b in zip(got, want, strict=True)), (got, want)


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        (SnapshotOrientation(flip_horizontal=True), [GREEN, RED, WHITE, BLUE]),
        (SnapshotOrientation(flip_vertical=True), [BLUE, WHITE, RED, GREEN]),
        (SnapshotOrientation(flip_horizontal=True, flip_vertical=True), [WHITE, BLUE, GREEN, RED]),
    ],
)
def test_orient_jpeg_flips_pixels(orientation: SnapshotOrientation, expected: list) -> None:
    source = quadrants()
    flipped = orient_jpeg(source, orientation)
    assert_colors(flipped, expected)
    assert len(flipped) <= len(source)


def test_no_flip_keeps_the_same_bytes() -> None:
    source = quadrants()
    assert orient_jpeg(source, SnapshotOrientation()) is source


def test_the_exif_rotation_comes_before_the_flip() -> None:
    exif = Image.Exif()
    exif[EXIF_ORIENTATION_TAG] = EXIF_ROTATE_90_CW
    source = quadrants(SIDE * 2, SIDE, exif)
    flipped = orient_jpeg(source, SnapshotOrientation(flip_horizontal=True))
    with Image.open(io.BytesIO(flipped)) as image:
        assert image.size == (SIDE, SIDE * 2)  # upright first
        assert image.getexif().get(EXIF_ORIENTATION_TAG) in (None, 1)
    # Rotated 90 degrees clockwise: top left blue, top right red, bottom left white, bottom right green.
    # Then mirrored left-right.
    assert_colors(flipped, [RED, BLUE, GREEN, WHITE])


def test_descriptions() -> None:
    assert SnapshotOrientation().describe() == "as taken (no flip)"
    assert SnapshotOrientation(flip_vertical=True).describe() == "flipped vertically"
    assert SnapshotOrientation(flip_horizontal=True, flip_vertical=True).describe() == (
        "flipped horizontally and vertically"
    )


def test_state_persists_and_keeps_the_other_settings(tmp_path: Path) -> None:
    store = SettingsStore.in_dir(tmp_path)
    store.save(UiSettings(vision_model="x/model"))
    seen: list[SnapshotOrientation] = []
    state = OrientationState(store)
    state.add_listener(seen.append)
    assert state.update(flip_vertical=True) == SnapshotOrientation(flip_vertical=True)
    assert state.update(flip_horizontal=True) == SnapshotOrientation(flip_horizontal=True, flip_vertical=True)
    assert state.update() == SnapshotOrientation(flip_horizontal=True, flip_vertical=True)  # None keeps both
    assert len(seen) == 3
    saved = store.load()
    assert saved.vision_model == "x/model"
    assert OrientationState(store).current == SnapshotOrientation(flip_horizontal=True, flip_vertical=True)


async def test_tool_returns_the_oriented_photo_and_says_so(settings: Settings, tmp_path: Path) -> None:
    fake_phone = FakePhone()
    fake_phone.snapshot = quadrants()
    services = make_services(settings, fake_phone, no_vision())
    services.orientation = OrientationState(SettingsStore.in_dir(tmp_path))
    saved_file = tmp_path / "shot.jpg"
    async with Client(build_server(services)) as client:
        read = await client.call_tool("phone_snapshot_orientation", {})
        assert read.structured_content == {"flip_horizontal": False, "flip_vertical": False}
        await client.call_tool("phone_snapshot_orientation", {"flip_horizontal": True})
        result = await client.call_tool("phone_snapshot", {"save_path": str(saved_file)})
    text = next(block.text for block in result.content if isinstance(block, TextContent))
    assert json.loads(text)["orientation"] == "flipped horizontally"
    image = next(block for block in result.content if isinstance(block, ImageContent))
    assert_colors(base64.b64decode(image.data), [GREEN, RED, WHITE, BLUE])
    assert_colors(saved_file.read_bytes(), [GREEN, RED, WHITE, BLUE])  # the saved file too


def test_page_and_tool_show_the_same_orientation(settings: Settings, tmp_path: Path) -> None:
    fake_phone = FakePhone()
    fake_phone.snapshot = quadrants(SIDE * 40, SIDE * 30)  # large: the panel image is scaled
    services = make_services(settings, fake_phone, no_vision())
    services.orientation = OrientationState(SettingsStore.in_dir(tmp_path))
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(port=0),
        MonitorParts(orientation=services.orientation),
    )
    services.orientation.add_listener(monitor.orientation_changed)
    services.phone.snapshot = monitor.phone_snapshot_recorder(services.phone.snapshot)  # type: ignore[method-assign]
    monitor.instrument(build_server(services))
    client = TestClient(create_app(monitor), base_url=BASE_URL)

    phone = client.post("/api/phone/orientation", json={"flip_vertical": True}).json()
    assert phone["orientation"] == {"flip_horizontal": False, "flip_vertical": True}
    client.post("/api/phone/snapshot")
    upside_down = [BLUE, WHITE, RED, GREEN]
    assert_colors(client.get("/api/phone/snapshot.jpg").content, upside_down)
    assert_colors(client.get("/api/phone/snapshot.jpg", params={"full": "true"}).content, upside_down)
    call = client.get("/api/state").json()["calls"][-1]
    assert_colors(client.get(f"/api/calls/{call['id']}/images/0").content, upside_down)  # the tool result

    # A new flip re-renders the same snapshot for the page at once.
    client.post("/api/phone/orientation", json={"flip_vertical": False})
    assert_colors(client.get("/api/phone/snapshot.jpg").content, [RED, GREEN, BLUE, WHITE])
    assert [c["tool"] for c in client.get("/api/state").json()["calls"]] == [
        "phone_snapshot_orientation",
        "phone_snapshot",
        "phone_snapshot_orientation",
    ]
    # A page save of other settings keeps the flips of OrientationState.
    services.orientation.update(flip_horizontal=True)
    stale = UiSettings(vision_model="y/model", snapshot_orientation=SnapshotOrientation())
    monitor.update_settings(stale)
    assert SettingsStore.in_dir(tmp_path).load().snapshot_orientation == SnapshotOrientation(flip_horizontal=True)
