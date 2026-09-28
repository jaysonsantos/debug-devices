"""overlay_region (docs/phone-api.md): the part of the still where the app draws boxes. It decides the "visible on the
phone" flag and the warnings (an older app: preview_region), and the server reads it again after a flip or a
rotation change. Fakes only."""

import json

import httpx
import numpy as np
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.highlight import PixelBox
from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.phone_api import CameraStatus, PreviewRegion
from debug_devices_mcp.pointer import ImageFrame, follow_boxes
from debug_devices_mcp.server import Services, build_server

from .conftest import make_jpeg
from .test_highlight import OverlayPhone
from .test_phone_api import STATUS
from .test_preview_sync import PREVIEW
from .test_server import make_services, no_vision

PREVIEW_REGION = {"snapshot_x": 0.2, "snapshot_y": 0.0, "width": 0.6, "height": 1.0}
# The status bar covers the top 8 % of the preview; a vertical flip puts it at the bottom; at rotation 90 the app's
# label is at the side.
TOP_BAR = {"snapshot_x": 0.2, "snapshot_y": 0.08, "width": 0.6, "height": 0.92}
BOTTOM_BAR = {"snapshot_x": 0.2, "snapshot_y": 0.0, "width": 0.6, "height": 0.92}
SIDE_LABEL = {"snapshot_x": 0.28, "snapshot_y": 0.0, "width": 0.52, "height": 1.0}
# A box in the top 2-6 % of the 400 x 300 px snapshot, in the middle: inside preview_region, under the status bar.
UNDER_THE_BAR = {"x": 180, "y": 6, "width": 40, "height": 12, "label": "C1"}


class RegionPhone(OverlayPhone):
    """The overlay fake with POST /v1/preview and an overlay_region that follows the flips and the rotation.
    `without_region`: an app from before overlay_region (only preview_region)."""

    def __init__(self, without_region: bool = False) -> None:
        super().__init__()
        self.without_region = without_region
        self.snapshot = make_jpeg(400, 300)
        self.status.update(preview_flip_horizontal=False, preview_flip_vertical=False, preview_region=PREVIEW_REGION)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == PREVIEW:
            body = json.loads(request.content)
            self.status.update(
                preview_flip_horizontal=body["flip_horizontal"], preview_flip_vertical=body["flip_vertical"]
            )
            response = httpx.Response(200, json=self.status)
        else:
            response = super().handle(request)
        # The region after this request (a flip or a rotation moves it), also in its status answer.
        self._update_region()
        if response.status_code == httpx.codes.OK and response.headers.get("content-type") == "application/json":
            return httpx.Response(200, json=self.status)
        return response

    def _update_region(self) -> None:
        if self.without_region:
            self.status.pop("overlay_region", None)
            return
        if self.status["rotation_degrees"] == 90:
            region = SIDE_LABEL
        else:
            region = BOTTOM_BAR if self.status["preview_flip_vertical"] else TOP_BAR
        self.status["overlay_region"] = region


async def highlight_under_the_bar(services: Services) -> dict:
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {"max_side": 0})
        result = await client.call_tool("phone_highlight", {"boxes": [UNDER_THE_BAR]})
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def test_a_box_under_the_status_bar_is_not_visible(settings: Settings) -> None:
    services = make_services(settings, RegionPhone(), no_vision())
    result = await highlight_under_the_bar(services)
    assert result["visibility"][0]["in_preview"] == "not"
    assert result["warning"] is not None
    assert services.overlay_region == PreviewRegion.model_validate(TOP_BAR)


async def test_an_older_app_uses_the_preview_region(settings: Settings) -> None:
    services = make_services(settings, RegionPhone(without_region=True), no_vision())
    result = await highlight_under_the_bar(services)
    assert result["visibility"][0]["in_preview"] == "fully"
    assert services.overlay_region == PreviewRegion.model_validate(PREVIEW_REGION)


async def test_the_region_is_read_again_after_a_flip_and_a_rotation(settings: Settings) -> None:
    phone = RegionPhone()
    services = make_services(settings, phone, no_vision())
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_status", {})
        assert services.overlay_region == PreviewRegion.model_validate(TOP_BAR)
        await client.call_tool("phone_snapshot_orientation", {"flip_vertical": True})
        assert services.overlay_region == PreviewRegion.model_validate(BOTTOM_BAR)
        await client.call_tool("phone_rotation", {"degrees": 90})
        assert services.overlay_region == PreviewRegion.model_validate(SIDE_LABEL)


# region: a zero-size overlay_region (the safe area is measured but empty)

EMPTY = {"snapshot_x": 0.2, "snapshot_y": 0.5, "width": 0.6, "height": 0.0}
MIDDLE_BOX = {"x": 180, "y": 130, "width": 40, "height": 30, "label": "U1"}


class EmptySafeAreaPhone(RegionPhone):
    def _update_region(self) -> None:
        self.status["overlay_region"] = EMPTY


async def test_a_zero_size_region_shows_nothing_and_does_not_fall_back(settings: Settings) -> None:
    services = make_services(settings, EmptySafeAreaPhone(), no_vision())
    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {"max_side": 0})
        result = await client.call_tool("phone_highlight", {"boxes": [MIDDLE_BOX]})
    assert result.structured_content is not None
    # The box is in the middle of preview_region, but the app draws nothing: not visible, with a warning.
    assert result.structured_content["visibility"][0]["in_preview"] == "not"
    assert result.structured_content["warning"] is not None
    assert services.overlay_region == PreviewRegion.model_validate(EMPTY)


def test_a_zero_size_view_gives_arrows_not_boxes() -> None:
    boxes = [PixelBox(x=180, y=130, width=40, height=30, label="U1", tag="A")]
    frame = ImageFrame((400, 300), SnapshotOrientation(), view=(80.0, 150.0, 240.0, 0.0))
    shown, arrows = follow_boxes(boxes, np.eye(3), frame)
    assert shown == []
    assert [arrow.label for arrow in arrows] == ["U1"]


def test_only_a_missing_overlay_region_falls_back() -> None:
    base = {**STATUS, "preview_region": PREVIEW_REGION}
    assert CameraStatus.model_validate(base).visible_region() == PreviewRegion.model_validate(PREVIEW_REGION)
    empty = CameraStatus.model_validate({**base, "overlay_region": EMPTY}).visible_region()
    assert empty is not None
    assert empty.empty


# endregion: a zero-size overlay_region
