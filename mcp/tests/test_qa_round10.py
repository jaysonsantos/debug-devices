"""Follow-up of the last-round check (QA round 10), dd-ui part: a target at the centre of an empty view (N27), the
distance for a centred band (N28), the crop delete inside the settings lock (N29), and a screen stream that stops
when the user selects another phone (N30). Fakes only."""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import IO

import numpy as np
import pytest
from mcp import Client
from starlette.testclient import TestClient

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.config import Settings
from debug_devices_mcp.highlight import PixelBox
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.phone_screen import NOT_SELECTED, PhoneScreen, PhoneScreenOptions, ScreenState, ScreenStatus
from debug_devices_mcp.pointer import AT_CENTRE_MESSAGE, ImageFrame, edge_point, follow_boxes, plan
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import ScreenRotation, SettingsStore, UiSettings
from debug_devices_mcp.webcam import Crop

from .conftest import FakeRunner, ok
from .test_box_tracking import MIDDLE, SHIFTED, frame_of, highlight, tracked_services
from .test_markings import START
from .test_pointing import MM, part
from .test_scrcpy import FakeProcess

IMAGE = (1000, 800)
CENTRE_POINT = ImageFrame(IMAGE, SnapshotOrientation(), (500.0, 400.0, 0.0, 0.0))
# A band of height 0 across the middle (the contract allows it; the app sends only a point today).
BAND = ImageFrame(IMAGE, SnapshotOrientation(), (200.0, 400.0, 600.0, 0.0))
TEN_PX_PER_MM = np.diag([MM, MM, 1.0])

# region: N27 a target at the centre of an empty view


def test_a_part_at_the_centre_of_an_empty_view_gets_a_note_and_no_arrow() -> None:
    with np.errstate(all="raise"):
        result = plan([part("C1", 50, 40)], Side.TOP, TEN_PX_PER_MM, CENTRE_POINT)
    assert result.boxes == []
    assert result.arrows == []
    [target] = result.targets
    assert target.in_view is False
    assert target.message.startswith(AT_CENTRE_MESSAGE)


def test_a_box_at_the_centre_of_an_empty_view_gets_no_arrow() -> None:
    boxes = [PixelBox(x=480, y=380, width=40, height=40, label="R1")]
    shown, arrows = follow_boxes(boxes, np.eye(3), CENTRE_POINT)
    assert (shown, arrows) == ([], [])


def test_edge_point_without_a_direction_is_the_centre() -> None:
    centre = np.array([500.0, 400.0])
    edge = edge_point(centre, centre.copy(), np.array([100.0, 100.0]), np.array([900.0, 700.0]))
    assert edge.tolist() == [500.0, 400.0]


async def test_a_geometry_error_in_a_frame_does_not_end_the_tracking(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    services, _ = tracked_services(settings)
    async with Client(build_server(services)) as client:
        await highlight(client, MIDDLE)

        def broken(*_: object) -> None:
            raise ValueError("min() arg is an empty sequence")

        monkeypatch.setattr("debug_devices_mcp.pointing.follow_boxes", broken)
        with caplog.at_level(logging.WARNING):
            await services.scene.frame(frame_of(SHIFTED))
    assert "cannot move the highlight boxes" in caplog.text
    assert "a frame listener failed" not in caplog.text
    assert services.pointing.boxes is not None


# endregion: N27 a target at the centre of an empty view

# region: N28 the distance for a centred band


def test_the_distance_for_a_band_is_from_its_centre() -> None:
    # A part 0.5 cm left of the centre (50 px at 10 px/mm), on the band line: the label says ~0.5 cm, not ~2 cm.
    with np.errstate(all="raise"):
        result = plan([part("C7", 45, 40)], Side.TOP, TEN_PX_PER_MM, BAND)
    [arrow] = result.arrows
    assert arrow.label == "C7 ~0.5 cm"
    assert arrow.angle_deg == pytest.approx(180)


# endregion: N28 the distance for a centred band

# region: N29 the crop delete inside the lock


async def test_a_crop_delete_keeps_a_change_of_another_setting(tmp_path: Path) -> None:
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.update_settings(UiSettings(webcam_crop=Crop(x=0, y=0, width=10, height=6)))
    # Another writer (the page's Screen view, or another server) changes the rotation after this page read the file.
    SettingsStore.in_dir(tmp_path).update(
        lambda saved: saved.model_copy(update={"screen_rotation": ScreenRotation.DEG_90})
    )
    await monitor.clear_crop()
    saved = SettingsStore.in_dir(tmp_path).load()
    assert saved.webcam_crop is None
    assert saved.screen_rotation is ScreenRotation.DEG_90


def test_the_crop_route_uses_the_locked_delete(tmp_path: Path) -> None:
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.update_settings(UiSettings(webcam_crop=Crop(x=0, y=0, width=10, height=6)))
    SettingsStore.in_dir(tmp_path).update(
        lambda saved: saved.model_copy(update={"screen_rotation": ScreenRotation.DEG_180})
    )
    client = TestClient(create_app(monitor), base_url="http://127.0.0.1:18766")
    response = client.delete("/api/settings/crop", headers={"Origin": "http://127.0.0.1:18766"})
    assert response.status_code == 200, response.text
    assert SettingsStore.in_dir(tmp_path).load().screen_rotation is ScreenRotation.DEG_180


# endregion: N29 the crop delete inside the lock

# region: N30 the screen stream and the selection


async def test_the_screen_stream_stops_when_another_phone_is_selected(tmp_path: Path) -> None:
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"jar")

    def respond(command: list[str]) -> CommandResult:
        if "forward" in command and "--remove" not in command:
            return ok(b"40000\n")
        return ok()

    async def spawner(args: Sequence[str], environ: Mapping[str, str], log: IO[bytes] | None) -> FakeProcess:
        raise FileNotFoundError("the session fails at once: the loop restarts it")

    runner = FakeRunner(respond)
    states: list[ScreenState] = []
    options = PhoneScreenOptions(
        adb_path="adb", server_path=server_file, version="4.1", restart_delay=timedelta(milliseconds=20)
    )
    screen = PhoneScreen(options, runner, spawner=spawner, on_state=states.append)
    selected = ["OLD1234"]
    screen.selection = lambda: selected[0]
    screen.ensure_running("OLD1234")
    for _ in range(100):
        if sum(1 for call in runner.calls if "push" in call) >= 2:
            break
        await asyncio.sleep(0.01)
    # The user selects another phone (maybe in the primary page): after the next restart delay, the loop stops.
    selected[0] = "NEW5678"
    await asyncio.sleep(0.1)
    calls = len(runner.calls)
    await asyncio.sleep(0.1)
    assert len(runner.calls) == calls
    assert states[-1] == ScreenState(status=ScreenStatus.ERROR, error=NOT_SELECTED.format(serial="OLD1234"))
    assert all(call[2] == "OLD1234" for call in runner.calls)
    await screen.stop()


async def test_the_screen_stream_does_not_start_for_a_phone_that_is_not_selected() -> None:
    runner = FakeRunner(lambda _: ok())
    screen = PhoneScreen(PhoneScreenOptions(adb_path="adb", version="4.1"), runner, on_state=lambda _: None)
    screen.selection = lambda: "NEW5678"
    screen.ensure_running("OLD1234")
    await asyncio.sleep(0.05)
    assert runner.calls == []
    await screen.stop()


# endregion: N30 the screen stream and the selection
