"""Bench feedback round 1: boxes outside the phone screen, and secondary servers (overlay, frames, tracking).

(a) The S22 preview shows only the middle ~60 % of the still (preview_region): boxes at the edge were invisible.
(b) A secondary MCP server's boxes never reached the page, and it had no phone screen frames for tracking.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.highlight import PREVIEW_WARNING, InPreview, in_preview, visibility
from debug_devices_mcp.phone_api import OverlayArrow, OverlayBox, PreviewRegion
from debug_devices_mcp.remote_webcam import RemoteMonitor
from debug_devices_mcp.scene import SceneState, SceneWatcher
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.constants import ingest
from debug_devices_mcp.ui.remote_screen import RemoteScreen

from .conftest import make_jpeg
from .test_highlight import OverlayPhone
from .test_pointing import blank_photo, board_file, register, registered_services  # noqa: F401
from .test_server import make_services, no_vision
from .test_shared_log import make_secondary, port_of, primary_client, start_primary, wait_for

# The S22: a 9:20 portrait screen filled from a 3:4 still shows the middle 60 % of its width.
REMOTE_TIMEOUT = timedelta(seconds=1)
S22_REGION = PreviewRegion(snapshot_x=0.2, snapshot_y=0.0, width=0.6, height=1.0)


def box(x: float, width: float, label: str = "C1") -> OverlayBox:
    return OverlayBox(snapshot_x=x, snapshot_y=0.4, width=width, height=0.1, label=label)


# region: visibility


def test_in_preview() -> None:
    assert in_preview(box(0.4, 0.1), S22_REGION) is InPreview.FULLY
    assert in_preview(box(0.15, 0.1), S22_REGION) is InPreview.PARTLY
    # The boxes of the bench session: snapshot_x 0.08-0.17.
    assert in_preview(box(0.08, 0.09), S22_REGION) is InPreview.NOT
    assert in_preview(box(0.08, 0.09), None) is InPreview.UNKNOWN


def test_visibility_warns_about_hidden_boxes() -> None:
    items, warning = visibility([box(0.4, 0.1, "U1"), box(0.08, 0.09, "C12")], S22_REGION)
    assert [item.in_preview for item in items] == [InPreview.FULLY, InPreview.NOT]
    assert warning == f"C12: {PREVIEW_WARNING}"
    assert visibility([box(0.4, 0.1)], S22_REGION)[1] is None


class RegionPhone(OverlayPhone):
    """The overlay fake with a preview region in its status (the part of the still on the phone screen)."""

    def __init__(self, region: PreviewRegion) -> None:
        super().__init__()
        self.status["preview_region"] = region.model_dump()


async def test_phone_highlight_reports_boxes_outside_the_phone_screen(settings: Settings) -> None:
    phone = RegionPhone(S22_REGION)
    phone.snapshot = make_jpeg(1000, 750)
    services = make_services(settings, phone, no_vision())
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {"max_side": 0})
        result = await client.call_tool(
            "phone_highlight",
            {
                "boxes": [
                    {"x": 80, "y": 300, "width": 90, "height": 60, "label": "C12"},  # x 0.08-0.17: outside
                    {"x": 450, "y": 300, "width": 90, "height": 60, "label": "U1"},
                ]
            },
        )
    data = result.structured_content
    assert data is not None
    assert [item["in_preview"] for item in data["visibility"]] == ["not", "fully"]
    assert data["warning"] == f"C12: {PREVIEW_WARNING}"


async def test_pointing_uses_the_phone_screen_as_the_view(settings: Settings, board_file: Path) -> None:  # noqa: F811
    # The phone screen shows only the right half of the still: U7301 (at 30 % of the photo width) is off screen.
    services, phone = registered_services(settings, blank_photo())
    phone.status["preview_region"] = {"snapshot_x": 0.5, "snapshot_y": 0.0, "width": 0.5, "height": 1.0}
    async with Client(build_server(services)) as client:
        await register(client, board_file)
        off_screen = await client.call_tool("phone_point_to", {"refdes": "U7301"})
        on_screen = await client.call_tool("phone_point_to", {"refdes": "TP9"})
    result = off_screen.structured_content
    assert result is not None
    assert result["targets"][0]["in_view"] is False
    [arrow] = result["arrows"]
    assert 90 < arrow["angle_deg"] < 270  # toward the left: where the part is
    assert on_screen.structured_content["targets"][0]["in_view"] is True
    assert on_screen.structured_content["visibility"][0]["in_preview"] == "fully"


# endregion: visibility

# region: ingest routes of the primary


def test_overlay_of_a_secondary_reaches_the_page(tmp_path: Path) -> None:
    _, client = primary_client(tmp_path)
    body = {"origin": "codex 4242", "boxes": [box(0.4, 0.1).model_dump()], "arrows": [{"angle_deg": 90, "label": "J4"}]}
    assert client.post(ingest.OVERLAY_PATH, json=body).status_code == 403
    sent = client.post(ingest.OVERLAY_PATH, json=body, headers={ingest.TOKEN_HEADER: "secret-token"})
    assert sent.status_code == 204
    phone = client.get("/api/state").json()["phone"]
    assert phone["overlay_origin"] == "codex 4242"
    assert phone["highlights"][0]["label"] == "C1"
    assert phone["arrows"][0]["label"] == "J4"


class FakeScreen:
    """A phone screen stream that only records its start."""

    def __init__(self) -> None:
        self.serials: list[str] = []

    def ensure_running(self, serial: str) -> bool:
        self.serials.append(serial)
        return len(self.serials) == 1

    async def stop(self) -> None:
        """The monitor stops the stream at its end."""


async def no_frames() -> AsyncIterator[bytes]:
    """A frame feed that never has a frame: the tests push frames into the scene state themselves."""
    await asyncio.Event().wait()
    yield b""


def test_screen_start_and_frames(tmp_path: Path) -> None:
    monitor, client = primary_client(tmp_path)
    # The user selected this phone in the primary's page (N2 of QA round 6: only it is streamed for a secondary).
    monitor.selected_serial = lambda: "R5CT1234567"
    headers = {ingest.TOKEN_HEADER: "secret-token"}
    no_screen = client.post(ingest.SCREEN_START_PATH, json={"serial": "R5CT1234567"}, headers=headers).json()
    assert no_screen == {"running": False, "detail": "this monitor has no phone screen (--no-phone-screen)"}
    screen = FakeScreen()
    monitor.screen = screen  # type: ignore[assignment]
    state = SceneState()
    monitor.scene_watcher = SceneWatcher(state, no_frames)
    started = client.post(ingest.SCREEN_START_PATH, json={"serial": "R5CT1234567"}, headers=headers).json()
    assert started == {"running": True, "detail": "started"}
    assert screen.serials == ["R5CT1234567"]
    assert client.get(ingest.FRAME_PATH, headers=headers).status_code == 204  # no frame yet
    frame = make_jpeg(48, 64)
    asyncio.run(state.frame(frame))
    got = client.get(ingest.FRAME_PATH, headers=headers)
    assert (got.status_code, got.content, got.headers[ingest.FRAME_SEQ_HEADER]) == (200, frame, "1")
    assert client.get(ingest.FRAME_PATH, params={"after": "1"}, headers=headers).status_code == 204
    assert client.get(ingest.FRAME_PATH).status_code == 403


# endregion: ingest routes of the primary

# region: a secondary server with a real primary


async def test_a_secondary_gets_frames_and_sends_its_boxes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = await start_primary(tmp_path, monkeypatch)
    primary.selected_serial = lambda: "R5CT1234567"
    primary.screen = FakeScreen()  # type: ignore[assignment]
    primary.scene_watcher = SceneWatcher(SceneState(), no_frames)
    port = port_of(primary)
    services, secondary = make_secondary(settings, tmp_path, port)
    secondary.remote_screen = RemoteScreen(RemoteMonitor(port, REMOTE_TIMEOUT), tmp_path / "tokens")
    secondary.remote_screen.watcher = SceneWatcher(services.scene, secondary.remote_screen.feed)
    server = build_server(services, secondary)
    try:
        async with Client(server) as client:
            await client.call_tool("phone_status", {})  # finds the primary
            await wait_for(secondary.is_secondary)
            result = await secondary.remote_screen.start("R5CT1234567")
            assert result.running
            secondary.remote_screen.watcher.start()
            frame = make_jpeg(48, 64)
            await primary.scene_watcher.state.frame(frame)
            await wait_for(lambda: services.scene.latest_frame == frame)
            assert secondary.screen_source() == (True, "the phone screen stream of the primary monitor")
            # The boxes of the secondary go to the primary's page, with the secondary's origin.
            boxes = [box(0.4, 0.1, "U1")]
            await secondary.overlay_changed(boxes, [OverlayArrow(angle_deg=0, label="J4 ~2 cm")])
            await wait_for(lambda: primary.bus.phone.overlay_origin is not None)
            assert primary.bus.phone.highlights == boxes
            assert primary.bus.phone.overlay_origin == secondary.origin()
    finally:
        if secondary.remote_screen.watcher is not None:
            await secondary.remote_screen.watcher.stop()
        await secondary.stop()
        await primary.stop()


def test_no_stream_anywhere_is_an_error_step(tmp_path: Path) -> None:
    monitor, _ = primary_client(tmp_path)
    running, detail = monitor.screen_source()
    assert running is False
    assert "no phone screen stream" in detail


# endregion: a secondary server with a real primary
