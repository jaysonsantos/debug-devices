import threading
from datetime import timedelta

from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.server import build_server
from debug_devices_mcp.shutdown import arm_exit_watchdog

from .test_server import FakePhone, make_services, no_vision


def test_watchdog_exits_after_the_grace_time() -> None:
    codes: list[int] = []
    fired = threading.Event()

    def fake_exit(code: int) -> None:
        codes.append(code)
        fired.set()

    timer = arm_exit_watchdog(timedelta(milliseconds=10), fake_exit)
    assert timer.daemon
    assert fired.wait(2)
    assert codes == [0]


def test_watchdog_can_be_cancelled() -> None:
    codes: list[int] = []
    timer = arm_exit_watchdog(timedelta(seconds=5), codes.append)
    timer.cancel()
    timer.join(1)
    assert codes == []


async def test_build_server_calls_after_stop_only_when_given(settings: Settings) -> None:
    calls: list[str] = []
    async with Client(
        build_server(make_services(settings, FakePhone(), no_vision()), after_stop=lambda: calls.append("x"))
    ):
        pass
    async with Client(build_server(make_services(settings, FakePhone(), no_vision()))):
        pass
    assert calls == ["x"]
