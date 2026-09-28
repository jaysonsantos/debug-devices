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
from debug_devices_mcp.devices import DeviceList, PhoneSelection, Transport, list_devices, transport_of
from debug_devices_mcp.ui.constants import tools
from debug_devices_mcp.ui.events import CallSource, CallStatus

if TYPE_CHECKING:
    from debug_devices_mcp.ui.monitor import Monitor

# After `adb tcpip`, the phone restarts its adb daemon: wait, then try `adb connect` a few times.
TCPIP_SETTLE = timedelta(seconds=2)
CONNECT_ATTEMPTS = 3
CONNECT_RETRY = timedelta(seconds=1)
PAIRING_CODE_PATTERN = r"^\d{6}$"
ADDRESS_PATTERN = r"^[\w.\-]+:\d{1,5}$"


class DeviceAccess(Protocol):
    adb: Adb
    selection: PhoneSelection
    settings: Settings


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
        return await list_devices(self._access.adb, self._access.selection, self._access.settings.adb_serial)

    async def _action(self, name: str, arguments: dict, work) -> DeviceAction:
        """Run one page action as one log row. A failed step ends it; the steps so far are in the result."""
        steps: list[Step] = []
        async with self._lock, self._monitor.bus.record(name, arguments, CallSource.UI) as call:
            try:
                await work(steps)
            except StepFailed:
                call.status = CallStatus.ERROR
                call.error = steps[-1].detail if steps else name
            call.summary = " · ".join(f"{step.step}: {'ok' if step.ok else step.detail}" for step in steps)
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

    async def _use(self, steps: list[Step], serial: str, connect: bool) -> None:
        """Stop the old phone (screen stream, adb forward), store the choice, then phone_connect on the new one."""
        listed = {device.serial: device for device in await self._access.adb.devices()}
        device = listed.get(serial)
        if device is None or device.state != adb.STATE_DEVICE:
            state = device.state if device is not None else "not connected"
            steps.append(Step(step="select", ok=False, detail=f"{serial} is {state}"))
            raise StepFailed
        await self._step(steps, "stop the old phone", self._monitor.stop_phone())
        self._access.selection.set(serial)
        steps.append(Step(step="select", ok=True, detail=serial))
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

    async def clear(self) -> DeviceAction:
        async def work(steps: list[Step]) -> None:
            await self._step(steps, "stop the old phone", self._monitor.stop_phone())
            self._access.selection.set(None)
            config = self._access.settings.adb_serial or "none (the only device)"
            steps.append(Step(step="clear", ok=True, detail=f"back to the config: {config}"))

        return await self._action(tools.ADB_CLEAR, {}, work)

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
