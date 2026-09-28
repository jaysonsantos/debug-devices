"""Capture ids and UTC capture times (evidence.py). Report: "Tie statements to the exact frame"."""

import json
import uuid

import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent

from debug_devices_mcp.config import Settings
from debug_devices_mcp.evidence import MAX_CAPTURES, CaptureKind, CaptureLog, StaleCaptureError
from debug_devices_mcp.server import build_server

from .test_server import FakePhone, make_services, no_vision

UUID_VERSION_7 = 7


def test_ids_are_uuid7_with_utc_time() -> None:
    log = CaptureLog()
    first = log.record(CaptureKind.PHONE_SNAPSHOT, "phone")
    second = log.record(CaptureKind.PHONE_SNAPSHOT, "phone")
    assert first.capture_id != second.capture_id
    assert uuid.UUID(first.capture_id).version == UUID_VERSION_7
    assert first.captured_at.utcoffset() is not None
    assert first.captured_at.utcoffset().total_seconds() == 0
    assert first.captured_at <= second.captured_at


def test_scene_change_invalidates_old_photos() -> None:
    log = CaptureLog()
    old = log.record(CaptureKind.PHONE_SNAPSHOT, "phone")
    assert log.status(old.capture_id).valid_for_position_claims
    log.scene_changed()
    status = log.status(old.capture_id)
    assert not status.valid_for_position_claims
    assert "moved" in status.reason
    with pytest.raises(StaleCaptureError, match="moved"):
        log.require_current_photo(old.capture_id)
    new = log.record(CaptureKind.PHONE_SNAPSHOT, "phone")
    assert log.require_current_photo(new.capture_id) == new


def test_meter_images_and_unknown_ids_cannot_support_position_claims() -> None:
    log = CaptureLog()
    meter = log.record(CaptureKind.METER_IMAGE, "webcam")
    assert not log.status(meter.capture_id).valid_for_position_claims
    unknown = log.status(str(uuid.uuid7()))
    assert unknown.capture is None
    assert not unknown.valid_for_position_claims


def test_log_is_bounded() -> None:
    log = CaptureLog()
    first = log.record(CaptureKind.PHONE_SNAPSHOT, "phone")
    for _ in range(MAX_CAPTURES):
        log.record(CaptureKind.METER_IMAGE, "webcam")
    assert log.get(first.capture_id) is None


def text_json(blocks: list[object]) -> dict[str, object]:
    return json.loads(next(block for block in blocks if isinstance(block, TextContent)).text)


def image_meta(blocks: list[object]) -> dict[str, object]:
    image = next(block for block in blocks if isinstance(block, ImageContent))
    return image.meta or {}


async def test_tools_return_capture_ids(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())

    async with Client(build_server(services)) as client:
        photo_one = await client.call_tool("phone_snapshot", {})
        photo_two = await client.call_tool("phone_snapshot", {})
        meter = await client.call_tool("multimeter_read", {"include_image": True})
        current = await client.call_tool("capture_status", {"capture_id": text_json(photo_two.content)["capture_id"]})
        meter_status = await client.call_tool("capture_status", {"capture_id": meter.structured_content["capture_id"]})
        await services.scene.mark_changed()
        stale = await client.call_tool("capture_status", {"capture_id": text_json(photo_two.content)["capture_id"]})

    one, two = text_json(photo_one.content), text_json(photo_two.content)
    assert one["capture_id"] != two["capture_id"]
    assert one["captured_at"].endswith("Z") or one["captured_at"].endswith("+00:00")
    # The photo and its image block name the same capture.
    assert image_meta(photo_two.content)["capture_id"] == two["capture_id"]
    # A meter result and its returned image have the same capture id.
    assert meter.structured_content is not None
    assert image_meta(meter.content)["capture_id"] == meter.structured_content["capture_id"]
    assert meter.structured_content["capture_id"] not in {one["capture_id"], two["capture_id"]}
    assert services.captures.meter_results[meter.structured_content["capture_id"]].status == "confirmed"
    assert current.structured_content is not None
    assert current.structured_content["valid_for_position_claims"] is True
    assert meter_status.structured_content is not None
    assert meter_status.structured_content["valid_for_position_claims"] is False
    assert stale.structured_content is not None
    assert stale.structured_content["valid_for_position_claims"] is False
