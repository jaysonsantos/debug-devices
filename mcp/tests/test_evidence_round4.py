"""QA round 4 fixes for the evidence and tracking tools (docs/reports/dd-qa.md, round 4; brief qa-round4-fixes.md).

B-F1 point_to view check, B-F2 photo sizes, B-E5 identify photo tie, B-E7 no scene watcher, B-F5/B-F7 stale reasons,
B-F8 crop check. B-E6 (landmark needs a visual input) is in test_board_identity.py.
Run with `uv run pytest mcp/tests/test_evidence_round4.py`. The boards are the synthetic test fixtures.
"""

import json
from datetime import timedelta
from pathlib import Path

from mcp import Client
from mcp.types import CallToolResult, TextContent

from debug_devices_mcp.app_restart import RESTART_STALE_REASON
from debug_devices_mcp.board.tools import STALE_REGISTRATION, scene_stale_reason
from debug_devices_mcp.config import Settings
from debug_devices_mcp.evidence import CameraView, CaptureKind, CaptureLog
from debug_devices_mcp.pointing import TRACKING_LOST_REASON
from debug_devices_mcp.scene import SceneOptions, SceneState
from debug_devices_mcp.server import build_server

from .test_board_identity import SIZE, Bench, identify, pairs, photo_id, to_photo
from .test_board_restart import Process as RestartProcess
from .test_board_restart import before_restart
from .test_carry import first_photo, jpeg
from .test_pointing import blank_photo, board_file, register, registered_services  # noqa: F401
from .test_server import FakePhone, make_services, no_vision

MAX_AGE_S = 60.0
WATCH_TIMEOUT_S = 5.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def text(result: CallToolResult) -> str:
    return next(block for block in result.content if isinstance(block, TextContent)).text


def view(zoom: float = 1.0) -> CameraView:
    return CameraView(
        zoom_ratio=zoom,
        in_sensor_zoom="off",
        focal_length_mm=6.07,
        turn_degrees=0,
        flip_horizontal=False,
        flip_vertical=False,
    )


# region: B-F1 phone_point_to checks the camera view


async def test_point_to_refuses_a_photo_of_another_view(settings: Settings, board_file: Path) -> None:  # noqa: F811
    services, _ = registered_services(settings, blank_photo())
    async with Client(build_server(services)) as client:
        await register(client, board_file)
        services.scene.watcher_alive()
        await client.call_tool("phone_zoom", {"ratio": 2.0})
        await client.call_tool("phone_snapshot", {})
        refused = await client.call_tool("phone_point_to", {"refdes": "U7301"})

    assert refused.is_error
    assert "camera view changed" in text(refused)
    assert "zoom_ratio" in text(refused)


# endregion

# region: B-F2 photo sizes


async def test_parts_at_photo_scales_another_size_and_refuses_another_shape(settings: Settings, tmp_path: Path) -> None:
    bench = Bench(settings, tmp_path)
    x, y = to_photo("U7301")
    async with Client(bench.server) as client:
        first, registration = await bench.ready(client)
        registered = await client.call_tool(
            "board_parts_at_photo", {"registration_id": registration, "x_px": x, "y_px": y, "photo_id": first}
        )
        second = photo_id(await client.call_tool("phone_snapshot", {}))
        captures = bench.services.captures
        assert captures.photo_size(second) == captures.photo_size(first)
        # The same view at half the size (like another max_side).
        capture = captures.get(second)
        assert capture is not None
        assert capture.view is not None
        width, height = captures.photo_size(first) or (0, 0)
        capture.view = capture.view.with_size(width // 2, height // 2)
        half = await client.call_tool(
            "board_parts_at_photo", {"registration_id": registration, "x_px": x / 2, "y_px": y / 2}
        )
        capture.view = capture.view.with_size(width // 2, height)
        other_shape = await client.call_tool(
            "board_parts_at_photo", {"registration_id": registration, "x_px": 1, "y_px": 1}
        )

    assert registered.structured_content is not None
    assert half.structured_content is not None
    assert half.structured_content["scale"] == 2.0
    assert half.structured_content["x_px"] == x / 2
    assert half.structured_content["board_point"] == registered.structured_content["board_point"]
    assert half.structured_content["parts"] == registered.structured_content["parts"]
    assert other_shape.is_error
    assert "another shape" in text(other_shape)


def test_view_size_is_not_a_view_difference() -> None:
    assert view().with_size(100, 75).differences(view().with_size(50, 37)) == []
    assert view().differences(view(2.0)) == ["zoom_ratio"]


# endregion

# region: B-E5 board_identify and the registered photo


async def test_identify_refuses_pixels_of_another_photo_view(settings: Settings, tmp_path: Path) -> None:
    bench = Bench(settings, tmp_path)
    x, y = to_photo("U7301")
    async with Client(bench.server) as client:
        await client.call_tool("board_open", {"path": str(bench.board_file)})
        at_1x = photo_id(await client.call_tool("phone_snapshot", {}))
        bench.services.scene.watcher_alive()
        await client.call_tool("phone_zoom", {"ratio": 2.0})
        at_2x = photo_id(await client.call_tool("phone_snapshot", {}))
        registered = await client.call_tool("board_register_photo", {"side": "top", "pairs": pairs(), **SIZE})
        assert registered.structured_content is not None
        registration = registered.structured_content["registration_id"]
        refused = await client.call_tool(
            "board_identify",
            {"photo_id": at_1x, "marking": "U7301", "registration_id": registration, "x_px": x, "y_px": y},
        )
        accepted = await identify(client, photo_id=at_2x, marking="U7301", registration_id=registration, x_px=x, y_px=y)

    assert refused.is_error
    assert "camera view changed" in text(refused)
    assert accepted["state"] == "confirmed"


async def test_identify_follows_a_carried_registration(settings: Settings, tmp_path: Path) -> None:
    bench = Bench(settings, tmp_path)
    x, y = to_photo("U7301")
    async with Client(bench.server) as client:
        photo, registration = await bench.ready(client)
        session = bench.services.board
        old = session.registrations[registration]
        carried = old.model_copy(update={"registration_id": "carried-1", "carried_from": registration})
        old.mark_stale()
        session.registrations[carried.registration_id] = carried
        claim = await identify(client, photo_id=photo, marking="U7301", registration_id=registration, x_px=x, y_px=y)
        carried.photo_id = "another-photo"
        refused = await client.call_tool(
            "board_identify",
            {"photo_id": photo, "marking": "U7301", "registration_id": registration, "x_px": x, "y_px": y},
        )

    assert claim["state"] == "confirmed"
    assert claim["registration_id"] == "carried-1"
    assert f"registration {registration} carried over as carried-1" in claim["reason"]
    assert claim["x_px"] == x
    assert refused.is_error
    assert "moved" in text(refused)


# endregion

# region: B-E7 no scene watcher


def unwatched_scene(clock: FakeClock) -> SceneState:
    options = SceneOptions(
        unwatched_max_age=timedelta(seconds=MAX_AGE_S), watch_timeout=timedelta(seconds=WATCH_TIMEOUT_S)
    )
    return SceneState(clock=clock, options=options)


async def test_photos_expire_without_a_watcher() -> None:
    clock = FakeClock()
    scene = unwatched_scene(clock)
    log = CaptureLog()
    log.attach(scene)
    scene.snapshot_taken()
    photo = log.record(CaptureKind.PHONE_SNAPSHOT, "phone", view())
    clock.now += MAX_AGE_S
    assert not await scene.expire_unwatched()
    assert log.status(photo.capture_id).valid_for_position_claims

    clock.now += 1
    assert await scene.expire_unwatched()
    status = log.status(photo.capture_id)
    assert not status.valid_for_position_claims
    assert "no scene watcher" in status.reason
    assert "expire 1 minutes" in status.reason
    # One change is enough: it does not repeat.
    assert not await scene.expire_unwatched()


async def test_a_running_watcher_stops_the_expiry() -> None:
    clock = FakeClock()
    scene = unwatched_scene(clock)
    scene.snapshot_taken()
    for _ in range(3):
        clock.now += MAX_AGE_S
        scene.watcher_alive()
        assert scene.watching
        assert not await scene.expire_unwatched()
    # The watcher stopped: the time counts from its last frame.
    clock.now += MAX_AGE_S
    assert not scene.watching
    assert not await scene.expire_unwatched()
    clock.now += 1
    assert await scene.expire_unwatched()


async def test_a_view_change_without_a_watcher_makes_the_photos_stale() -> None:
    clock = FakeClock()
    scene = unwatched_scene(clock)
    log = CaptureLog()
    log.attach(scene)
    assert not await scene.unwatched_view_change("zoom")  # no photo yet: nothing to make stale
    scene.snapshot_taken()
    photo = log.record(CaptureKind.PHONE_SNAPSHOT, "phone", view())
    scene.watcher_alive()
    assert not await scene.unwatched_view_change("zoom")  # the watcher takes a new reference
    clock.now += WATCH_TIMEOUT_S + 1
    assert await scene.unwatched_view_change("zoom")
    assert "camera view changed (zoom)" in log.status(photo.capture_id).reason
    # A new photo is the scene again; a later change keeps the first reason for the old photo.
    scene.snapshot_taken()
    newer = log.record(CaptureKind.PHONE_SNAPSHOT, "phone", view())
    await scene.mark_changed()
    assert "camera view changed (zoom)" in log.status(photo.capture_id).reason
    assert "moved" in log.status(newer.capture_id).reason


async def test_zoom_without_a_watcher_through_the_tools(settings: Settings, tmp_path: Path) -> None:
    bench = Bench(settings, tmp_path)
    x, y = to_photo("U7301")
    async with Client(bench.server) as client:
        photo, registration = await bench.ready(client)
        await client.call_tool("phone_zoom", {"ratio": 2.0})
        status = await client.call_tool("capture_status", {"capture_id": photo})
        refused = await client.call_tool(
            "board_parts_at_photo", {"registration_id": registration, "x_px": x, "y_px": y}
        )

    assert status.structured_content is not None
    assert status.structured_content["valid_for_position_claims"] is False
    assert "camera view changed (zoom)" in status.structured_content["reason"]
    assert refused.is_error
    assert "camera view changed (zoom)" in text(refused)
    assert "board_register_photo again" in text(refused)


# endregion

# region: B-F5 and B-F7 stale reasons


async def test_a_new_stale_reason_replaces_the_old_one(settings: Settings, board_file: Path) -> None:  # noqa: F811
    services, _ = registered_services(settings, jpeg(first_photo()))
    async with Client(build_server(services)) as client:
        registration = await register(client, board_file)
        # The app restarted (no move): the same picture carries the registration over.
        services.board.mark_registrations_stale(RESTART_STALE_REASON)
        info = json.loads((await client.call_tool("phone_snapshot", {"max_side": 0})).content[0].text)
        carried_id = info["registration"]["registration_id"]
        carried = services.board.registrations[carried_id]
        assert info["registration"]["carried"] is True
        assert carried.stale_reason is None
        # A later move: the move message, not the app restart.
        await services.scene.mark_changed()
        refused = await client.call_tool("board_parts_at_photo", {"registration_id": carried_id, "x_px": 1, "y_px": 1})

    assert registration["registration_id"] != carried_id
    assert refused.is_error
    assert text(refused).endswith(STALE_REGISTRATION.format(id=carried_id))
    carried.mark_stale(TRACKING_LOST_REASON)
    assert carried.stale_reason == TRACKING_LOST_REASON
    carried.mark_stale()
    assert carried.stale_reason is None


def test_scene_stale_reason_keeps_braces_as_text() -> None:
    reason = scene_stale_reason("the camera view changed ({zoom}) while no scene watcher checks")
    assert reason.format(id="r1") == (
        "registration 'r1': the camera view changed ({zoom}) while no scene watcher checks, then call "
        "board_register_photo again"
    )


async def test_a_restored_registration_says_server_restart(settings: Settings, tmp_path: Path) -> None:
    store_path, registration_id = await before_restart(settings, tmp_path)
    second = RestartProcess(settings, store_path)
    async with Client(second.server) as client:
        located = await client.call_tool(
            "board_locate_in_photo", {"registration_id": registration_id, "refdes": ["J4"]}
        )

    assert located.is_error
    assert "restored after a server restart" in text(located)
    assert "moved" not in text(located)


# endregion

# region: B-F8 crop area


async def test_a_crop_outside_any_image_is_refused_before_the_still(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), no_vision())
    async with Client(build_server(services)) as client:
        negative = await client.call_tool("phone_snapshot", {"crop": {"x": -100, "y": -100, "radius": 10}})
        beyond = await client.call_tool("phone_snapshot", {"crop": {"x": 5000, "y": 10}})
        assert services.captures.latest_photo is None
        # Inside the max_side, but outside this small image: the photo stays, without the crop image.
        missed = await client.call_tool("phone_snapshot", {"crop": {"x": 1000, "y": 1000, "radius": 5}})

    assert negative.is_error
    assert "negative" in text(negative)
    assert beyond.is_error
    assert not missed.is_error
    info = json.loads(text(missed))
    assert info["crop"] is None
    assert "outside the image" in info["crop_error"]
    assert services.captures.latest_photo is not None
    assert services.captures.latest_photo.capture_id == info["capture_id"]


# endregion
