"""The exit path of the server (N57, N66): the client close and the exit watchdog, through the lifespan. Unsafe readings
need no exit save: the bench journal has them (N80, test_bench_journal.py)."""

import asyncio

import anyio
import pytest

from debug_devices_mcp.config import Settings
from debug_devices_mcp.server import Services, build_server

from .test_server import FakePhone, make_services, no_vision


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


class Exit:
    """A server and a record of what the exit did."""

    def __init__(self, settings: Settings, monitor: StubMonitor | None = None) -> None:
        self.services: Services = make_services(settings, FakePhone(), no_vision())
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


async def test_ctrl_c_closes_the_clients_and_stops(settings: Settings) -> None:
    run = Exit(settings, StubMonitor(stop_seconds=0.05))
    with anyio.CancelScope() as scope:
        async with run.lifespan():  # type: ignore[attr-defined]
            scope.cancel()
            await anyio.sleep(5)

    assert run.events == ["phone closed", "vision closed", "after_stop"]


async def test_a_monitor_stop_that_raises_skips_nothing(settings: Settings) -> None:
    # N66
    run = Exit(settings, StubMonitor(error=RuntimeError("the page did not stop")))
    with pytest.raises(RuntimeError, match="the page did not stop"):
        async with run.lifespan():  # type: ignore[attr-defined]
            pass

    assert run.events == ["phone closed", "vision closed", "after_stop"]
