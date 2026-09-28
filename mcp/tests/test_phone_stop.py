"""Stop and switch the phone when the old device is already gone, Disconnect, and the "gone" state.

The adb is scripts/fake_adb.py with a state file (test_devices). The monitor removes the camera API forward with the
same adb (as ui/setup.py does). No real device.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from mcp import Client

from debug_devices_mcp.adb import AdbError, ForwardRemoval
from debug_devices_mcp.config import Settings
from debug_devices_mcp.devices import list_devices
from debug_devices_mcp.server import FORWARD_RELEASED, NO_OWN_FORWARD
from debug_devices_mcp.ui.constants import tools
from debug_devices_mcp.ui.device_panel import FORWARD_NOT_REMOVED, DevicePanel
from debug_devices_mcp.ui.monitor import OLD_PHONE_GONE, Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import SettingsStore

from .test_devices import PHONE, START, STATE, TV, Bench

# A second phone on USB: the user switches to it.
OTHER = "R5CT7654321"
FORWARD = "tcp:18765"


class StopBench(Bench):
    """test_devices.Bench, and a monitor that removes the forward with the fake adb."""

    def __init__(self, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(settings, tmp_path, monkeypatch)
        state = dict(STATE)
        state["devices"] = [*STATE["devices"], {"serial": OTHER, "state": "device", "model": "SM_S901B", "app": True}]
        self.state_file.write_text(json.dumps(state))

        # Set: the forward removal fails with this adb error (not a "gone" one).
        self.remove_error: str | None = None

        async def remove_forward() -> str:
            if self.remove_error is not None:
                raise AdbError(self.remove_error)
            return await self.services.release_forward()

        self.monitor = Monitor(
            START,
            SettingsStore.in_dir(tmp_path / "page"),
            MonitorOptions(open_browser=False, port=0),
            MonitorParts(forward_remover=remove_forward),
        )
        self.monitor.instrument(self.server)
        self.panel = DevicePanel(self.services, self.monitor)

    def change_state(self, change: Callable[[dict], None]) -> None:
        state = json.loads(self.state_file.read_text())
        change(state)
        self.state_file.write_text(json.dumps(state))


@pytest.fixture
def stop_bench(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StopBench:
    return StopBench(settings, tmp_path, monkeypatch)


def vanish(serial: str) -> Callable[[dict], None]:
    def change(state: dict) -> None:
        state["devices"] = [device for device in state["devices"] if device["serial"] != serial]

    return change


def offline(state: dict) -> None:
    state["devices"][0]["state"] = "offline"


def forward_lost(state: dict) -> None:
    state["forwards"] = []


async def use_phone(bench: StopBench) -> None:
    used = await bench.panel.select(PHONE)
    assert all(step.ok for step in used.steps), used.steps
    assert bench.monitor.bus.phone.serial == PHONE


ALREADY_REMOVED = FORWARD_RELEASED[ForwardRemoval.ALREADY_GONE].format(serial=PHONE, port=18765)


@pytest.mark.parametrize(
    ("change", "note"),
    [(vanish(PHONE), OLD_PHONE_GONE), (offline, OLD_PHONE_GONE), (forward_lost, ALREADY_REMOVED)],
    ids=["not-found", "offline", "listener"],
)
async def test_a_switch_away_from_a_gone_phone_works(
    stop_bench: StopBench, change: Callable[[dict], None], note: str
) -> None:
    await use_phone(stop_bench)
    stop_bench.change_state(change)
    switched = await stop_bench.panel.select(OTHER)
    assert all(step.ok for step in switched.steps), switched.steps
    [stop] = [step for step in switched.steps if step.step == "stop the old phone"]
    assert stop.detail == note
    assert stop_bench.services.selection.page_serial == OTHER
    assert stop_bench.monitor.bus.phone.serial == OTHER
    # The activity log row has the note.
    row = [call for call in stop_bench.monitor.bus.calls() if call.tool == tools.ADB_SELECT][-1]
    assert note in row.summary
    assert row.error is None
    assert TV not in stop_bench.device_serials_used()


async def test_disconnect_works_with_a_vanished_phone(stop_bench: StopBench) -> None:
    await use_phone(stop_bench)
    stop_bench.change_state(vanish(PHONE))
    result = await stop_bench.panel.disconnect()
    assert all(step.ok for step in result.steps), result.steps
    assert [step.step for step in result.steps] == ["stop the old phone", "clear the selection"]
    assert result.steps[0].detail == OLD_PHONE_GONE
    assert stop_bench.store.load().adb_serial is None
    assert stop_bench.monitor.bus.phone.serial is None
    assert stop_bench.monitor.bus.phone.status is None


async def test_disconnect_also_works_when_adb_cannot_remove_the_forward(stop_bench: StopBench) -> None:
    await use_phone(stop_bench)
    # Another adb failure (not "gone"): the action still clears the phone and the selection.
    stop_bench.remove_error = "cannot connect to daemon at tcp:5037"
    result = await stop_bench.panel.disconnect()
    assert all(step.ok for step in result.steps), result.steps
    assert result.steps[0].detail.startswith(FORWARD_NOT_REMOVED)
    assert stop_bench.store.load().adb_serial is None
    assert stop_bench.monitor.bus.phone.serial is None


async def test_disconnect_without_a_phone(stop_bench: StopBench) -> None:
    result = await stop_bench.panel.disconnect()
    assert all(step.ok for step in result.steps), result.steps
    assert result.steps[0].detail == NO_OWN_FORWARD
    assert not [call for call in stop_bench.calls() if "forward" in call]


def forward_calls(bench: StopBench) -> list[list[str]]:
    return [call for call in bench.calls() if "forward" in call]


async def test_a_second_stop_does_nothing(stop_bench: StopBench) -> None:
    # B-W1 of QA round 4: the real adb answers "listener not found" to a second remove.
    await use_phone(stop_bench)
    first = await stop_bench.panel.disconnect()
    assert first.steps[0].detail == f"stopped (removed the forward tcp:18765 of {PHONE})"
    before = len(forward_calls(stop_bench))
    second = await stop_bench.panel.disconnect()
    assert second.steps[0].detail == NO_OWN_FORWARD
    assert len(forward_calls(stop_bench)) == before
    # Use this phone again after a stop works (it failed at the stop step before).
    again = await stop_bench.panel.select(PHONE)
    assert all(step.ok for step in again.steps), again.steps


async def test_a_forward_that_goes_to_another_phone_now_is_kept(stop_bench: StopBench, settings: Settings) -> None:
    # S5 of QA round 4: another server (or its page) connected another phone on the same port. adb removes a port
    # whatever device it goes to, so this server must not remove it.
    await use_phone(stop_bench)
    await stop_bench.services.adb.forward(OTHER, settings.local_forward_port)
    result = await stop_bench.panel.disconnect()
    assert result.steps[0].detail == FORWARD_RELEASED[ForwardRemoval.OTHER_DEVICE].format(serial=PHONE, port=18765)
    assert not [call for call in forward_calls(stop_bench) if "--remove" in call]
    owners = await stop_bench.services.adb.forwards()
    assert [(owner.serial, owner.local) for owner in owners] == [(OTHER, "tcp:18765")]


async def test_the_gone_state_keeps_the_selection(stop_bench: StopBench) -> None:
    await use_phone(stop_bench)
    stop_bench.change_state(vanish(PHONE))
    services = stop_bench.services
    listed = await list_devices(services.adb, services.selection, "", services.discovery)
    assert listed.selected_gone is True
    assert listed.selected_serial == PHONE
    assert listed.selected_note is not None
    assert f"the selected phone {PHONE} is gone" in listed.selected_note
    # The user decides: the saved choice stays.
    assert stop_bench.store.load().adb_serial == PHONE
    stop_bench.change_state(lambda state: state["devices"].insert(0, {**STATE["devices"][0]}))
    back = await list_devices(services.adb, services.selection, "", services.discovery)
    assert (back.selected_gone, back.selected_note) == (False, None)


async def test_the_phone_tool_says_that_the_phone_is_gone(stop_bench: StopBench) -> None:
    await use_phone(stop_bench)
    stop_bench.change_state(vanish(PHONE))
    async with Client(stop_bench.server) as client:
        result = await client.call_tool("phone_connect", {})
    assert result.is_error
    text = result.content[0].text
    assert f"the selected phone {PHONE} is gone" in text
    assert "Disconnect" in text
    assert "exited with" not in text
    # A USB serial gets no adb connect.
    assert not [call for call in stop_bench.calls() if call[:1] == ["connect"]]
