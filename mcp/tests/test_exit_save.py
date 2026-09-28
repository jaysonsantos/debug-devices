"""The exit path of the server (N33, N57, N58): the last save of kept unsafe readings, the client close, and the exit
watchdog, through the lifespan, after Ctrl-C (a cancelled scope) and after SIGTERM."""

import asyncio
import os
import signal
from pathlib import Path

import anyio
import pytest

from debug_devices_mcp.bench_state import BenchStateStore
from debug_devices_mcp.config import Settings
from debug_devices_mcp.multimeter import MeterResult
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.shutdown import route_sigterm_to_ctrl_c

from .test_bench_state import meter, other_writer
from .test_server import FakePhone, make_services, no_vision

READ_ONLY = 0o500
WRITABLE = 0o700


class Exit:
    """A server with one kept (unsaved) unsafe reading, and a record of what the exit did."""

    def __init__(self, settings: Settings, path: Path, monitor: object = None) -> None:
        self.services: Services = make_services(settings, FakePhone(), no_vision())
        self.services.bench = BenchStateStore(path)
        self.unsafe: MeterResult = meter("dc_voltage", "V", 7.0, "7.00")
        self.services.bench.keep_unsaved(self.unsafe)
        self.events: list[str] = []
        self.patch_close("phone")
        self.patch_close("vision")
        self.server = build_server(
            self.services,
            monitor,  # type: ignore[arg-type]
            after_stop=lambda: self.events.append("after_stop"),
        )

    def patch_close(self, name: str) -> None:
        client = getattr(self.services, name)
        close = client.aclose

        async def recorded() -> None:
            await close()
            self.events.append(f"{name} closed")

        client.aclose = recorded

    def lifespan(self) -> object:
        return self.server._lowlevel_server.lifespan(self.server._lowlevel_server)  # type: ignore[attr-defined]

    def saved_points(self, path: Path) -> list[str]:
        return [point.point for point in BenchStateStore(path).load().residual_points]


async def test_ctrl_c_saves_the_kept_reading_and_stops(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "bench-state.json"
    run = Exit(settings, path)
    with anyio.CancelScope() as scope:
        async with run.lifespan():  # type: ignore[attr-defined]
            scope.cancel()
            await anyio.sleep(5)

    assert run.saved_points(path) == [f"unknown point {run.unsafe.capture_id}"]
    assert run.events == ["phone closed", "vision closed", "after_stop"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only folder")
async def test_a_read_only_state_folder_still_stops(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "state"
    folder.mkdir()
    folder.chmod(READ_ONLY)
    try:
        run = Exit(settings, folder / "bench-state.json")
        with anyio.CancelScope() as scope:
            async with run.lifespan():  # type: ignore[attr-defined]
                scope.cancel()
                await anyio.sleep(5)
    finally:
        folder.chmod(WRITABLE)

    assert "1 unsafe readings are not in the bench state file at exit" in caplog.text
    assert "Permission denied" in caplog.text
    assert run.events == ["phone closed", "vision closed", "after_stop"]


def test_sigterm_takes_the_ctrl_c_path_and_saves(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "bench-state.json"
    run = Exit(settings, path)

    async def serve() -> None:
        async with run.lifespan():  # type: ignore[attr-defined]
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)

    previous = signal.getsignal(signal.SIGTERM)
    route_sigterm_to_ctrl_c()
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(serve())
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert run.saved_points(path) == [f"unknown point {run.unsafe.capture_id}"]
    assert run.events == ["phone closed", "vision closed", "after_stop"]


class StubMonitor:
    """The parts of the monitor that the lifespan uses. `stop` is slow, or raises."""

    def __init__(self, stop_seconds: float = 0.0, error: Exception | None = None) -> None:
        self.stop_seconds, self.error = stop_seconds, error

    def instrument(self, server: object) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        await asyncio.sleep(self.stop_seconds)
        if self.error is not None:
            raise self.error


def test_sigterm_then_sigint_during_a_slow_monitor_stop_keeps_the_save(settings: Settings, tmp_path: Path) -> None:
    # N65: `uv run` passes a group SIGTERM, and watchexec a second interrupt 0.2 s later.
    path = tmp_path / "bench-state.json"
    run = Exit(settings, path, StubMonitor(stop_seconds=3))

    async def serve() -> None:
        async with run.lifespan():  # type: ignore[attr-defined]
            asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)

    previous = signal.getsignal(signal.SIGTERM)
    assert route_sigterm_to_ctrl_c()
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(serve())
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert run.saved_points(path) == [f"unknown point {run.unsafe.capture_id}"]
    assert "after_stop" in run.events


async def test_a_monitor_stop_that_raises_skips_nothing(settings: Settings, tmp_path: Path) -> None:
    # N66
    path = tmp_path / "bench-state.json"
    run = Exit(settings, path, StubMonitor(error=RuntimeError("the page did not stop")))
    with pytest.raises(RuntimeError, match="the page did not stop"):
        async with run.lifespan():  # type: ignore[attr-defined]
            pass

    assert run.saved_points(path) == [f"unknown point {run.unsafe.capture_id}"]
    assert run.events == ["phone closed", "vision closed", "after_stop"]


async def test_a_cut_exit_save_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # N65: a native cancel (a second interrupt) reaches the save through the shield: the log says so.
    path = tmp_path / "bench-state.json"
    store = BenchStateStore(path)
    store.keep_unsaved(meter("dc_voltage", "V", 7.0, "7.00"))
    with other_writer(path):
        save = asyncio.create_task(store.save_unsaved_at_exit())
        await asyncio.sleep(0.05)
        save.cancel()
        with pytest.raises(asyncio.CancelledError):
            await save
    assert "may not be in the bench state file: a second interrupt cut the exit save (CancelledError)" in caplog.text


def test_sigterm_goes_through_sigint_only_with_the_default_handler() -> None:
    # N67: a background job of a script starts with SIGINT ignored; then SIGTERM keeps its default.
    previous_int, previous_term = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        assert route_sigterm_to_ctrl_c() is False
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        signal.signal(signal.SIGINT, signal.default_int_handler)
        assert route_sigterm_to_ctrl_c() is True
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
