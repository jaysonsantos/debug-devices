"""One page shows the tool calls of every MCP server: secondaries send their calls to the primary monitor."""

import asyncio
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid7

import httpx
import pytest
from mcp import Client
from starlette.testclient import TestClient

from debug_devices_mcp.config import Settings
from debug_devices_mcp.remote_webcam import MonitorIdentity, RemoteMonitor, SharedWebcam
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.constants import UiStart, ingest
from debug_devices_mcp.ui.events import CallSource, CallStatus, ToolCallEvent
from debug_devices_mcp.ui.forward import (
    CallForwarder,
    ForwarderOptions,
    IngestCall,
    read_token,
    token_path,
    write_token,
)
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore
from debug_devices_mcp.webcam_stream import StreamOptions, WebcamStream

from .conftest import JPEG
from .test_server import FakePhone, make_services, no_vision

START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"
OTHER_PID = os.getpid() + 1
FORWARD_WAIT = 3.0
SLOW_PRIMARY_LIMIT = 3.0


def event(status: CallStatus = CallStatus.OK, tool: str = "phone_status") -> ToolCallEvent:
    return ToolCallEvent(
        id=uuid7(),
        tool=tool,
        source=CallSource.MCP,
        arguments={"step": "in"},
        started_at="2026-09-25T10:00:00Z",
        duration_ms=None if status is CallStatus.RUNNING else 12.5,
        status=status,
        summary="zoom 1.5",
        error=None,
        details={},
        images=[],
    )


def stream() -> WebcamStream:
    options = StreamOptions(
        ffmpeg_path="ffmpeg", device=Path("/dev/video0"), warmup_frames=0, timeout=timedelta(seconds=1)
    )
    return WebcamStream(options)


async def start_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int = 0) -> Monitor:
    """A primary monitor on a real port. It reports another pid, so a secondary in this process accepts it."""
    primary = Monitor(START, SettingsStore.in_dir(tmp_path / "primary"), MonitorOptions(port=port))
    primary.token_dir = tmp_path / "tokens"
    real_identity = primary.identity

    def identity() -> MonitorIdentity:
        return real_identity().model_copy(update={"pid": OTHER_PID})

    monkeypatch.setattr(primary, "identity", identity)
    await primary.ensure_page(auto_open=False)
    return primary


def port_of(monitor: Monitor) -> int:
    assert monitor.url is not None
    return httpx.URL(monitor.url).port or 0


def make_secondary(settings: Settings, tmp_path: Path, port: int, transport: httpx.AsyncBaseTransport | None = None):
    services = make_services(settings, FakePhone(), no_vision())
    webcam_stream = stream()
    remote = RemoteMonitor(port, timedelta(seconds=1))
    secondary = Monitor(
        START,
        SettingsStore.in_dir(tmp_path / "secondary"),
        MonitorOptions(port=port, start=UiStart.LAZY),
        MonitorParts(stream=webcam_stream, shared=SharedWebcam(webcam_stream, remote)),
    )
    secondary.token_dir = tmp_path / "tokens"
    secondary.forwarder = CallForwarder(
        secondary.bus,
        RemoteMonitor(port, timedelta(seconds=1)),
        secondary.is_secondary,
        secondary.origin,
        ForwarderOptions(directory=tmp_path / "tokens", transport=transport),
    )
    return services, secondary


async def wait_for(condition, timeout: float = FORWARD_WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


# region: ingest routes


def primary_client(tmp_path: Path) -> tuple[Monitor, TestClient]:
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(port=0))
    monitor.ingest_token = "secret-token"
    return monitor, TestClient(create_app(monitor), base_url=BASE_URL)


def test_ingest_needs_the_token(tmp_path: Path) -> None:
    monitor, client = primary_client(tmp_path)
    body = IngestCall(origin="codex 1824788", event=event()).model_dump_json()
    assert client.post(ingest.CALLS_PATH, content=body).status_code == 403
    assert client.post(ingest.CALLS_PATH, content=body, headers={ingest.TOKEN_HEADER: "wrong"}).status_code == 403
    assert monitor.bus.calls() == []
    ok = client.post(ingest.CALLS_PATH, content=body, headers={ingest.TOKEN_HEADER: "secret-token"})
    assert ok.status_code == 204
    [call] = client.get("/api/state").json()["calls"]
    assert call["origin"] == "codex 1824788"
    assert call["duration_ms"] == 12.5


def test_ingest_images_and_limits(tmp_path: Path) -> None:
    _, client = primary_client(tmp_path)
    headers = {ingest.TOKEN_HEADER: "secret-token"}
    sent = event()
    image_path = ingest.IMAGE_PATH.format(call_id=sent.id)
    params = {"origin": "codex 1", "label": "result image"}
    jpeg_headers = headers | {"content-type": "image/jpeg"}
    assert client.post(image_path, content=JPEG, params=params, headers=jpeg_headers).status_code == 404  # no event yet
    client.post(ingest.CALLS_PATH, content=IngestCall(origin="codex 1", event=sent).model_dump_json(), headers=headers)
    assert client.post(image_path, content=JPEG, params=params, headers=jpeg_headers).status_code == 204
    assert (
        client.post(
            image_path, content=b"x", params=params, headers=headers | {"content-type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        client.post(image_path, content=JPEG, params=params, headers={"content-type": "image/jpeg"}).status_code == 403
    )
    assert client.get(f"/api/calls/{sent.id}/images/0").content == JPEG


def test_a_late_start_event_does_not_undo_the_end(tmp_path: Path) -> None:
    monitor, _ = primary_client(tmp_path)
    done = event(CallStatus.OK)
    monitor.bus.ingest(done, "codex 1")
    monitor.bus.ingest(done.model_copy(update={"status": CallStatus.RUNNING, "duration_ms": None}), "codex 1")
    assert monitor.bus.calls()[0].status is CallStatus.OK


def test_limits_per_server(tmp_path: Path) -> None:
    monitor, _ = primary_client(tmp_path)
    for _ in range(205):
        monitor.bus.ingest(event(), "codex 1")
    monitor.bus.ingest(event(), "claude 2")
    calls = monitor.bus.calls()
    assert sum(call.origin == "codex 1" for call in calls) == 200
    assert sum(call.origin == "claude 2" for call in calls) == 1


def test_token_file_is_private(tmp_path: Path) -> None:
    token = write_token(tmp_path, 18766)
    assert read_token(tmp_path, 18766) == token
    assert token_path(tmp_path, 18766).stat().st_mode & 0o777 == 0o600


# endregion: ingest routes

# region: forwarding


async def test_secondary_sends_its_calls_and_images(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = await start_primary(tmp_path, monkeypatch)
    services, secondary = make_secondary(settings, tmp_path, port_of(primary))
    server = build_server(services, secondary)
    try:
        async with Client(server) as client:
            await client.call_tool("phone_status", {})
            await client.call_tool("phone_snapshot", {})
            opened = await client.call_tool("monitor_open", {"open_browser": False})
        assert secondary.url is None  # no page of its own
        assert opened.structured_content["url"] == primary.url
        await wait_for(lambda: len([c for c in primary.bus.calls() if c.status is not CallStatus.RUNNING]) == 3)
        calls = primary.bus.calls()
        assert [call.tool for call in calls] == ["phone_status", "phone_snapshot", "monitor_open"]
        assert {call.origin for call in calls} == {f"{secondary.client_name or 'mcp'} {os.getpid()}"}
        await wait_for(lambda: len(calls[1].images) == 1)
        assert calls[1].images[0].label == "result image"
        # The secondary keeps its own log too.
        assert [call.tool for call in secondary.bus.calls()] == ["phone_status", "phone_snapshot", "monitor_open"]
    finally:
        await secondary.stop()
        await primary.stop()


async def test_instructions_are_not_forwarded(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = await start_primary(tmp_path, monkeypatch)
    services, secondary = make_secondary(settings, tmp_path, port_of(primary))
    server = build_server(services, secondary)
    try:
        async with Client(server) as client:
            await client.call_tool("bench_instructions", {})
        await wait_for(lambda: any(c.status is not CallStatus.RUNNING for c in primary.bus.calls()))
        [call] = primary.bus.calls()
        assert call.tool == "bench_instructions"
        assert call.summary == ingest.REDACTED_TEXT
        assert call.details == {}
        assert call.images == []
    finally:
        await secondary.stop()
        await primary.stop()


async def test_primary_restart_is_found_again(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = await start_primary(tmp_path, monkeypatch)
    port = port_of(primary)
    services, secondary = make_secondary(settings, tmp_path, port)
    server = build_server(services, secondary)
    try:
        async with Client(server) as client:
            await client.call_tool("phone_status", {})
            await wait_for(lambda: len(primary.bus.calls()) == 1)
            # A dev monitor reload: the page stops, and a new process takes the same port with a new token.
            old_token = primary.ingest_token
            await primary.stop_page()
            restarted = asyncio.create_task(start_primary(tmp_path, monkeypatch, port))
            await client.call_tool("phone_status", {})  # waits for the primary (restart grace)
            new_primary = await restarted
            assert new_primary.ingest_token != old_token
            assert secondary.url is None  # it did not take the port
            await wait_for(lambda: any(c.status is CallStatus.OK for c in new_primary.bus.calls()))
            assert new_primary.bus.calls()[-1].tool == "phone_status"
        await new_primary.stop()
    finally:
        await secondary.stop()
        await primary.stop()


async def test_a_slow_primary_does_not_slow_the_tool(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = await start_primary(tmp_path, monkeypatch)

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(204)

    services, secondary = make_secondary(settings, tmp_path, port_of(primary), transport=httpx.MockTransport(hang))
    server = build_server(services, secondary)
    try:
        async with Client(server) as client:
            started = time.monotonic()
            for _ in range(3):
                assert not (await client.call_tool("phone_status", {})).is_error
            # The fake primary hangs for 10 s; the calls must not wait for it (3 s leaves room for a busy machine).
            assert time.monotonic() - started < SLOW_PRIMARY_LIMIT
    finally:
        await secondary.stop()
        await primary.stop()


async def test_no_primary_means_no_forwarding(settings: Settings, tmp_path: Path) -> None:
    services, secondary = make_secondary(settings, tmp_path, 0)
    server = build_server(services, secondary)
    try:
        async with Client(server) as client:
            await client.call_tool("phone_status", {})
            assert secondary.url is not None  # it serves its own page
            assert secondary.primary_url is None
        assert secondary.forwarder is not None
        assert secondary.forwarder.sent == 0
    finally:
        await secondary.stop()


# endregion: forwarding
