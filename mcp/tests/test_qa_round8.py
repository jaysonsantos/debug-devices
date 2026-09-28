"""Last round of the follow-up check (QA round 8), dd-ui part: an empty serial never reaches adb (N2 rest), a
secondary sends the app run of its overlay call (N19), two flip changes at the same time both apply (N20), and the
arrow math for an empty overlay_region at the centre of preview_region (N12, client). Fakes only."""

import asyncio
from pathlib import Path
from uuid import uuid7

import numpy as np
import pytest

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.config import Settings
from debug_devices_mcp.highlight import PixelBox
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.orientation import OrientationState
from debug_devices_mcp.phone_api import OverlayArrow, OverlayBox
from debug_devices_mcp.phone_screen import PhoneScreen, PhoneScreenOptions, ScreenError
from debug_devices_mcp.pointer import ImageFrame, follow_boxes, plan
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.constants import ingest
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import SettingsStore
from debug_devices_mcp.ui.setup import connect_services

from .conftest import FakeRunner, make_jpeg, ok
from .test_bench_feedback import FakeScreen
from .test_highlight import OverlayPhone
from .test_markings import START
from .test_pointing import MM, part
from .test_server import make_services, no_vision
from .test_shared_log import primary_client

# The view of an empty overlay_region: a point at the centre of the 1000 x 800 px image (contract).
EMPTY_AT_CENTRE = (500.0, 400.0, 0.0, 0.0)
FRAME = ImageFrame((1000, 800), SnapshotOrientation(), EMPTY_AT_CENTRE)
BOX = {"x": 10, "y": 10, "width": 40, "height": 30, "label": "R1"}

# region: N2 rest, an empty serial


@pytest.mark.parametrize("selected", [None, ""], ids=["no-selection-hook", "nothing-selected"])
def test_an_empty_serial_never_passes_the_screen_start(tmp_path: Path, selected: str | None) -> None:
    monitor, client = primary_client(tmp_path)
    screen = FakeScreen()
    monitor.screen = screen  # type: ignore[assignment]
    monitor.selected_serial = None if selected is None else (lambda: "")
    headers = {ingest.TOKEN_HEADER: "secret-token"}
    for serial in ("", "R5CT1234567"):
        response = client.post(ingest.SCREEN_START_PATH, json={"serial": serial}, headers=headers)
        assert response.status_code == 403
    assert screen.serials == []


def test_the_phone_screen_refuses_an_empty_serial() -> None:
    runner = FakeRunner(lambda _: ok())
    screen = PhoneScreen(PhoneScreenOptions(adb_path="adb", version="4.1"), runner, on_state=lambda _: None)
    with pytest.raises(ScreenError, match="no ADB serial"):
        screen.ensure_running("")
    assert runner.calls == []


# endregion: N2 rest, an empty serial

# region: N19 the app run of the overlay call


class RecordingForwarder:
    def __init__(self) -> None:
        self.sent: list[str | None] = []

    def send_overlay_soon(self, boxes: list[OverlayBox], arrows: list[OverlayArrow], app_start_id: str | None) -> None:
        self.sent.append(app_start_id)


async def test_a_secondary_sends_the_app_run_of_its_overlay_call(settings: Settings, tmp_path: Path) -> None:
    phone = OverlayPhone()
    phone.snapshot = make_jpeg(400, 300)
    services = make_services(settings, phone, no_vision())
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.instrument(build_server(services))
    connect_services(monitor, services)
    forwarder = RecordingForwarder()
    monitor.forwarder = forwarder  # type: ignore[assignment]
    monitor.is_secondary = lambda: True  # type: ignore[method-assign]
    await monitor.call_from_ui("phone_status", {})
    first_run = phone.status["app_start_id"]
    assert monitor.bus.phone.status is not None
    assert monitor.bus.phone.status.app_start_id == first_run
    # The app restarts; this secondary has no status poll. Its next snapshot and boxes are in the new run.
    phone.status["app_start_id"] = new_run = str(uuid7())
    await monitor.call_from_ui("phone_snapshot", {})
    await monitor.call_from_ui("phone_highlight", {"boxes": [BOX]})
    assert forwarder.sent[-1] == new_run


# endregion: N19 the app run of the overlay call

# region: N20 two flip changes at the same time


async def test_two_flip_changes_at_the_same_time_both_apply(tmp_path: Path) -> None:
    # Two servers (or the page and an agent): each has its own state on the same settings file.
    page = OrientationState(SettingsStore.in_dir(tmp_path))
    agent = OrientationState(SettingsStore.in_dir(tmp_path))
    await asyncio.gather(page.save(flip_horizontal=True), agent.save(flip_vertical=True))
    saved = SettingsStore.in_dir(tmp_path).load().snapshot_orientation
    assert saved == SnapshotOrientation(flip_horizontal=True, flip_vertical=True)
    assert OrientationState(SettingsStore.in_dir(tmp_path)).current == saved


def test_a_sync_update_also_merges_in_the_lock(tmp_path: Path) -> None:
    first = OrientationState(SettingsStore.in_dir(tmp_path))
    second = OrientationState(SettingsStore.in_dir(tmp_path))
    first.update(flip_horizontal=True)
    # `second` did not read the file since; its change still keeps the other flip.
    assert second.update(flip_vertical=True) == SnapshotOrientation(flip_horizontal=True, flip_vertical=True)


# endregion: N20 two flip changes at the same time

# region: N12 the arrows of an empty overlay_region at the centre


def test_arrows_of_an_empty_region_at_the_centre() -> None:
    matrix = np.diag([MM, MM, 1.0])  # 10 px per mm
    with np.errstate(all="raise"):
        result = plan([part("J4", 80, 40), part("U2", 50, 10)], Side.TOP, matrix, FRAME)
    # Nothing is in view: no box, one arrow each, with the direction and the distance from the centre.
    assert result.boxes == []
    right, up = result.arrows
    assert right.angle_deg == pytest.approx(0)
    assert right.label == "J4 ~3 cm"
    assert up.angle_deg == pytest.approx(270)
    assert up.label == "U2 ~3 cm"


def test_plain_boxes_of_an_empty_region_become_arrows() -> None:
    boxes = [PixelBox(x=780, y=380, width=40, height=40, label="R1", tag="A")]
    with np.errstate(all="raise"):
        shown, arrows = follow_boxes(boxes, np.eye(3), FRAME)
    assert shown == []
    [arrow] = arrows
    assert arrow.angle_deg == pytest.approx(0)
    assert arrow.tag == "A"


# endregion: N12 the arrows of an empty overlay_region at the centre
