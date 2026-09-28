"""Choose the phone: the device list, the user's selection, Wi-Fi switch, pair, connect, and the reconnect.

The adb here is scripts/fake_adb.py with a state file (several devices), run as a real process. No real device.
"""

import json
import shlex
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client

from debug_devices_mcp.adb import Adb
from debug_devices_mcp.config import Settings
from debug_devices_mcp.devices import APP_NOT_SELECTED, PhoneSelection, SelectionSource
from debug_devices_mcp.process import SubprocessRunner
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.ui.device_panel import DevicePanel, PairBody
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore

from .test_server import FakePhone, make_services, no_vision

FAKE_ADB = Path(__file__).resolve().parents[2] / "scripts" / "fake_adb.py"
START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
PHONE = "R5CT1234567"
PHONE_WIFI = "192.0.2.23:5555"
# Another Android device on the network (a Fire TV): it must never get a command.
TV = "192.0.2.50:5555"
STATE = {
    "devices": [
        {
            "serial": PHONE,
            "state": "device",
            "model": "SM_A556B",
            "product": "a55",
            "app": True,
            "wlan": "192.0.2.23",
        },
        {"serial": TV, "state": "device", "model": "AFTMM", "product": "firetv", "app": False},
        {"serial": "ZY22ABC", "state": "unauthorized"},
    ],
    "pairing": {"192.0.2.60:37000": "123456"},
    "wireless": {"192.0.2.60:41000": PHONE},
}


class Bench:
    def __init__(self, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.state_file = tmp_path / "adb-state.json"
        self.state_file.write_text(json.dumps(STATE))
        self.log_file = tmp_path / "adb.log"
        monkeypatch.setenv("FAKE_ADB_STATE", str(self.state_file))
        monkeypatch.setenv("FAKE_ADB_LOG", str(self.log_file))
        monkeypatch.setattr("debug_devices_mcp.ui.device_panel.TCPIP_SETTLE", timedelta(0))
        self.store = SettingsStore.in_dir(tmp_path / "state")
        self.services: Services = make_services(settings, FakePhone(), no_vision())
        self.services.adb = Adb(SubprocessRunner(), str(FAKE_ADB), timedelta(seconds=10))
        self.services.selection = PhoneSelection(self.store)
        self.monitor = Monitor(
            START, SettingsStore.in_dir(tmp_path / "page"), MonitorOptions(open_browser=False, port=0)
        )
        self.server = build_server(self.services)
        self.monitor.instrument(self.server)
        self.panel = DevicePanel(self.services, self.monitor)

    def calls(self) -> list[list[str]]:
        if not self.log_file.exists():
            return []
        return [shlex.split(line.split(" ", 1)[1]) for line in self.log_file.read_text().splitlines()]

    def device_serials_used(self) -> set[str]:
        return {call[1] for call in self.calls() if call[:1] == ["-s"]}


@pytest.fixture
def bench(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    return Bench(settings, tmp_path, monkeypatch)


async def test_the_list_and_no_command_to_other_wifi_devices(bench: Bench) -> None:
    devices = await bench.panel.devices()
    by_serial = {device.serial: device for device in devices.devices}
    phone, tv, locked = by_serial[PHONE], by_serial[TV], by_serial["ZY22ABC"]
    # Nothing is selected: no device gets `pm path`, also not the USB phone (S2 of QA round 4).
    assert (phone.transport, phone.state, phone.model, phone.product, phone.app_installed) == (
        "usb",
        "device",
        "SM_A556B",
        "a55",
        None,
    )
    assert phone.note == APP_NOT_SELECTED
    assert (tv.transport, tv.app_installed, tv.note) == ("wifi", None, APP_NOT_SELECTED)
    assert locked.state == "unauthorized"
    assert "allow USB debugging" in (locked.note or "")
    assert devices.selected_from is SelectionSource.NONE
    assert "not supported" in (devices.mdns_note or "")
    assert "Only the user selects the phone" in devices.note
    assert bench.device_serials_used() == set()
    # The selected phone gets the check.
    bench.services.selection.set(PHONE)
    selected = {device.serial: device for device in (await bench.panel.devices()).devices}
    assert selected[PHONE].app_installed is True
    assert bench.device_serials_used() == {PHONE}


async def test_the_server_never_picks_one_of_several_devices(bench: Bench) -> None:
    async with Client(bench.server) as client:
        result = await client.call_tool("phone_connect", {})
    assert result.is_error
    assert "select the phone in the monitor page (Devices)" in result.content[0].text
    # No command to any device: the server never picks one (S1 of QA round 4).
    assert bench.device_serials_used() == set()


async def test_selection_is_saved_and_replaces_the_config(bench: Bench, settings: Settings) -> None:
    bench.services.settings = settings.model_copy(update={"adb_serial": "CONFIG123"})
    assert bench.services.selection.effective("CONFIG123") == ("CONFIG123", SelectionSource.CONFIG)
    action = await bench.panel.select(PHONE)
    # The choice is saved first: a choice that cannot be saved does not stop the old phone (B-W8).
    assert [step.step for step in action.steps] == ["select", "stop the old phone", "phone_connect"]
    assert all(step.ok for step in action.steps), action.steps
    assert bench.store.load().adb_serial == PHONE
    # Another server (a new selection object on the same file) sees it.
    assert PhoneSelection(bench.store).effective("CONFIG123") == (PHONE, SelectionSource.PAGE)
    assert action.devices.selected_serial == PHONE
    cleared = await bench.panel.clear()
    assert cleared.steps[-1].detail == "back to the config: CONFIG123"
    assert bench.store.load().adb_serial is None
    [row] = [call for call in bench.monitor.bus.calls() if call.tool == "adb_select"]
    assert row.source == "ui"


async def test_an_unauthorized_device_cannot_be_used(bench: Bench) -> None:
    action = await bench.panel.select("ZY22ABC")
    assert action.steps[-1].ok is False
    assert "unauthorized" in action.steps[-1].detail
    assert bench.store.load().adb_serial is None


async def test_switch_to_wifi(bench: Bench) -> None:
    action = await bench.panel.switch_to_wifi(PHONE)
    assert all(step.ok for step in action.steps), action.steps
    names = [step.step for step in action.steps]
    assert names[:5] == ["select", "stop the old phone", "Wi-Fi address", "adb tcpip 5555", f"adb connect {PHONE_WIFI}"]
    assert names[-1] == "phone_connect"
    assert bench.store.load().adb_serial == PHONE_WIFI
    assert PHONE_WIFI in {device.serial for device in action.devices.devices}
    # Every device command went to the chosen phone (USB, then its Wi-Fi serial), never to the TV.
    assert bench.device_serials_used() <= {PHONE, PHONE_WIFI}
    assert ["connect", PHONE_WIFI] in bench.calls()


async def test_a_lost_wifi_phone_gets_one_connect(bench: Bench) -> None:
    # The phone is in tcpip mode, but adb lost it (not in the list).
    state = json.loads(bench.state_file.read_text())
    state["devices"][0]["tcpip"] = 5555
    bench.state_file.write_text(json.dumps(state))
    bench.services.selection.set(PHONE_WIFI)
    async with Client(bench.server) as client:
        connected = await client.call_tool("phone_connect", {})
        assert not connected.is_error, connected.content
        assert connected.structured_content["serial"] == PHONE_WIFI
        bench.services.selection.set("10.0.0.9:5555")
        lost = await client.call_tool("phone_connect", {})
    assert [call for call in bench.calls() if call[:1] == ["connect"]] == [
        ["connect", PHONE_WIFI],
        ["connect", "10.0.0.9:5555"],
    ]
    assert lost.is_error
    # A clear message (the phone is gone, what the user does), with the adb reason at the end.
    assert "the selected phone 10.0.0.9:5555 is gone" in lost.content[0].text
    assert "adb connect failed" in lost.content[0].text


async def test_pair_and_connect(bench: Bench) -> None:
    wrong = await bench.panel.pair(
        PairBody(pair_address="192.0.2.60:37000", code="000000", connect_address="192.0.2.60:41000")
    )
    assert wrong.steps[-1].ok is False
    paired = await bench.panel.pair(
        PairBody(pair_address="192.0.2.60:37000", code="123456", connect_address="192.0.2.60:41000")
    )
    assert all(step.ok for step in paired.steps), paired.steps
    assert "192.0.2.60:41000" in {device.serial for device in paired.devices.devices}
    # The code is not in the log.
    assert all("123456" not in json.dumps(call.arguments) for call in bench.monitor.bus.calls())
    plain = await bench.panel.connect("192.0.2.99:5555")
    assert plain.steps[-1].ok is False
    assert "failed to connect" in plain.steps[-1].detail


async def test_the_agent_tool_is_read_only(bench: Bench) -> None:
    async with Client(bench.server) as client:
        tools = {tool.name: tool.description or "" for tool in (await client.list_tools()).tools}
        listed = await client.call_tool("phone_devices", {})
    assert "only the user selects the phone in the monitor page" in tools["phone_devices"]
    assert not {name for name in tools if "pair" in name or "select" in name or "tcpip" in name}
    assert listed.structured_content is not None
    assert {device["serial"] for device in listed.structured_content["devices"]} == {PHONE, TV, "ZY22ABC"}
    assert bench.store.load().adb_serial is None
