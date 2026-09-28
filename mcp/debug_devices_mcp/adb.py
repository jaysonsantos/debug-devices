"""ADB commands: pick the device, forward the port, start the camera app."""

import re
from datetime import timedelta

from pydantic import BaseModel

from debug_devices_mcp.constants import adb, phone
from debug_devices_mcp.process import CommandError, CommandResult, CommandRunner


class AdbDevice(BaseModel):
    serial: str
    state: str
    description: str = ""

    def detail(self, key: str) -> str:
        """A `key:value` detail of `adb devices -l`, for example model or product."""
        for token in self.description.split():
            name, _, value = token.partition(":")
            if name == key:
                return value
        return ""


class MdnsService(BaseModel):
    """A wireless-debugging phone that adb sees on the network (`adb mdns services`)."""

    name: str
    service: str
    address: str


MDNS_COLUMNS = 3
INET_ADDRESS = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")
# `pm path` is quick; a phone that does not answer in this time shows "unknown".
APP_CHECK_TIMEOUT = timedelta(seconds=3)


class AdbError(Exception):
    """An adb command failed, or the target device is not clear."""


class AppNotInstalledError(AdbError):
    """The camera app activity does not exist on the device."""


def parse_devices(output: str) -> list[AdbDevice]:
    """Parse `adb devices -l` output. Header and daemon lines are skipped."""
    devices: list[AdbDevice] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((adb.DAEMON_LINE_PREFIX, adb.DEVICES_HEADER)):
            continue
        serial, state, *rest = stripped.split(maxsplit=2)
        devices.append(AdbDevice(serial=serial, state=state, description="".join(rest)))
    return devices


def parse_mdns(output: str) -> list[MdnsService]:
    """Parse `adb mdns services`: name, service type, and address per line after the header."""
    services = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == MDNS_COLUMNS and ":" in parts[2]:
            services.append(MdnsService(name=parts[0], service=parts[1], address=parts[2]))
    return services


def parse_wifi_address(output: str) -> str | None:
    match = INET_ADDRESS.search(output)
    return match.group(1) if match else None


class Adb:
    def __init__(self, runner: CommandRunner, adb_path: str, timeout: timedelta) -> None:
        self._runner = runner
        self._adb_path = adb_path
        self._timeout = timeout

    async def devices(self) -> list[AdbDevice]:
        result = await self._run(adb.DEVICES, adb.LONG_FLAG)
        return parse_devices(result.stdout.decode(errors="replace"))

    async def select_device(self, serial: str) -> AdbDevice:
        """Return the device with `serial`, or the only ready device when `serial` is empty."""
        devices = await self.devices()
        if serial:
            for device in devices:
                if device.serial == serial:
                    if device.state != adb.STATE_DEVICE:
                        raise AdbError(f"device {serial} is in state {device.state!r}, not {adb.STATE_DEVICE!r}")
                    return device
            raise AdbError(f"device {serial} is not connected. Connected: {_describe(devices)}")
        ready = [device for device in devices if device.state == adb.STATE_DEVICE]
        if not ready:
            raise AdbError(f"no ready adb device. Connected: {_describe(devices)}")
        if len(ready) > 1:
            raise AdbError(
                "several adb devices are connected; select the phone in the monitor page (Devices), or set "
                f"--adb-serial or DEBUG_DEVICES_ADB_SERIAL to one of: "
                f"{_describe(ready)}"
            )
        return ready[0]

    async def forward(self, serial: str, local_port: int, device_port: int = phone.DEVICE_PORT) -> None:
        await self._run(
            adb.SERIAL_FLAG, serial, adb.FORWARD, f"{adb.TCP_PREFIX}{local_port}", f"{adb.TCP_PREFIX}{device_port}"
        )

    async def remove_forward(self, serial: str, local_port: int) -> None:
        await self._run(adb.SERIAL_FLAG, serial, adb.FORWARD, adb.REMOVE_FLAG, f"{adb.TCP_PREFIX}{local_port}")

    async def start_app(self, serial: str) -> None:
        try:
            result = await self._run(adb.SERIAL_FLAG, serial, adb.SHELL, *adb.AM_START, phone.COMPONENT)
        except AdbError as exc:
            if adb.AM_MISSING_MARKER in str(exc):
                raise AppNotInstalledError(_not_installed(serial)) from exc
            raise
        output = result.stdout.decode(errors="replace")
        # Some Android versions exit with 0 even when the activity does not exist.
        if adb.AM_MISSING_MARKER in output:
            raise AppNotInstalledError(_not_installed(serial))
        if adb.AM_ERROR_MARKER in output:
            raise AdbError(f"am start failed on {serial}: {output.strip()}")

    # region: device list and Wi-Fi (only `devices`, `mdns`, `connect`, and `pair` go without -s)

    async def mdns_services(self) -> list[MdnsService] | str:
        """The wireless-debugging phones on the network, or why this adb cannot list them."""
        try:
            result = await self._run(*adb.MDNS)
        except AdbError as exc:
            return str(exc)
        output = result.stdout.decode(errors="replace")
        if adb.MDNS_UNSUPPORTED_MARKER in output:
            return output.strip()
        return parse_mdns(output)

    async def app_installed(self, serial: str) -> bool | None:
        """True when our camera app is on the device, None when the device does not answer."""
        try:
            result = await self._runner.run(
                [self._adb_path, adb.SERIAL_FLAG, serial, adb.SHELL, *adb.PM_PATH, phone.PACKAGE], APP_CHECK_TIMEOUT
            )
        except CommandError:
            return None
        output = result.stdout.decode(errors="replace")
        if adb.PACKAGE_PREFIX in output:
            return True
        return False if not result.stderr.strip() else None

    async def wifi_address(self, serial: str) -> str:
        result = await self._run(adb.SERIAL_FLAG, serial, adb.SHELL, *adb.WIFI_ADDRESS)
        address = parse_wifi_address(result.stdout.decode(errors="replace"))
        if address is None:
            raise AdbError(f"{serial} has no Wi-Fi address (wlan0): connect the phone to the Wi-Fi first")
        return address

    async def tcpip(self, serial: str, port: int = adb.TCPIP_PORT) -> str:
        result = await self._run(adb.SERIAL_FLAG, serial, adb.TCPIP, str(port))
        return result.stdout.decode(errors="replace").strip()

    async def connect(self, address: str) -> str:
        """`adb connect` exits 0 also when it fails: the output says it."""
        result = await self._run(adb.CONNECT, address)
        output = result.stdout.decode(errors="replace").strip()
        if not output.startswith(adb.CONNECTED_MARKERS):
            raise AdbError(f"adb connect {address}: {output or 'no answer'}")
        return output

    async def pair(self, address: str, code: str) -> str:
        result = await self._run(adb.PAIR, address, code)
        output = result.stdout.decode(errors="replace").strip()
        if adb.PAIRED_MARKER not in output:
            raise AdbError(f"adb pair {address}: {output or 'no answer'}")
        return output

    # endregion: device list and Wi-Fi

    async def _run(self, *args: str) -> CommandResult:
        command = [self._adb_path, *args]
        try:
            result = await self._runner.run(command, self._timeout)
        except CommandError as exc:
            raise AdbError(str(exc)) from exc
        if not result.ok:
            stderr = result.stderr.decode(errors="replace").strip()
            raise AdbError(f"{' '.join(command)} exited with {result.returncode}: {stderr}")
        return result


def _not_installed(serial: str) -> str:
    return f"the camera app {phone.PACKAGE} is not installed on {serial}. Build and install android/ first."


def _describe(devices: list[AdbDevice]) -> str:
    if not devices:
        return "none"
    return ", ".join(f"{d.serial} ({' '.join(filter(None, [d.state, d.description]))})" for d in devices)
