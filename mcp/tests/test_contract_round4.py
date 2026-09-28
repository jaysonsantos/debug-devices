"""Contract gaps of QA round 4 (dd-ui part): arrow tags (C3), the overlay TTL (C11), and the snapshot response
headers (C12). C7 (the tag rule) is in test_overlay_sync_round4.py; C18 in test_env_example.py. Fakes only."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid7

import httpx
import pytest
from mcp import Client
from pydantic import ValidationError

from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import phone as phone_names
from debug_devices_mcp.orientation import OrientationState
from debug_devices_mcp.phone_api import OverlayArrow, OverlayBox
from debug_devices_mcp.server import OVERLAY_EXPIRED_NOTE, Services, build_server
from debug_devices_mcp.ui.settings import ScreenRotation, SettingsStore

from .conftest import make_jpeg
from .test_highlight import OVERLAY, OverlayPhone
from .test_server import FakePhone, make_services, no_vision

BOX = {"x": 10, "y": 10, "width": 20, "height": 20, "label": "R1"}


class HeaderPhone(FakePhone):
    """`/v1/snapshot` with the response headers of the contract (C12)."""

    def __init__(self, rotation: int | None, app_start_id: str | None) -> None:
        super().__init__()
        self.snapshot = make_jpeg(64, 48)
        self.still_rotation = rotation
        self.still_app_start_id = app_start_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != phone_names.PATH_SNAPSHOT:
            return super().handle(request)
        headers = {"content-type": "image/jpeg"}
        if self.still_rotation is not None:
            headers[phone_names.ROTATION_HEADER] = str(self.still_rotation)
        if self.still_app_start_id is not None:
            headers[phone_names.APP_START_HEADER] = self.still_app_start_id
        return httpx.Response(200, content=self.snapshot, headers=headers)


# region: C12 the snapshot headers


def fixed_view(settings: Settings, phone: FakePhone, tmp_path: Path) -> Services:
    """A Screen view of 0 degrees: the turn of the still is its rotation (in Auto it is always 0)."""
    store = SettingsStore.in_dir(tmp_path)
    store.update(lambda saved: saved.model_copy(update={"screen_rotation": ScreenRotation.DEG_0}))
    services = make_services(settings, phone, no_vision())
    services.orientation = OrientationState(store)
    services.__post_init__()
    return services


async def test_the_still_rotation_comes_from_its_header(settings: Settings, tmp_path: Path) -> None:
    # The status says 0; the phone turned before the still: the still says 90.
    phone = HeaderPhone(rotation=90, app_start_id=None)
    services = fixed_view(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        info = json.loads((await client.call_tool("phone_snapshot", {"max_side": 0})).content[0].text)
    assert info["turn_degrees"] == 90
    assert (info["width"], info["height"]) == (48, 64)  # the 64 x 48 still, turned by 90 degrees


async def test_an_old_app_without_headers_uses_the_status(settings: Settings, tmp_path: Path) -> None:
    phone = HeaderPhone(rotation=None, app_start_id=None)
    phone.status["rotation_degrees"] = 90
    services = fixed_view(settings, phone, tmp_path)
    async with Client(build_server(services)) as client:
        info = json.loads((await client.call_tool("phone_snapshot", {"max_side": 0})).content[0].text)
    assert info["turn_degrees"] == 90


async def test_an_app_restart_between_status_and_still_is_seen(settings: Settings) -> None:
    phone = HeaderPhone(rotation=0, app_start_id=None)
    services = make_services(settings, phone, no_vision())
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_status", {})
        phone.still_app_start_id = str(uuid7())  # the app restarted after the status of this snapshot
        snapshot = await client.call_tool("phone_snapshot", {"max_side": 0})
    # The restart is seen: this phone_snapshot carries the notice (one last time, round 34).
    assert "the phone app restarted" in snapshot.content[-1].text


# endregion: C12 the snapshot headers

# region: C3 arrow tags


def test_arrow_tags_follow_the_box_tag_rule() -> None:
    assert OverlayArrow(angle_deg=90, label="J4", tag="A").tag == "A"
    with pytest.raises(ValidationError):
        OverlayArrow(angle_deg=90, tag="😀")


class NoArrowTagPhone(OverlayPhone):
    """An app from before arrow tags: an arrow with a tag gives 400."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        arrows = json.loads(request.content).get("arrows", []) if request.url.path == OVERLAY else []
        if any("tag" in arrow for arrow in arrows):
            return httpx.Response(400, json={"error": "bad_request", "message": "unknown field tag"})
        return super().handle(request)


async def test_an_app_without_arrow_tags_gets_them_without(settings: Settings) -> None:
    phone = NoArrowTagPhone()
    services = make_services(settings, phone, no_vision())
    box = OverlayBox(snapshot_x=0.1, snapshot_y=0.1, width=0.1, height=0.1, label="R1", tag="A")
    await services.send_overlay([box], [OverlayArrow(angle_deg=0, label="C9", tag="B")])
    assert phone.arrows_sent[-1] == [{"angle_deg": 0.0, "label": "C9"}]
    # Our record and the page keep the tags.
    assert services.arrows[0].tag == "B"


# endregion: C3 arrow tags

# region: C11 the overlay TTL


async def test_the_boxes_are_forgotten_after_the_ttl(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_names, "OVERLAY_TTL", timedelta(milliseconds=50))
    phone = OverlayPhone()
    phone.snapshot = make_jpeg(400, 300)
    services = make_services(settings, phone, no_vision())
    heard: list[int] = []

    async def page(boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> None:
        heard.append(len(boxes))

    services.add_overlay_listener(page)
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {})
        await client.call_tool("phone_highlight", {"boxes": [BOX]})
        assert len(services.highlights) == 1
        await asyncio.sleep(0.2)
        # The app removed them: the server and the page forget them, and the next phone tool result says so.
        assert services.highlights == []
        assert heard[-1] == 0
        status = await client.call_tool("phone_status", {})
    assert status.content[-1].text == OVERLAY_EXPIRED_NOTE
    # No request went to the phone for this: the app forgot them by itself.
    assert len(phone.sent) == 1


async def test_a_new_overlay_call_restarts_the_ttl(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone_names, "OVERLAY_TTL", timedelta(milliseconds=150))
    services = make_services(settings, OverlayPhone(), no_vision())
    box = OverlayBox(snapshot_x=0.1, snapshot_y=0.1, width=0.1, height=0.1, label="R1")
    await services.send_overlay([box], [])
    await asyncio.sleep(0.1)
    await services.send_overlay([box], [])
    await asyncio.sleep(0.1)
    assert len(services.highlights) == 1  # 200 ms after the first call, 100 ms after the second
    await asyncio.sleep(0.15)
    assert services.highlights == []


# endregion: C11 the overlay TTL
