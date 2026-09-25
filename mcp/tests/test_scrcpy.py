from collections.abc import Mapping, Sequence
from typing import IO

import pytest

from debug_devices_mcp.scrcpy import ScrcpyError, ScrcpyLauncher, ScrcpyOptions, scrcpy_args
from debug_devices_mcp.ui.desktop import child_env


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class FakeSpawner:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.processes: list[FakeProcess] = []
        self.fail = fail

    async def __call__(self, args: Sequence[str], environ: Mapping[str, str], log: IO[bytes] | None) -> FakeProcess:
        if self.fail:
            raise FileNotFoundError(args[0])
        self.calls.append((list(args), dict(environ)))
        process = FakeProcess()
        self.processes.append(process)
        return process


def test_scrcpy_args() -> None:
    assert scrcpy_args("scrcpy", "0a1b2c3d") == [
        "scrcpy",
        "-s",
        "0a1b2c3d",
        "--window-title",
        "debug-devices: phone 0a1b2c3d",
        "--no-audio",
        "--stay-awake",
    ]


async def test_starts_once_per_serial_and_stops() -> None:
    spawner = FakeSpawner()
    launcher = ScrcpyLauncher(ScrcpyOptions(adb_path="/opt/adb"), spawner=spawner, environ={"PATH": "/bin"})
    assert await launcher.ensure_running("0a1b2c3d")
    assert not await launcher.ensure_running("0a1b2c3d")
    assert len(spawner.calls) == 1
    assert spawner.calls[0][1] == {"PATH": "/bin", "ADB": "/opt/adb"}
    assert launcher.serial == "0a1b2c3d"
    await launcher.stop()
    assert spawner.processes[0].terminated
    assert not launcher.running


async def test_restarts_after_the_window_closes() -> None:
    spawner = FakeSpawner()
    launcher = ScrcpyLauncher(spawner=spawner, environ={})
    await launcher.ensure_running("0a1b2c3d")
    spawner.processes[0].returncode = 0
    assert await launcher.ensure_running("0a1b2c3d")
    assert len(spawner.calls) == 2


async def test_new_serial_replaces_the_old_process() -> None:
    spawner = FakeSpawner()
    launcher = ScrcpyLauncher(spawner=spawner, environ={})
    await launcher.ensure_running("a")
    await launcher.ensure_running("b")
    assert spawner.processes[0].terminated
    assert launcher.serial == "b"


async def test_missing_scrcpy_and_empty_serial_raise() -> None:
    launcher = ScrcpyLauncher(spawner=FakeSpawner(fail=True), environ={})
    with pytest.raises(ScrcpyError, match="cannot start"):
        await launcher.ensure_running("a")
    with pytest.raises(ScrcpyError, match="serial"):
        await launcher.ensure_running("")


def test_child_env_sets_wayland_when_no_display(tmp_path) -> None:
    (tmp_path / "wayland-0").touch()
    assert child_env({"XDG_RUNTIME_DIR": str(tmp_path)})["WAYLAND_DISPLAY"] == "wayland-0"
    assert "WAYLAND_DISPLAY" not in child_env({"XDG_RUNTIME_DIR": str(tmp_path), "DISPLAY": ":0"})
    assert "WAYLAND_DISPLAY" not in child_env({"XDG_RUNTIME_DIR": str(tmp_path / "none")})
    assert child_env({"WAYLAND_DISPLAY": "wayland-1"})["WAYLAND_DISPLAY"] == "wayland-1"
