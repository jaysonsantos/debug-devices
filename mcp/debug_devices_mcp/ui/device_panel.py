"""The Devices part of the phone panel: select the phone, switch it to Wi-Fi, pair or connect a Wi-Fi phone.

These are user actions in the page only. There is no MCP tool for them: an agent can only list the devices
(phone_devices). Each action is one log row (source ui) with its steps.
"""

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from debug_devices_mcp.adb import Adb, AdbError
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import adb
from debug_devices_mcp.devices import (
    DeviceList,
    PhoneSelection,
    SelectionNotSavedError,
    Transport,
    list_devices,
    transport_of,
)
from debug_devices_mcp.discovery import PhoneDiscovery
from debug_devices_mcp.ui.constants import tools
from debug_devices_mcp.ui.events import CallSource, CallStatus

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor

# After `adb tcpip`, the phone restarts its adb daemon: wait, then try `adb connect` a few times.
TCPIP_SETTLE = timedelta(seconds=2)
CONNECT_ATTEMPTS = 3
CONNECT_RETRY = timedelta(seconds=1)
PAIRING_CODE_PATTERN = r"^\d{6}$"
FORWARD_NOT_REMOVED = "the adb forward of the old phone was not removed"
ADDRESS_PATTERN = r"^[\w.\-]+:\d{1,5}$"


class DeviceAccess(Protocol):
    adb: Adb
    selection: PhoneSelection
    settings: Settings
    discovery: PhoneDiscovery


class Step(BaseModel):
    step: str
    ok: bool
    detail: str = ""


class DeviceAction(BaseModel):
    steps: list[Step]
    devices: DeviceList


class SelectBody(BaseModel):
    serial: str = Field(min_length=1)


class ConnectBody(BaseModel):
    address: str = Field(pattern=ADDRESS_PATTERN)


class PairBody(BaseModel):
    pair_address: str = Field(pattern=ADDRESS_PATTERN)
    code: str = Field(pattern=PAIRING_CODE_PATTERN)
    connect_address: str = Field(pattern=ADDRESS_PATTERN)


class StepFailed(Exception):
    """A step failed: the steps so far are in the result."""


class DevicePanel:
    def __init__(self, access: DeviceAccess, monitor: Monitor) -> None:
        self._access = access
        self._monitor = monitor
        self._lock = asyncio.Lock()

    async def devices(self) -> DeviceList:
        access = self._access
        return await list_devices(access.adb, access.selection, access.settings.adb_serial, access.discovery)

    async def _action(self, name: str, arguments: dict, work) -> DeviceAction:
        """Run one page action as one log row. A failed step ends it; the steps so far are in the result."""
        steps: list[Step] = []
        async with self._lock, self._monitor.bus.record(name, arguments, CallSource.UI) as call:
            try:
                await work(steps)
            except StepFailed:
                call.status = CallStatus.ERROR
                call.error = steps[-1].detail if steps else name
            call.summary = " · ".join(f"{step.step}: {step.detail or 'ok'}" for step in steps)
        return DeviceAction(steps=steps, devices=await self.devices())

    @staticmethod
    async def _step(steps: list[Step], name: str, coroutine) -> str:
        try:
            detail = await coroutine
        except (AdbError, ToolError) as exc:
            steps.append(Step(step=name, ok=False, detail=str(exc)))
            raise StepFailed from exc
        steps.append(Step(step=name, ok=True, detail=str(detail or "")))
        return str(detail or "")

    async def _stop_old_phone(self, steps: list[Step]) -> None:
        """Never blocks: an old phone that is gone, or a forward that adb cannot remove, is a note in the step."""
        try:
            note = await self._monitor.stop_phone()
        except AdbError as exc:
            note = f"{FORWARD_NOT_REMOVED}: {exc}"
        steps.append(Step(step="stop the old phone", ok=True, detail=note))

    async def _save_selection(self, steps: list[Step], name: str, serial: str | None, detail: str) -> None:
        """A failed save is a failed step (B-W8 of QA round 4): the choice did not change."""
        try:
            await self._access.selection.save(serial)
        except SelectionNotSavedError as exc:
            steps.append(Step(step=name, ok=False, detail=str(exc)))
            raise StepFailed from exc
        steps.append(Step(step=name, ok=True, detail=detail))

    async def _use(self, steps: list[Step], serial: str, connect: bool) -> None:
        """Store the choice, stop the old phone (screen stream, adb forward), then phone_connect on the new one. A
        choice that cannot be saved stops here, before the old phone stops."""
        listed = {device.serial: device for device in await self._access.adb.devices()}
        device = listed.get(serial)
        if device is None or device.state != adb.STATE_DEVICE:
            state = device.state if device is not None else "not connected"
            steps.append(Step(step="select", ok=False, detail=f"{serial} is {state}"))
            raise StepFailed
        await self._save_selection(steps, "select", serial, serial)
        await self._stop_old_phone(steps)
        if connect:
            await self._step(steps, "phone_connect", self._connect())

    async def _connect(self) -> str:
        call, result = await self._monitor.call_from_ui(tools.PHONE_CONNECT, {})
        if result.is_error:
            raise ToolError(call.summary)
        serial = (result.structured_content or {}).get("serial")
        return f"connected to {serial}" if serial else "connected"

    async def select(self, serial: str) -> DeviceAction:
        return await self._action(tools.ADB_SELECT, {"serial": serial}, lambda steps: self._use(steps, serial, True))

    async def disconnect(self) -> DeviceAction:
        """Always works: stop the phone (screen stream, status poll, adb forward) and clear the selection (back to
        the config, or none), also when the device is gone. No command goes to the device except the forward
        removal; the phone stays paired and connected in adb."""

        async def work(steps: list[Step]) -> None:
            await self._stop_old_phone(steps)
            config = self._access.settings.adb_serial or "none (select a phone)"
            await self._save_selection(steps, "clear the selection", None, f"back to the config: {config}")

        return await self._action(tools.ADB_DISCONNECT, {}, work)

    async def clear(self) -> DeviceAction:
        """The old "Clear selection" route (a page from before Disconnect): the same action."""
        return await self.disconnect()

    async def switch_to_wifi(self, serial: str) -> DeviceAction:
        """A USB phone: select it, read its Wi-Fi address, `adb tcpip`, `adb connect`, then use the Wi-Fi serial."""

        async def work(steps: list[Step]) -> None:
            if transport_of(serial) is not Transport.USB:
                steps.append(Step(step="check", ok=False, detail=f"{serial} is not a USB device"))
                raise StepFailed
            # The user chose this phone: from here, every device command goes to it only (-s).
            await self._use(steps, serial, connect=False)
            bridge = self._access.adb
            address = await self._step(steps, "Wi-Fi address", bridge.wifi_address(serial))
            await self._step(steps, f"adb tcpip {adb.TCPIP_PORT}", bridge.tcpip(serial))
            await asyncio.sleep(TCPIP_SETTLE.total_seconds())
            target = f"{address}:{adb.TCPIP_PORT}"
            await self._step(steps, f"adb connect {target}", self._connect_with_retry(target))
            await self._use(steps, target, connect=True)

        return await self._action(tools.ADB_WIFI, {"serial": serial}, work)

    async def _connect_with_retry(self, address: str) -> str:
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                return await self._access.adb.connect(address)
            except AdbError:
                if attempt == CONNECT_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(CONNECT_RETRY.total_seconds())
        raise AssertionError("unreachable")

    async def pair(self, body: PairBody) -> DeviceAction:
        async def work(steps: list[Step]) -> None:
            bridge = self._access.adb
            await self._step(steps, f"adb pair {body.pair_address}", bridge.pair(body.pair_address, body.code))
            await self._step(steps, f"adb connect {body.connect_address}", bridge.connect(body.connect_address))

        # The code is not logged.
        arguments = {"pair_address": body.pair_address, "connect_address": body.connect_address}
        return await self._action(tools.ADB_PAIR, arguments, work)

    async def connect(self, address: str) -> DeviceAction:
        async def work(steps: list[Step]) -> None:
            await self._step(steps, f"adb connect {address}", self._access.adb.connect(address))

        return await self._action(tools.ADB_CONNECT, {"address": address}, work)
