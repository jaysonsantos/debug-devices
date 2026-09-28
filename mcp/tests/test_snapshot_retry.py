"""A snapshot during a camera rebind: the app answers 503 camera_not_ready, and the server tries again (N45)."""

import asyncio
import time
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent

from debug_devices_mcp import server
from debug_devices_mcp.config import Settings
from debug_devices_mcp.server import build_server

from .test_meter_frames import answers
from .test_multimeter import READING
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
    assert "the server retried the snapshot for 0.05 s" in text
    assert "bring the app to the front, or call phone_connect" in text
    assert phone.snapshots > 1


async def test_other_errors_do_not_retry(settings: Settings) -> None:
    phone = BusyPhone(busy=1, code="capture_failed", status=500)
    is_error, content = await snapshot(settings, phone)

    assert is_error
    text = " ".join(block.text for block in content if isinstance(block, TextContent))
    assert "500 capture_failed" in text
    assert phone.snapshots == 1


class HangingPhone(BusyPhone):
    """After the busy answers, a snapshot request does not end (the hard deadline must stop it)."""

    def handle(self, request: httpx.Request) -> httpx.Response | Coroutine[None, None, httpx.Response]:
        if request.url.path == SNAPSHOT_PATH and self.busy == 0:
            self.requests.append((request.method, request.url.path))
            return self.hang()
        return super().handle(request)

    async def hang(self) -> httpx.Response:
        await asyncio.sleep(HANG_SECONDS)
        return httpx.Response(500)


HANG_SECONDS = 30


@pytest.mark.parametrize(("busy", "text"), [(1, "the server retried the snapshot for 0.05 s"), (0, "in 0.05 s")])
async def test_the_deadline_also_stops_a_running_request(settings: Settings, busy: int, text: str) -> None:
    settings.app_start_timeout = timedelta(milliseconds=50)
    phone = HangingPhone(busy=busy)
    started = time.monotonic()
    is_error, content = await snapshot(settings, phone)

    assert is_error
    assert time.monotonic() - started < HANG_SECONDS / 10
    message = " ".join(block.text for block in content if isinstance(block, TextContent))
    assert text in message
    assert "bring the app to the front, or call phone_connect" in message


async def measure(settings: Settings, phone: BusyPhone) -> dict[str, Any]:
    services = make_services(settings, phone, answers(READING))
    async with Client(build_server(services)) as client:
        result = await client.call_tool("bench_measure", {"frames": 1})
    assert result.structured_content is not None, result.content
    return result.structured_content


async def test_bench_measure_warns_about_a_late_photo(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    # N52: snapshot retries make the photo come after the meter frame.
    monkeypatch.setattr(server, "PHOTO_LATE_LIMIT", timedelta(milliseconds=100))
    settings.poll_interval = timedelta(milliseconds=100)
    late = await measure(settings, BusyPhone(busy=3))

    assert late["warning"].startswith("the photo came ")
    assert "after the last meter frame (limit 0.1 s)" in late["warning"]
    assert "measure again (bench_measure)" in late["warning"]


async def test_bench_measure_has_no_warning_for_a_photo_in_time(settings: Settings) -> None:
    in_time = await measure(settings, BusyPhone(busy=0))

    assert in_time["warning"] is None
    assert "when `warning` is empty" in in_time["note"]
