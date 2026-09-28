"""bench_measure: a meter reading and a fresh phone_snapshot at the same moment. Bench feedback 2, item 3."""

from mcp import Client
from mcp.types import ImageContent

from debug_devices_mcp.config import Settings
from debug_devices_mcp.server import build_server

from .test_server import FakePhone, make_services, no_vision


async def test_value_and_photo_belong_together(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())
    async with Client(build_server(services)) as client:
        result = await client.call_tool("bench_measure", {"include_meter_images": True})
        status = await client.call_tool(
            "capture_status", {"capture_id": result.structured_content["photo"]["capture_id"]}
        )

    assert not result.is_error, result.content
    measured = result.structured_content
    assert measured is not None
    meter_id, photo_id = measured["meter"]["capture_id"], measured["photo"]["capture_id"]
    assert meter_id != photo_id
    assert measured["meter"]["status"] == "confirmed"
    assert len(measured["meter"]["frames"]) == 2
    assert measured["gap_seconds"] < 5
    assert "belong together" in measured["note"]
    # The first image is the photo, then the meter frames; every image names its capture.
    image_ids = [block.meta["capture_id"] for block in result.content if isinstance(block, ImageContent)]
    assert image_ids[0] == photo_id
    assert image_ids[1:] == [frame["capture_id"] for frame in measured["meter"]["frames"]]
    # The photo is a phone_snapshot of the current scene: a position claim can name it.
    assert status.structured_content is not None
    assert status.structured_content["valid_for_position_claims"] is True
    # The meter result can go into the bench record by its id.
    assert services.captures.meter_results[meter_id].status == "confirmed"


async def test_without_meter_images_only_the_photo(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())
    async with Client(build_server(services)) as client:
        result = await client.call_tool("bench_measure", {"frames": 1})

    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert result.structured_content is not None
    assert len(result.structured_content["meter"]["frames"]) == 1


async def test_phone_failure_stops_the_meter_read(settings: Settings) -> None:
    services = make_services(settings, FakePhone(up=False), no_vision())
    async with Client(build_server(services)) as client:
        result = await client.call_tool("bench_measure", {})

    assert result.is_error
