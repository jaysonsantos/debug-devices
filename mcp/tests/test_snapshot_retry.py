"""A snapshot during a camera rebind: the app answers 503 camera_not_ready, and the server tries again (N44)."""

from datetime import timedelta

import httpx
from mcp import Client
from mcp.types import ImageContent, TextContent

from debug_devices_mcp.config import Settings
from debug_devices_mcp.server import build_server

from .test_server import FakePhone, make_services, no_vision

SNAPSHOT_PATH = "/v1/snapshot"
REBIND_MESSAGE = "camera change still running"


class BusyPhone(FakePhone):
    """The first `busy` snapshots get `status` with the error `code` (the default: a camera rebind)."""

    def __init__(self, busy: int, code: str = "camera_not_ready", status: int = 503) -> None:
        super().__init__()
        self.busy, self.code, self.error_status = busy, code, status

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == SNAPSHOT_PATH and self.busy > 0:
            self.busy -= 1
            self.requests.append((request.method, request.url.path))
            return httpx.Response(self.error_status, json={"error": self.code, "message": REBIND_MESSAGE})
        return super().handle(request)

    @property
    def snapshots(self) -> int:
        return sum(path == SNAPSHOT_PATH for _, path in self.requests)


async def snapshot(settings: Settings, phone: BusyPhone) -> tuple[bool, list[object]]:
    async with Client(build_server(make_services(settings, phone, no_vision()))) as client:
        result = await client.call_tool("phone_snapshot", {})
    return result.is_error, list(result.content)


async def test_a_rebind_then_a_still_gives_the_still(settings: Settings) -> None:
    phone = BusyPhone(busy=2)
    is_error, content = await snapshot(settings, phone)

    assert not is_error, content
    assert any(isinstance(block, ImageContent) for block in content)
    assert phone.snapshots == 3


async def test_a_rebind_until_the_start_timeout_gives_the_error(settings: Settings) -> None:
    settings.app_start_timeout = timedelta(milliseconds=50)
    phone = BusyPhone(busy=1_000_000)
    is_error, content = await snapshot(settings, phone)

    assert is_error
    text = " ".join(block.text for block in content if isinstance(block, TextContent))
    assert "503 camera_not_ready" in text
    assert REBIND_MESSAGE in text
    assert phone.snapshots > 1


async def test_other_errors_do_not_retry(settings: Settings) -> None:
    phone = BusyPhone(busy=1, code="capture_failed", status=500)
    is_error, content = await snapshot(settings, phone)

    assert is_error
    text = " ".join(block.text for block in content if isinstance(block, TextContent))
    assert "500 capture_failed" in text
    assert phone.snapshots == 1
