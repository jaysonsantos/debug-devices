"""ADB commands: pick the device, forward the port, start the camera app."""

import re
from datetime import timedelta
from enum import StrEnum

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
FORWARD_COLUMNS = 3
INET_ADDRESS = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")
# adb errors that mean: the device is already gone, for example after it left the Wi-Fi.
DEVICE_GONE = re.compile(r"device (?:'[^']*'|\S+) not found|device offline|no devices/emulators found")
# `forward --remove` of a forward that does not exist (any more): nothing to remove.
LISTENER_MISSING = re.compile(r"listener (?:'[^']*'|\S+) not found")
# Secret arguments (the pairing code) never go into an error text or a log.
REDACTED = "******"
NO_SELECTION = (
    "no phone is selected: select the phone in the monitor page (Devices), or set --adb-serial / "
    "DEBUG_DEVICES_ADB_SERIAL. No command went to any device. adb sees: {devices}"
)
# `pm path` is quick; a phone that does not answer in this time shows "unknown".
APP_CHECK_TIMEOUT = timedelta(seconds=3)


class AdbError(Exception):
    """An adb command failed, or the target device is not clear."""


class AppNotInstalledError(AdbError):
    """The camera app activity does not exist on the device."""


class DeviceGoneError(AdbError):
    """adb says that the device or its forward does not exist (any more)."""


class DeviceNotListedError(AdbError):
    """The serial is not in `adb devices`."""


class DeviceStateError(AdbError):
    """The device is listed, but not in state `device` (offline, unauthorized, ...)."""

    def __init__(self, message: str, state: str) -> None:
        super().__init__(message)
        self.state = state


class NoSelectionError(AdbError):
    """No phone is selected: the server never picks a device by itself."""


class ListenerMissingError(AdbError):
    """`forward --remove`: adb has no forward on that local port."""


class ForwardOwner(BaseModel):
    """One line of `adb forward --list`: `<serial> <local> <remote>`."""

    serial: str
    local: str
    remote: str


class ForwardRemoval(StrEnum):
    REMOVED = "removed"
    # adb had no forward on the port (a second stop, or adb dropped it with the device).
    ALREADY_GONE = "already gone"
    # The port now forwards to another device (another server or page connected it): kept.
    OTHER_DEVICE = "other device"


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


def parse_forwards(output: str) -> list[ForwardOwner]:
    owners = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == FORWARD_COLUMNS:
            owners.append(ForwardOwner(serial=parts[0], local=parts[1], remote=parts[2]))
    return owners


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


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
        """Return the device with `serial` (the user's selection). No selection: an error, also with one device.
        The server never picks a device by itself: it can be a TV on the network (AGENTS.md)."""
        devices = await self.devices()
        if not serial:
            raise NoSelectionError(NO_SELECTION.format(devices=_describe(devices)))
        for device in devices:
            if device.serial == serial:
                if device.state != adb.STATE_DEVICE:
                    message = f"device {serial} is in state {device.state!r}, not {adb.STATE_DEVICE!r}"
                    raise DeviceStateError(message, device.state)
                return device
        raise DeviceNotListedError(f"device {serial} is not connected. Connected: {_describe(devices)}")

    async def forward(self, serial: str, local_port: int, device_port: int = phone.DEVICE_PORT) -> None:
        await self._run(
            adb.SERIAL_FLAG, serial, adb.FORWARD, f"{adb.TCP_PREFIX}{local_port}", f"{adb.TCP_PREFIX}{device_port}"
        )

    async def forwards(self) -> list[ForwardOwner]:
        """`adb forward --list` (a question to the adb server, no device command)."""
        result = await self._run(adb.FORWARD, adb.LIST_FLAG)
        return parse_forwards(result.stdout.decode(errors="replace"))

    async def remove_forward(self, serial: str, local_port: int) -> bool:
        """Remove the forward of `serial` on `local_port`. False: adb had none there (not an error)."""
        try:
            await self._run(adb.SERIAL_FLAG, serial, adb.FORWARD, adb.REMOVE_FLAG, f"{adb.TCP_PREFIX}{local_port}")
        except ListenerMissingError:
            return False
        return True

    async def remove_own_forward(self, serial: str, local_port: int) -> ForwardRemoval:
        """Remove the forward that this server made (`serial`, `local_port`), but only while the port still goes to
        that device: `forward --remove` removes the port whatever device it goes to now."""
        local = f"{adb.TCP_PREFIX}{local_port}"
        owner = next((item.serial for item in await self.forwards() if item.local == local), None)
        if owner is None:
            return ForwardRemoval.ALREADY_GONE
        if owner != serial:
            return ForwardRemoval.OTHER_DEVICE
        removed = await self.remove_forward(serial, local_port)
        return ForwardRemoval.REMOVED if removed else ForwardRemoval.ALREADY_GONE

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

    async def disconnect(self, address: str) -> str:
        """`adb disconnect` of one Wi-Fi serial (the selected phone): adb forgets a stale (offline) connection."""
        result = await self._run(adb.DISCONNECT, address)
        return result.stdout.decode(errors="replace").strip()

    async def pair(self, address: str, code: str) -> str:
        """The code never goes into an error text (it is redacted)."""
        result = await self._run(adb.PAIR, address, code, secrets=(code,))
        output = result.stdout.decode(errors="replace").strip()
        if adb.PAIRED_MARKER not in output:
            raise AdbError(f"adb pair {address}: {output or 'no answer'}")
        return output

    # endregion: device list and Wi-Fi

    async def _run(self, *args: str, secrets: tuple[str, ...] = ()) -> CommandResult:
        command = [self._adb_path, *args]
        try:
            result = await self._runner.run(command, self._timeout)
        except CommandError as exc:
            raise AdbError(redact(str(exc), secrets)) from None
        if not result.ok:
            stderr = result.stderr.decode(errors="replace").strip()
            error = AdbError
            if LISTENER_MISSING.search(stderr):
                error = ListenerMissingError
            elif DEVICE_GONE.search(stderr):
                error = DeviceGoneError
            raise error(redact(f"{' '.join(command)} exited with {result.returncode}: {stderr}", secrets))
        return result


def _not_installed(serial: str) -> str:
    return f"the camera app {phone.PACKAGE} is not installed on {serial}. Build and install android/ first."


def _describe(devices: list[AdbDevice]) -> str:
    if not devices:
        return "none"
    return ", ".join(f"{d.serial} ({' '.join(filter(None, [d.state, d.description]))})" for d in devices)
