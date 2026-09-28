"""QA round 14, dd-ui part: the short whoami probe is only for the other ports of the webcam sharing (N55). The own
port (the primary lookup, `_other_monitor_runs`, the forwarder, the remote screen) keeps the client timeout, so a
busy primary is still found. A real local HTTP responder that answers late."""

import asyncio
import json
import os
from datetime import timedelta

import pytest

from debug_devices_mcp.remote_webcam import RemoteMonitor
from debug_devices_mcp.ui.constants import APP_NAME, remote

from .conftest import free_port

PROBE = timedelta(milliseconds=200)
LATE = 0.5
CLIENT_TIMEOUT = timedelta(seconds=3)


async def late_monitor() -> tuple[asyncio.Server, int]:
    """A debug-devices monitor of another process that answers /api/whoami after LATE seconds (a busy event loop)."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(LATE)
        port = writer.get_extra_info("sockname")[1]
        body = json.dumps(
            {
                "app": APP_NAME,
                "pid": os.getpid() + 1,
                "url": f"http://127.0.0.1:{port}/",
                "webcam": None,
                "webcam_running": False,
                "webcam_crop": None,
            }
        ).encode()
        head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        writer.write(head.encode() + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_a_busy_primary_on_the_own_port_is_still_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "PROBE_TIMEOUT", PROBE)
    server, port = await late_monitor()
    # The primary lookup: only the own port, with the client timeout (3 s), not the 0.2 s probe.
    lookup = RemoteMonitor(port, CLIENT_TIMEOUT)
    try:
        assert await lookup.find_monitor() is not None
    finally:
        await lookup.aclose()
        server.close()


async def test_the_same_late_monitor_on_another_port_is_cut_by_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "PROBE_TIMEOUT", PROBE)
    server, port = await late_monitor()
    # The webcam sharing: the own port is closed, the late monitor is one of the other ports.
    lookup = RemoteMonitor(free_port(), CLIENT_TIMEOUT, other_ports=lambda: [port])
    try:
        assert await lookup.find_monitor() is None
    finally:
        await lookup.aclose()
        server.close()
