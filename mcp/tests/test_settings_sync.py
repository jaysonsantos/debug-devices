"""Two MCP servers and one phone: no settings fight (the camera rebound every ~7 s on 2026-09-27).

The rule (app_start.py): send a stored setting again only after an app restart (a new app_start_id) or phone_connect,
never because the phone status differs. The settings file is the one source of truth for all servers.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest

from debug_devices_mcp.camera_choice import AfModeChoice, InSensorZoomChoice
from debug_devices_mcp.config import Settings
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.orientation import OrientationState
from debug_devices_mcp.server import Services
from debug_devices_mcp.ui.settings import SettingsStore

from .test_af_mode import AfPhone
from .test_server import make_services, no_vision

ROUNDS = 20


class AllPhone(AfPhone):
    """The fake phone with /v1/camera (in-sensor zoom and af_mode) and /v1/preview."""

    def __init__(self) -> None:
        super().__init__()
        self.status.update(in_sensor_zoom="off", preview_flip_horizontal=False, preview_flip_vertical=False)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/preview":
            body = json.loads(request.content)
            self.bodies.append({"preview": body})
            self.status.update(
                preview_flip_horizontal=body["flip_horizontal"], preview_flip_vertical=body["flip_vertical"]
            )
            return httpx.Response(200, json=self.status)
        if request.url.path == "/v1/camera":
            body = json.loads(request.content)
            if "in_sensor_zoom" in body:
                self.status["in_sensor_zoom"] = "on" if body["in_sensor_zoom"] else "off"
        return super().handle(request)

    def restart_app(self) -> None:
        super().restart_app()
        self.status.update(in_sensor_zoom="off", preview_flip_horizontal=False, preview_flip_vertical=False)


def server(settings: Settings, phone: AllPhone, directory: Path) -> Services:
    """One MCP server with its settings in `directory` (the same directory for servers of the same user)."""
    services = make_services(settings, phone, no_vision())
    store = SettingsStore.in_dir(directory)
    services.orientation = OrientationState(store)
    services.in_sensor_zoom = InSensorZoomChoice(store)
    services.af_mode = AfModeChoice(store)
    services.__post_init__()
    return services


async def poll(servers: list[Services], rounds: int = ROUNDS) -> None:
    """The status polls of each server's monitor, one after the other."""
    for _ in range(rounds):
        for services in servers:
            await services.sync_phone(await services.phone.status())


def sends(phone: AllPhone) -> int:
    return len(phone.bodies)


async def test_servers_with_different_values_do_not_fight(settings: Settings, tmp_path: Path) -> None:
    # The worst case: two servers with different stored values (the bug had two in-memory copies).
    phone = AllPhone()
    first, second = server(settings, phone, tmp_path / "a"), server(settings, phone, tmp_path / "b")
    first.in_sensor_zoom.set(True)
    first.af_mode.set("macro")
    first.orientation.update(flip_horizontal=True)
    await poll([first, second])
    # Each server may send once at its first status after the start; then nobody sends again.
    after_start = sends(phone)
    assert after_start <= 6
    await poll([first, second])
    assert sends(phone) == after_start


async def test_servers_that_share_the_file_agree(settings: Settings, tmp_path: Path) -> None:
    phone = AllPhone()
    first, second = server(settings, phone, tmp_path), server(settings, phone, tmp_path)
    seen: list[bool] = []
    second.in_sensor_zoom.add_listener(seen.append)
    await poll([first, second], rounds=2)
    assert sends(phone) == 0  # the phone already matches the defaults
    # The page or an agent on the first server turns the in-sensor zoom on: that server sends it.
    first.in_sensor_zoom.set(True)
    await first.in_sensor_zoom_sync.send()
    assert second.in_sensor_zoom.enabled is True  # the file changed: the second server sees it at once
    await poll([first, second])
    assert sends(phone) == 1
    assert seen == [True]  # the second server's page heard about it one time


async def test_an_app_restart_gets_one_resend(settings: Settings, tmp_path: Path) -> None:
    phone = AllPhone()
    first, second = server(settings, phone, tmp_path), server(settings, phone, tmp_path)
    first.in_sensor_zoom.set(True)
    first.orientation.update(flip_vertical=True)
    await poll([first, second])
    before = sends(phone)
    phone.restart_app()
    await poll([first, second])
    # One server sends the flips and the zoom again; the other then sees the phone already right.
    resent = phone.bodies[before:]
    assert resent == [{"preview": {"flip_horizontal": False, "flip_vertical": True}}, {"in_sensor_zoom": True}]
    assert phone.status["in_sensor_zoom"] == "on"


async def test_another_client_change_is_not_undone(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    phone = AllPhone()
    services = server(settings, phone, tmp_path)
    await poll([services], rounds=2)
    # Another client turns the zoom on in the same app run (for example an older server that writes no file).
    phone.status["in_sensor_zoom"] = "on"
    with caplog.at_level(logging.INFO, logger="debug_devices_mcp.app_start"):
        await poll([services])
    assert sends(phone) == 0
    assert phone.status["in_sensor_zoom"] == "on"
    assert sum("changed by another client" in record.message for record in caplog.records) == 1


async def test_an_old_app_gets_the_settings_only_at_phone_connect(settings: Settings, tmp_path: Path) -> None:
    phone = AllPhone()
    del phone.status["app_start_id"]
    services = server(settings, phone, tmp_path)
    services.in_sensor_zoom.set(True)
    await poll([services], rounds=1)
    assert sends(phone) == 1  # the first status (as after phone_connect)
    phone.restart_app()
    del phone.status["app_start_id"]
    await poll([services])
    assert sends(phone) == 1  # no periodic resend: a restart of an old app is not visible
    services.reset_phone_syncs()  # phone_connect
    await poll([services], rounds=1)
    assert sends(phone) == 2


def test_the_store_reads_a_file_that_another_server_wrote(tmp_path: Path) -> None:
    mine, other = SettingsStore.in_dir(tmp_path), SettingsStore.in_dir(tmp_path)
    assert mine.current().snapshot_orientation is None
    flipped = SnapshotOrientation(flip_horizontal=True, flip_vertical=False)
    other.save(other.load().model_copy(update={"snapshot_orientation": flipped}))
    assert mine.current().snapshot_orientation == flipped
    other.save(other.load().model_copy(update={"af_mode": "macro"}))
    assert mine.current().af_mode == "macro"
    # No change: no new read of the file.
    cached = mine.current()
    assert mine.current() is cached
