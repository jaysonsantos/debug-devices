"""ADB commands: pick the device, forward the port, start the camera app."""

from datetime import timedelta

from pydantic import BaseModel

from debug_devices_mcp.constants import adb, phone
from debug_devices_mcp.process import CommandError, CommandResult, CommandRunner


class AdbDevice(BaseModel):
    serial: str
    state: str
    description: str = ""


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
                f"several adb devices are connected; set --adb-serial or DEBUG_DEVICES_ADB_SERIAL to one of: "
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
