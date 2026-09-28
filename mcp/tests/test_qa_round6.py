"""Follow-up of the final QA run (round 6), dd-ui part: the lifetime of the boxes of a secondary server and of an old
app with hidden markings (C11), the screen start of a secondary only for the selected phone (N2), a settings lock
that does not stop the event loop (N3), the forward record after a failed removal (N4), and saved webcam controls
that say what the camera has (N7). Fakes only."""

import asyncio
import fcntl
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from debug_devices_mcp.adb import Adb, AdbError
from debug_devices_mcp.camera_choice import MarkingsChoice
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import phone as phone_names
from debug_devices_mcp.phone_api import OverlayBox
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.ui.constants import ingest
from debug_devices_mcp.ui.forward import IngestOverlay
from debug_devices_mcp.ui.settings import LOCK_SUFFIX, SettingsStore
from debug_devices_mcp.webcam_controls import V4l2Controls, WebcamControls, WebcamControlStore

from .conftest import FakeRunner, failed, ok
from .test_bench_feedback import FakeScreen
from .test_markings import VisiblePhone, services_for
from .test_server import FakePhone, make_services, no_vision
from .test_shared_log import primary_client
from .test_webcam_controls import DEVICE, fake_v4l2

TTL = timedelta(milliseconds=60)
BOX = OverlayBox(snapshot_x=0.1, snapshot_y=0.1, width=0.1, height=0.1, label="R1")
OTHER_BOX = OverlayBox(snapshot_x=0.5, snapshot_y=0.5, width=0.1, height=0.1, label="C9")
RUN_1, RUN_2 = "run-1", "run-2"


# region: C11 (a) the boxes of a secondary server on the primary page


async def test_the_boxes_of_a_secondary_go_after_the_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_names, "OVERLAY_TTL", TTL)
    monitor, _ = primary_client(tmp_path)
    # The secondary draws boxes and exits: nothing more comes from it.
    assert monitor.remote_overlay(IngestOverlay(origin="codex 1", boxes=[BOX], arrows=[], seq=1))
    await asyncio.sleep(TTL.total_seconds() / 2)
    assert monitor.bus.phone.highlights == [BOX]
    await asyncio.sleep(TTL.total_seconds() * 2)
    assert monitor.bus.phone.highlights == []
    assert monitor.bus.phone.overlay_origin is None


async def test_a_newer_overlay_of_another_origin_stays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_names, "OVERLAY_TTL", TTL)
    monitor, _ = primary_client(tmp_path)
    monitor.remote_overlay(IngestOverlay(origin="codex 1", boxes=[BOX], arrows=[], seq=1))
    await asyncio.sleep(TTL.total_seconds() / 2)
    monitor.remote_overlay(IngestOverlay(origin="claude 2", boxes=[OTHER_BOX], arrows=[], seq=1))
    await asyncio.sleep(TTL.total_seconds() * 0.7)
    # The first origin expired, but the page shows the boxes of the second one: they stay.
    assert monitor.bus.phone.highlights == [OTHER_BOX]


def test_the_boxes_of_another_app_run_go(tmp_path: Path) -> None:
    monitor, _ = primary_client(tmp_path)
    monitor.remote_overlay(IngestOverlay(origin="codex 1", boxes=[BOX], arrows=[], seq=1, app_start_id=RUN_1))
    # A status of the same app run keeps them; a status of a new run (an app restart) removes them.
    monitor.remote_overlays.app_run(RUN_1)
    assert monitor.bus.phone.highlights == [BOX]
    monitor.remote_overlays.app_run(RUN_2)
    assert monitor.bus.phone.highlights == []


# endregion: C11 (a) the boxes of a secondary server on the primary page

# region: C11 (b) an old app with hidden markings


async def test_hidden_boxes_of_an_old_app_expire_too(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phone_names, "OVERLAY_TTL", TTL)
    phone = VisiblePhone(old_visible=True)
    services = services_for(settings, phone, tmp_path)
    await services.set_markings(False)
    await services.send_overlay([BOX], [])
    # The old app got nothing (it cannot hide boxes); our record has the box and its lifetime.
    assert phone.sent[-1] == []
    assert services.highlights == [BOX]
    await asyncio.sleep(TTL.total_seconds() * 3)
    assert services.highlights == []


# endregion: C11 (b) an old app with hidden markings

# region: N2 the screen of a secondary only for the selected phone


def test_the_primary_streams_only_the_selected_phone_for_a_secondary(tmp_path: Path) -> None:
    monitor, client = primary_client(tmp_path)
    screen = FakeScreen()
    monitor.screen = screen  # type: ignore[assignment]
    monitor.selected_serial = lambda: "R5CT1234567"
    headers = {ingest.TOKEN_HEADER: "secret-token"}
    refused = client.post(ingest.SCREEN_START_PATH, json={"serial": "192.0.2.50:5555"}, headers=headers)
    assert refused.status_code == 403
    assert "not the phone that the user selected" in refused.text
    assert screen.serials == []
    # Without a selection: nothing is streamed.
    monitor.selected_serial = lambda: ""
    assert client.post(ingest.SCREEN_START_PATH, json={"serial": "R5CT1234567"}, headers=headers).status_code == 403
    assert screen.serials == []


# endregion: N2 the screen of a secondary only for the selected phone

# region: N3 the settings lock and the event loop


async def test_a_held_lock_does_not_stop_the_event_loop(tmp_path: Path) -> None:
    store = SettingsStore.in_dir(tmp_path)
    handle = (tmp_path / f"{store.path.name}{LOCK_SUFFIX}").open("a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    # Another process holds the lock for 0.3 s.
    threading.Timer(0.3, lambda: (fcntl.flock(handle, fcntl.LOCK_UN), handle.close())).start()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    running = asyncio.create_task(ticker())
    started = time.monotonic()
    choice = MarkingsChoice(store)
    await choice.save(False)
    waited = time.monotonic() - started
    running.cancel()
    assert waited >= 0.25
    # The loop ran during the wait (about 30 ticks of 10 ms); a blocking wait gives 0 or 1.
    assert ticks >= 10
    assert store.load().markings_visible is False
    assert choice.visible is False


# endregion: N3 the settings lock and the event loop

# region: N4 the forward record


async def test_a_passing_removal_failure_keeps_the_forward_record(settings: Settings) -> None:
    fail_list = [True]

    def answer(command: list[str]) -> CommandResult:
        if command[1:] == ["forward", "--list"]:
            if fail_list[0]:
                return failed(b"adb: error: cannot connect to daemon")
            return ok(b"R5CT1234567 tcp:18765 tcp:8765\n")
        return ok()

    services = make_services(settings, FakePhone(), no_vision())
    services.adb = Adb(FakeRunner(answer), "adb", timedelta(seconds=1))
    services.forwarded_serial = "R5CT1234567"
    with pytest.raises(AdbError, match="cannot connect to daemon"):
        await services.release_forward()
    # The forward can still be there: a later stop tries again.
    assert services.forwarded_serial == "R5CT1234567"
    fail_list[0] = False
    note = await services.release_forward()
    assert "removed the forward" in note
    assert services.forwarded_serial is None


# endregion: N4 the forward record

# region: N7 saved webcam controls


async def test_the_saved_controls_say_what_the_camera_has(tmp_path: Path) -> None:
    store = WebcamControlStore(tmp_path / "webcam-controls.json")
    controls = V4l2Controls(fake_v4l2(), DEVICE, store, "v4l2-ctl")
    await controls.update(WebcamControls(auto_exposure=True))
    # A fixed exposure for the dim LCD: the camera goes to manual, and so does the saved value.
    await controls.update(WebcamControls(exposure=900))
    saved = store.load()
    assert (saved.auto_exposure, saved.exposure) == (False, 900)
    # After a restart, the camera gets the same values again (manual, 900).
    assert saved.settings() == {"auto_exposure": 1, "exposure_time_absolute": 900}
    # Auto exposure on again: the fixed time is gone from the saved values.
    await controls.update(WebcamControls(auto_exposure=True))
    saved = store.load()
    assert (saved.auto_exposure, saved.exposure) == (True, None)


# endregion: N7 saved webcam controls
