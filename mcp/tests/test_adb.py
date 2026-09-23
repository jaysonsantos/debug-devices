from datetime import timedelta

import pytest

from debug_devices_mcp.adb import Adb, AdbError, AppNotInstalledError, parse_devices

from .conftest import FakeRunner, failed, ok

TWO_DEVICES = (
    b"List of devices attached\n"
    b"192.168.0.43:5555      device product:raven model:AFTR device:raven transport_id:1\n"
    b"R5CT1234567            device usb:1-2 product:a55 model:SM_A556B device:a55 transport_id:3\n"
    b"\n"
)
ONE_DEVICE = b"List of devices attached\nR5CT1234567            device usb:1-2 model:SM_A556B transport_id:3\n\n"


def adb_with(stdout: bytes) -> tuple[Adb, FakeRunner]:
    runner = FakeRunner(lambda _: ok(stdout))
    return Adb(runner, "adb", timedelta(seconds=1)), runner


def test_parse_devices_skips_header_and_daemon_lines() -> None:
    output = "* daemon started successfully\nList of devices attached\nemulator-5554\tdevice\nABC\tunauthorized\n"
    devices = parse_devices(output)
    assert [(d.serial, d.state) for d in devices] == [("emulator-5554", "device"), ("ABC", "unauthorized")]


async def test_select_only_device() -> None:
    adb, runner = adb_with(ONE_DEVICE)
    device = await adb.select_device("")
    assert device.serial == "R5CT1234567"
    assert runner.calls == [["adb", "devices", "-l"]]


async def test_several_devices_need_a_serial() -> None:
    adb, _ = adb_with(TWO_DEVICES)
    with pytest.raises(AdbError, match=r"several adb devices.*192\.168\.0\.43:5555.*R5CT1234567"):
        await adb.select_device("")


async def test_select_by_serial() -> None:
    adb, _ = adb_with(TWO_DEVICES)
    assert (await adb.select_device("R5CT1234567")).serial == "R5CT1234567"
    with pytest.raises(AdbError, match="not connected"):
        await adb.select_device("missing")


async def test_no_device() -> None:
    adb, _ = adb_with(b"List of devices attached\n\n")
    with pytest.raises(AdbError, match="no ready adb device"):
        await adb.select_device("")


async def test_forward_and_start_commands() -> None:
    adb, runner = adb_with(b"Starting: Intent { cmp=dev.jayson.debugdevices.camera/.MainActivity }\n")
    await adb.forward("R5CT", 18765)
    await adb.start_app("R5CT")
    assert runner.calls == [
        ["adb", "-s", "R5CT", "forward", "tcp:18765", "tcp:8765"],
        ["adb", "-s", "R5CT", "shell", "am", "start", "-n", "dev.jayson.debugdevices.camera/.MainActivity"],
    ]


MISSING_ACTIVITY = b"Error type 3\nError: Activity class {dev.jayson.debugdevices.camera/...} does not exist.\n"


async def test_start_app_reports_missing_app_with_exit_0() -> None:
    adb, _ = adb_with(MISSING_ACTIVITY)
    with pytest.raises(AppNotInstalledError, match="not installed on R5CT"):
        await adb.start_app("R5CT")


async def test_start_app_reports_missing_app_with_exit_1() -> None:
    adb = Adb(FakeRunner(lambda _: failed(MISSING_ACTIVITY)), "adb", timedelta(seconds=1))
    with pytest.raises(AppNotInstalledError, match="Build and install android/"):
        await adb.start_app("R5CT")


async def test_start_app_other_error() -> None:
    adb, _ = adb_with(b"Error: Activity not started, unable to resolve Intent\n")
    with pytest.raises(AdbError, match="am start failed"):
        await adb.start_app("R5CT")


async def test_failed_command() -> None:
    adb = Adb(FakeRunner(lambda _: failed(b"error: device offline")), "adb", timedelta(seconds=1))
    with pytest.raises(AdbError, match="device offline"):
        await adb.forward("R5CT", 18765)
