"""QA round 4, batch 3 (dd-ui): the annotated image (B-S3, B-S4), a read-only flips call (B-S5), the resend retry
(B-S6), the markings note of board_locate_in_photo (B-S7), tags (B-S10), the page snapshot (B-S11, B-E9), the preview
rotation (B-E10), and the secondary's frames and overlays (B-F6, B-F9). B-F10 is in test_webcam_controls.py; B-S8 is
the page settings save in round 39. Fakes only."""

import io
from pathlib import Path

import httpx
import pytest
from mcp import Client
from PIL import Image
from pydantic import ValidationError
from starlette.testclient import TestClient

from debug_devices_mcp.app_start import MAX_RESEND_TRIES, AppStartWatch
from debug_devices_mcp.camera_choice import AfModeChoice, AfModeSync, MarkingsChoice
from debug_devices_mcp.config import Settings
from debug_devices_mcp.highlight import PixelBox
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.orientation import OrientationState, PreviewSync, phone_preview_flips, view_rotation
from debug_devices_mcp.overlay_draw import draw_layout
from debug_devices_mcp.overlay_layout import LayoutBox
from debug_devices_mcp.phone_api import ApiError, CameraStatus, OverlayBox, PhoneApiError
from debug_devices_mcp.pointer import PointResult
from debug_devices_mcp.server import MARKINGS_HIDDEN, build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.constants import ingest
from debug_devices_mcp.ui.forward import IngestOverlay
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore

from .conftest import make_jpeg
from .test_phone_api import STATUS
from .test_preview_sync import PreviewPhone, client_for, services_with
from .test_server import FakePhone, make_services, no_vision

START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
GREY = (128, 128, 128)


def grey_jpeg(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), GREY).save(output, format="JPEG", quality=95)
    return output.getvalue()


def decode(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg)).convert("RGB")


def near(pixel: tuple[int, int, int], colour: tuple[int, int, int], tolerance: int = 50) -> bool:
    return all(abs(a - b) < tolerance for a, b in zip(pixel, colour, strict=True))


# region: B-S3, B-S4 the annotated image


def test_the_dark_outline_is_also_outside_the_colour_outline() -> None:
    annotated, result = draw_layout(grey_jpeg(400, 300), [LayoutBox(x=100, y=100, width=120, height=80, label="R1")])
    image = decode(annotated)
    rect = result.boxes[0].rect
    middle = round(rect.y + rect.height / 2)
    # 1 px outside the colour outline: the dark outline (rule 5: 2 px on each side), not the grey board.
    outside = image.getpixel((round(rect.x) - 1, middle))
    assert sum(outside) < sum(GREY) - 60, outside
    # 2 px further out: the board again.
    assert near(image.getpixel((round(rect.x) - 4, middle)), GREY)


def test_the_inset_has_the_real_boxes_and_their_tags() -> None:
    # Two small boxes (below 3% of the width): the drawn boxes are 32 px, larger than the enlarged real ones.
    boxes = [
        LayoutBox(x=600, y=400, width=8, height=6, label="R1", tag="A"),
        LayoutBox(x=630, y=410, width=8, height=6, label="R2", tag="B"),
    ]
    annotated, result = draw_layout(grey_jpeg(1200, 900), boxes)
    assert result.inset is not None
    image = decode(annotated)
    dest = result.inset.dest
    scale = result.inset.scale
    # The colour outline of the first box, on the enlarged real area inside the inset.
    left = dest.x + (boxes[0].x - result.inset.source.x) * scale
    top = dest.y + (boxes[0].y - result.inset.source.y) * scale
    bottom = top + boxes[0].height * scale
    red, green, blue = image.getpixel((round(left) + 1, round(bottom) - 2))
    assert green > red + 80, (red, green, blue)
    assert green > blue + 30, (red, green, blue)
    # The tag badge on the top left corner of the box: a dark fill.
    assert sum(image.getpixel((round(left) + 1, round(top) + 1))) < sum(GREY)


# endregion: B-S3, B-S4 the annotated image

# region: B-S5 read-only flips, B-E10 the rotation of a push


async def test_a_flips_read_sends_nothing_and_saves_nothing(settings: Settings, tmp_path: Path) -> None:
    phone = PreviewPhone()
    services = services_with(settings, phone, tmp_path, SnapshotOrientation(flip_horizontal=True))
    settings_file = SettingsStore.in_dir(tmp_path).path
    before = settings_file.stat().st_mtime_ns
    async with Client(build_server(services)) as client:
        read = await client.call_tool("phone_snapshot_orientation", {})
        assert read.structured_content == {"flip_horizontal": True, "flip_vertical": False}
        assert phone.previews == []
        assert settings_file.stat().st_mtime_ns == before
        # A set still pushes the flips.
        await client.call_tool("phone_snapshot_orientation", {"flip_vertical": True})
    assert phone.previews


async def test_a_push_reads_the_rotation_of_now(tmp_path: Path) -> None:
    phone = PreviewPhone()
    orientation = OrientationState(SettingsStore.in_dir(tmp_path))
    orientation.update(True, False)
    sync = PreviewSync(client_for(phone), orientation)
    # The phone turned since the last status that the sync saw.
    phone.status["rotation_degrees"] = 90
    await sync.push()
    assert ("GET", "/v1/status") in phone.requests
    expected = phone_preview_flips(orientation.current, view_rotation(orientation.screen_rotation, 90))
    assert phone.previews[-1] == {"flip_horizontal": expected.flip_horizontal, "flip_vertical": expected.flip_vertical}


# endregion: B-S5 read-only flips, B-E10 the rotation of a push

# region: B-S6 resend retry


def status_of(**changes: object) -> CameraStatus:
    return CameraStatus.model_validate({**STATUS, "app_start_id": "run-1", **changes})


def test_a_failed_resend_may_try_again_a_few_times() -> None:
    watch = AppStartWatch("test setting")
    status = status_of()
    assert watch.may_send(status)
    for _ in range(MAX_RESEND_TRIES - 1):
        watch.failed()
        assert watch.may_send(status)
    watch.failed()
    assert not watch.may_send(status)
    # A new app run starts again.
    assert watch.may_send(status_of(app_start_id="run-2"))


class FlakyCameraPhone:
    """`camera()` fails with 503 (camera not ready) `failures` times, then takes the mode."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def camera(self, request: object) -> CameraStatus:
        self.calls += 1
        if self.calls <= self.failures:
            raise PhoneApiError(503, ApiError(error="camera_not_ready", message="Camera is not bound yet"))
        return status_of(af_mode="macro")


async def test_the_autofocus_sync_retries_after_a_passing_error() -> None:
    choice = AfModeChoice()
    choice.set("macro")
    phone = FlakyCameraPhone(failures=1)
    sync = AfModeSync(phone, choice)  # type: ignore[arg-type]
    status = status_of(af_mode="continuous")
    await sync.ensure(status)
    # The next status read (the same app run) tries again: a passing error does not stop the sync.
    result = await sync.ensure(status)
    assert phone.calls == 2
    assert result.af_mode is not None
    assert result.af_mode.value == "macro"


# endregion: B-S6 resend retry

# region: B-S7 the markings note, B-S10 tags


async def test_locate_with_highlight_has_the_markings_note(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())
    services.markings = MarkingsChoice()
    services.markings.set(False)

    async def point_to(refdes: list[str], registration_id: str | None) -> PointResult:
        return PointResult(
            count=0, boxes=[], arrows=[], targets=[], tracking=False, overlay_boxes=0, overlay_arrows=0, note=""
        )

    services.pointing.point_to = point_to  # type: ignore[method-assign]
    result, _ = await services.highlight_parts("reg-1", ["U1"], [], (100, 100))
    assert result.markings == MARKINGS_HIDDEN


@pytest.mark.parametrize("tag", ["😀😀", "é", "", "ABCD", "A-1", "Ⅻ"])
def test_tags_follow_the_app_rule(tag: str) -> None:
    with pytest.raises(ValidationError):
        PixelBox(x=1, y=1, width=5, height=5, tag=tag)
    with pytest.raises(ValidationError):
        OverlayBox(snapshot_x=0.1, snapshot_y=0.1, width=0.1, height=0.1, tag=tag)


def test_ascii_tags_pass() -> None:
    assert PixelBox(x=1, y=1, width=5, height=5, tag="A1").tag == "A1"
    assert OverlayBox(snapshot_x=0.1, snapshot_y=0.1, width=0.1, height=0.1, tag="R12").tag == "R12"


# endregion: B-S7 the markings note, B-S10 tags

# region: B-S11, B-E9 the page snapshot


def two_colour_jpeg() -> bytes:
    image = Image.new("RGB", (80, 40), (220, 20, 20))
    image.paste((20, 20, 220), (40, 0, 80, 40))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


async def test_the_page_snapshot_without_the_raw_still_follows_a_flip(tmp_path: Path) -> None:
    orientation = OrientationState(SettingsStore.in_dir(tmp_path))
    monitor = Monitor(
        START,
        SettingsStore.in_dir(tmp_path),
        MonitorOptions(open_browser=False, port=0),
        MonitorParts(orientation=orientation),
    )
    monitor.last_snapshot = two_colour_jpeg()  # taken without flips; no raw still
    assert await monitor.render_snapshot(full=False) == monitor.last_snapshot
    orientation.update(True, None)
    flipped = decode(await monitor.render_snapshot(full=False) or b"")
    # Mirrored like the boxes that the page draws with the flips of now: blue is at the left.
    assert near(flipped.getpixel((5, 20)), (20, 20, 220))


async def test_a_bench_measure_photo_is_the_page_snapshot(settings: Settings, tmp_path: Path) -> None:
    phone = FakePhone()
    phone.snapshot = make_jpeg(64, 48)
    services = make_services(settings, phone, no_vision())
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.instrument(build_server(services))
    _, result = await monitor.call_from_ui("bench_measure", {})
    assert not result.is_error, result.content
    assert monitor.last_snapshot is not None
    assert monitor.bus.phone.snapshot_seq == 1
    assert monitor.bus.phone.has_snapshot


# endregion: B-S11, B-E9 the page snapshot

# region: B-F6, B-F9 a secondary server


def test_a_frame_number_from_before_a_primary_restart_gets_the_newest_frame(tmp_path: Path) -> None:
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.ingest_token = "token-1"
    monitor.scene_frame = lambda: (3, b"jpeg-3")  # type: ignore[method-assign]
    client = TestClient(create_app(monitor), base_url="http://127.0.0.1:18766")
    headers = {ingest.TOKEN_HEADER: "token-1"}
    same = client.get(ingest.FRAME_PATH, params={ingest.AFTER_PARAM: "3"}, headers=headers)
    assert same.status_code == httpx.codes.NO_CONTENT
    # The secondary counted to 250 with the old primary: it gets frame 3 of the new one.
    newer = client.get(ingest.FRAME_PATH, params={ingest.AFTER_PARAM: "250"}, headers=headers)
    assert newer.status_code == httpx.codes.OK
    assert newer.content == b"jpeg-3"


def test_an_older_overlay_that_comes_late_is_dropped(tmp_path: Path) -> None:
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    newer = [OverlayBox(snapshot_x=0.5, snapshot_y=0.5, width=0.1, height=0.1, label="new")]
    older = [OverlayBox(snapshot_x=0.1, snapshot_y=0.1, width=0.1, height=0.1, label="old")]
    assert monitor.remote_overlay(IngestOverlay(origin="codex 1", boxes=newer, arrows=[], seq=2))
    assert not monitor.remote_overlay(IngestOverlay(origin="codex 1", boxes=older, arrows=[], seq=1))
    assert [box.label for box in monitor.bus.phone.highlights] == ["new"]
    # Another secondary has its own numbers; a sender without numbers is always taken.
    assert monitor.remote_overlay(IngestOverlay(origin="claude 2", boxes=older, arrows=[], seq=1))
    assert monitor.remote_overlay(IngestOverlay(origin="old server", boxes=newer, arrows=[]))


# endregion: B-F6, B-F9 a secondary server
