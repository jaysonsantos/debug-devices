"""Device safety from QA round 4. Batch 1: serial classes (S3), the page-only device routes (S4), the screen cleanup
after a switch (S6), "listener not found" (B-W1), and the pairing code in errors and the log (B-W3); S1, S2, and S5 are
in test_adb.py, test_devices.py, and test_phone_stop.py. Batch 2: the offline Wi-Fi reconnect (B-W5), a failed
selection save (B-W8), and the settings file lock (B-W9)."""

import asyncio
import fcntl
import json
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import IO

import pytest
from mcp import Client
from starlette.testclient import TestClient

from debug_devices_mcp.adb import REDACTED, Adb, AdbError
from debug_devices_mcp.devices import PhoneSelection, SelectionNotSavedError, Transport, transport_of
from debug_devices_mcp.phone_screen import PhoneScreen, PhoneScreenOptions
from debug_devices_mcp.process import CommandError, CommandResult
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.device_panel import PairBody
from debug_devices_mcp.ui.settings import LOCK_SUFFIX, SettingsStore, UiSettings

from .conftest import FakeRunner, failed, ok
from .test_devices import PHONE, PHONE_WIFI, Bench, bench  # noqa: F401
from .test_scrcpy import FakeProcess

BASE_URL = "http://127.0.0.1:18766"
CODE = "482913"

# region: S3 serial classes


@pytest.mark.parametrize(
    ("serial", "transport"),
    [
        ("R5CT1234567", Transport.USB),
        ("emulator-5554", Transport.USB),
        ("192.0.2.23:5555", Transport.WIFI),
        ("firetv.lan:5555", Transport.WIFI),
        ("[fe80::1%wlan0]:5555", Transport.WIFI),
        ("[2001:db8::7]:41023", Transport.WIFI),
        ("adb-R5CT1234567-Ab12Cd._adb-tls-connect._tcp", Transport.WIFI),
        ("adb-G0911X03944704D0._adb._tcp", Transport.WIFI),
    ],
)
def test_network_serials_are_wifi(serial: str, transport: Transport) -> None:
    assert transport_of(serial) is transport


async def test_a_host_name_serial_gets_no_switch_and_no_app_check(bench: Bench) -> None:  # noqa: F811
    # After `adb connect firetv.lan:5555` the device is Wi-Fi: no pm path, and the switch refuses (no tcpip).
    state = json.loads(bench.state_file.read_text())
    state["devices"].append({"serial": "firetv.lan:5555", "state": "device", "model": "AFTR", "app": False})
    bench.state_file.write_text(json.dumps(state))
    listed = {device.serial: device for device in (await bench.panel.devices()).devices}
    assert listed["firetv.lan:5555"].transport is Transport.WIFI
    assert listed["firetv.lan:5555"].app_installed is None
    switched = await bench.panel.switch_to_wifi("firetv.lan:5555")
    assert not switched.steps[-1].ok
    assert "firetv.lan:5555" not in bench.device_serials_used()


# endregion: S3 serial classes

# region: S4 page-only device routes

DEVICE_POSTS = [
    ("/api/devices/select", {"serial": PHONE}),
    ("/api/devices/disconnect", {}),
    ("/api/devices/clear", {}),
    ("/api/devices/wifi", {"serial": PHONE}),
    ("/api/devices/pair", {"pair_address": "192.0.2.60:37000", "code": CODE, "connect_address": "192.0.2.60:41000"}),
    ("/api/devices/connect", {"address": "192.0.2.60:41000"}),
]


@pytest.mark.parametrize(("path", "body"), DEVICE_POSTS)
def test_device_routes_refuse_a_request_without_the_page_origin(
    bench: Bench,  # noqa: F811
    path: str,
    body: dict,
) -> None:
    bench.monitor.device_panel = bench.panel
    client = TestClient(create_app(bench.monitor), base_url=BASE_URL)
    # No Origin (curl, a script, an agent): refused, and nothing reaches adb.
    assert client.post(path, json=body).status_code == 403
    # Another site: refused (LocalOnly).
    assert client.post(path, json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    # Another local port (another local web page): refused.
    assert client.post(path, json=body, headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert bench.calls() == []
    assert bench.store.load().adb_serial is None


def test_the_page_origin_can_use_the_device_routes(bench: Bench) -> None:  # noqa: F811
    bench.monitor.device_panel = bench.panel
    client = TestClient(create_app(bench.monitor), base_url=BASE_URL)
    response = client.post("/api/devices/disconnect", json={}, headers={"Origin": BASE_URL})
    assert response.status_code == 200, response.text
    # The list is read-only and stays open to a GET.
    assert client.get("/api/devices").status_code == 200


# endregion: S4 page-only device routes

# region: S6 the screen cleanup after a switch


async def test_the_screen_cleanup_uses_the_old_serial_for_the_old_port(tmp_path: Path) -> None:
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"jar")
    ports = {"OLD1234": b"40000\n", "NEW5678": b"40001\n"}

    def respond(command: list[str]) -> CommandResult:
        if "forward" in command and "--remove" not in command:
            return ok(ports[command[2]])
        return ok()

    started = asyncio.Event()

    async def spawner(args: Sequence[str], environ: Mapping[str, str], log: IO[bytes] | None) -> FakeProcess:
        started.set()
        await asyncio.Event().wait()  # the old session runs until the switch cancels it
        raise AssertionError("unreachable")

    runner = FakeRunner(respond)
    options = PhoneScreenOptions(
        adb_path="adb", server_path=server_file, version="4.1", restart_delay=timedelta(seconds=10)
    )
    phone_screen = PhoneScreen(options, runner, spawner=spawner, on_state=lambda state: None)
    phone_screen.ensure_running("OLD1234")
    await asyncio.wait_for(started.wait(), 2)
    phone_screen.ensure_running("NEW5678")
    for _ in range(100):
        if ["adb", "-s", "OLD1234", "forward", "--remove", "tcp:40000"] in runner.calls:
            break
        await asyncio.sleep(0.01)
    await phone_screen.stop()
    assert ["adb", "-s", "OLD1234", "forward", "--remove", "tcp:40000"] in runner.calls
    assert ["adb", "-s", "NEW5678", "forward", "--remove", "tcp:40000"] not in runner.calls


# endregion: S6 the screen cleanup after a switch

# region: B-W1 and B-W3 adb errors


async def test_listener_not_found_is_not_an_error() -> None:
    runner = FakeRunner(lambda _: failed(b"adb: error: listener 'tcp:18765' not found\n"))
    adb = Adb(runner, "adb", timedelta(seconds=1))
    assert await adb.remove_forward(PHONE, 18765) is False


async def test_the_pairing_code_is_redacted_in_adb_errors() -> None:
    wrong = Adb(FakeRunner(lambda _: failed(b"Failed: Unable to start pairing client.\n")), "adb", timedelta(seconds=1))
    with pytest.raises(AdbError) as failure:
        await wrong.pair("192.0.2.60:37000", CODE)
    assert CODE not in str(failure.value)
    assert REDACTED in str(failure.value)

    class TimeoutRunner:
        async def run(self, args: Sequence[str], timeout: timedelta) -> CommandResult:
            raise CommandError(f"command timed out after {timeout}: {' '.join(args)}")

    slow = Adb(TimeoutRunner(), "adb", timedelta(seconds=1))
    with pytest.raises(AdbError) as timeout:
        await slow.pair("192.0.2.60:37000", CODE)
    assert CODE not in str(timeout.value)
    assert timeout.value.__cause__ is None
    assert timeout.value.__suppress_context__


async def test_a_failed_pair_keeps_the_code_out_of_the_log(bench: Bench) -> None:  # noqa: F811
    result = await bench.panel.pair(
        PairBody(pair_address="192.0.2.60:37000", code=CODE, connect_address="192.0.2.60:41000")
    )
    assert not result.steps[-1].ok
    row = bench.monitor.bus.calls()[-1]
    text = " ".join([row.summary or "", row.error or "", str(row.arguments), *(step.detail for step in result.steps)])
    assert CODE not in text
    assert REDACTED in text


# endregion: B-W1 and B-W3 adb errors


# region: batch 2 (B-W5, B-W8, B-W9)


async def test_an_offline_wifi_phone_is_disconnected_then_connected(bench: Bench) -> None:  # noqa: F811
    # adb keeps the lost Wi-Fi connection as offline and says "already connected": a plain connect does not help.
    state = json.loads(bench.state_file.read_text())
    state["devices"][0]["tcpip"] = 5555
    state["devices"].append({**state["devices"][0], "serial": PHONE_WIFI, "state": "offline", "tcpip": None})
    bench.state_file.write_text(json.dumps(state))
    bench.services.selection.set(PHONE_WIFI)
    async with Client(bench.server) as client:
        connected = await client.call_tool("phone_connect", {})
    assert not connected.is_error, connected.content
    host_calls = [call for call in bench.calls() if call[:1] in (["disconnect"], ["connect"])]
    assert host_calls == [["disconnect", PHONE_WIFI], ["connect", PHONE_WIFI]]


async def test_a_selection_that_cannot_be_saved_is_a_failed_step(
    bench: Bench,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full_disk(change: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bench.store, "update", full_disk)
    action = await bench.panel.select(PHONE)
    assert [(step.step, step.ok) for step in action.steps] == [("select", False)]
    assert "cannot save the selected phone" in action.steps[0].detail
    # The old phone keeps running: nothing stopped, nothing connected.
    assert not [call for call in bench.calls() if "forward" in call]
    row = bench.monitor.bus.calls()[-1]
    assert row.error is not None
    cleared = await bench.panel.disconnect()
    assert [(step.step, step.ok) for step in cleared.steps] == [
        ("stop the old phone", True),
        ("clear the selection", False),
    ]


def test_two_writers_do_not_lose_a_change(tmp_path: Path) -> None:
    store_a, store_b = SettingsStore.in_dir(tmp_path), SettingsStore.in_dir(tmp_path)
    inside = threading.Event()

    def slow_change(saved: UiSettings) -> UiSettings:
        inside.set()
        time.sleep(0.2)  # the other writer tries now
        return saved.model_copy(update={"af_mode": "macro"})

    writer = threading.Thread(target=store_a.update, args=(slow_change,))
    writer.start()
    assert inside.wait(2)
    store_b.update(lambda saved: saved.model_copy(update={"in_sensor_zoom": True}))
    writer.join()
    final = SettingsStore.in_dir(tmp_path).load()
    assert (final.af_mode, final.in_sensor_zoom) == ("macro", True)


def test_a_held_lock_makes_the_save_fail_after_the_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("debug_devices_mcp.ui.settings.LOCK_TIMEOUT", timedelta(milliseconds=50))
    store = SettingsStore.in_dir(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    with (tmp_path / f"{store.path.name}{LOCK_SUFFIX}").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(OSError, match="locked"):
            store.update(lambda saved: saved)
        with pytest.raises(SelectionNotSavedError):
            PhoneSelection(store).set(PHONE)
    assert store.load().adb_serial is None


# endregion: batch 2 (B-W5, B-W8, B-W9)
