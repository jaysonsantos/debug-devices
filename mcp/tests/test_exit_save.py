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

from .test_bench_state import meter
from .test_server import FakePhone, make_services, no_vision

READ_ONLY = 0o500
WRITABLE = 0o700


class Exit:
    """A server with one kept (unsaved) unsafe reading, and a record of what the exit did."""

    def __init__(self, settings: Settings, path: Path) -> None:
        self.services: Services = make_services(settings, FakePhone(), no_vision())
        self.services.bench = BenchStateStore(path)
        self.unsafe: MeterResult = meter("dc_voltage", "V", 7.0, "7.00")
        self.services.bench.keep_unsaved(self.unsafe)
        self.events: list[str] = []
        self.patch_close("phone")
        self.patch_close("vision")
        self.server = build_server(self.services, after_stop=lambda: self.events.append("after_stop"))

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
