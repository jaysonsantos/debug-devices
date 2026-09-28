"""board_parts_at_photo (a photo pixel to parts) and the reuse of a registration for a newer phone_snapshot.

Fixture: mcp/tests/fixtures/boardview/identity.json (synthetic; all names are invented).
"""

from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from debug_devices_mcp.config import Settings
from debug_devices_mcp.evidence import CameraView, CaptureKind, CaptureLog

from .test_board_identity import PX_PER_MM, Bench, photo_id, to_photo


@pytest.fixture
def bench(settings: Settings, tmp_path: Path) -> Bench:
    return Bench(settings, tmp_path)


async def parts_at(client: Client, **arguments: object) -> dict[str, Any]:
    result = await client.call_tool("board_parts_at_photo", arguments)
    assert result.structured_content is not None, result.content
    return result.structured_content


async def test_pixel_to_the_nearest_parts(bench: Bench) -> None:
    async with Client(bench.server) as client:
        _, registration = await bench.ready(client)
        x, y = to_photo("L502")
        small = await parts_at(client, registration_id=registration, x_px=x, y_px=y)
        wide = await parts_at(client, registration_id=registration, x_px=x, y_px=y, radius_px=80)
        bottom_x, bottom_y = to_photo("U7303")
        bottom = await parts_at(client, registration_id=registration, x_px=bottom_x, y_px=bottom_y)

    assert [part["refdes"] for part in small["parts"]] == ["L502"]
    first = small["parts"][0]
    assert first["inside_box"] is True
    assert first["center_distance_mm"] == pytest.approx(0.0, abs=0.01)
    assert small["radius_mm"] == pytest.approx(20 / PX_PER_MM, rel=0.01)
    assert small["identity"] == "candidate"
    assert "supporting evidence" in small["evidence"]
    # 8 mm: the neighbours 5.08 mm away come in, sorted by distance.
    assert [part["refdes"] for part in wide["parts"]][:3] == ["L502", "L501", "L503"]
    # The registration is of the top side: the bottom part is not listed.
    assert all(part["side"] != "bottom" for part in bottom["parts"])


async def test_newer_snapshot_with_the_same_view_reuses_the_registration(bench: Bench) -> None:
    async with Client(bench.server) as client:
        _, registration = await bench.ready(client)
        await client.call_tool("phone_snapshot", {})
        x, y = to_photo("U7301")
        same_view = await parts_at(client, registration_id=registration, x_px=x, y_px=y)
        located = await client.call_tool("board_locate_in_photo", {"registration_id": registration, "refdes": ["J4"]})

    assert same_view["parts"][0]["refdes"] == "U7301"
    assert not located.is_error, located.content


async def test_zoom_change_refuses_the_old_registration(bench: Bench) -> None:
    async with Client(bench.server) as client:
        photo, registration = await bench.ready(client)
        # A running scene watcher: the zoom is our own command, not a move (no watcher: test_evidence_round4.py).
        bench.services.scene.watcher_alive()
        await client.call_tool("phone_zoom", {"ratio": 2.0})
        new_photo = photo_id(await client.call_tool("phone_snapshot", {}))
        x, y = to_photo("U7301")
        refused = await client.call_tool(
            "board_parts_at_photo", {"registration_id": registration, "x_px": x, "y_px": y}
        )
        located = await client.call_tool("board_locate_in_photo", {"registration_id": registration, "refdes": ["J4"]})
        identify = await client.call_tool(
            "board_identify",
            {"photo_id": new_photo, "marking": "U7301", "registration_id": registration, "x_px": x, "y_px": y},
        )

    assert photo != new_photo
    for result in (refused, located, identify):
        assert result.is_error
        text = next(block for block in result.content if isinstance(block, TextContent)).text
        assert "camera view changed" in text
        assert "zoom_ratio" in text


def test_reuse_problem_rules() -> None:
    log = CaptureLog()
    view = CameraView(
        zoom_ratio=1.0,
        in_sensor_zoom="off",
        focal_length_mm=5.4,
        turn_degrees=90,
        flip_horizontal=False,
        flip_vertical=False,
    )
    first = log.record(CaptureKind.PHONE_SNAPSHOT, "phone", view)
    assert log.reuse_problem(first.capture_id) is None
    log.record(CaptureKind.PHONE_SNAPSHOT, "phone", view)
    assert log.reuse_problem(first.capture_id) is None
    log.record(CaptureKind.PHONE_SNAPSHOT, "phone", view.model_copy(update={"flip_horizontal": True}))
    assert "flip_horizontal" in (log.reuse_problem(first.capture_id) or "")
    log.scene_changed()
    log.record(CaptureKind.PHONE_SNAPSHOT, "phone", view)
    assert "moved" in (log.reuse_problem(first.capture_id) or "")
    # A registration without a known photo (not a phone_snapshot of this session) is not checked here.
    assert log.reuse_problem(None) is None
