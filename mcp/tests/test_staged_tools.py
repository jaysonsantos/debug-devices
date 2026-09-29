"""Staged captures, part 2: multimeter_read pops the queue (oldest first; live only when none wait; `live: true` skips
it), staged_captures lists it, another server pops the page server's captures, and the QA fixes N88 (the camera view
of an agent photo during a capture), N89 (the crop cleared during a capture), N90 (only a UUID names a file), and N91
(the refusal text). Fakes only."""

import asyncio
import base64
import io
import os
import time
from pathlib import Path

import httpx
import pytest
from mcp import Client
from PIL import Image as PilImage

from debug_devices_mcp.bench_state import BenchStateStore
from debug_devices_mcp.config import Settings
from debug_devices_mcp.images import transform_jpeg
from debug_devices_mcp.server import (
    CROP_REMOVED,
    META_STAGED,
    STAGED_METER_CROP,
    STAGED_METER_FRAME,
    STAGED_PHOTO,
    build_server,
)
from debug_devices_mcp.staged import STAGED_NOTE, StagedStore
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import SettingsStore
from debug_devices_mcp.ui.staged_capture import StagedCapturer

from .conftest import make_jpeg
from .test_bench_state import call, identity_board
from .test_markings import START
from .test_multimeter import READING
from .test_staged_capture import BASE_URL, CROP, MeterWebcam, capture_setup

AGENT_STILL_DELAY = 0.5
STAGED_STILL_DELAY = 0.3


def texts(result) -> list[str]:
    return [block.text for block in result.content if block.type == "text"]


def images(result) -> list[dict]:
    return [block.meta or {} for block in result.content if block.type == "image"]


async def stage(capturer: StagedCapturer, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        ids.append((await capturer.capture()).capture_id)
        await capturer.wait()
    return ids


# region: multimeter_read and staged_captures


async def test_multimeter_read_pops_all_oldest_first_then_reads_live(settings: Settings, tmp_path: Path) -> None:
    capturer, services, vision, _ = capture_setup(settings, tmp_path)
    capturer.store = services.staged
    ids = await stage(capturer, 2)
    async with Client(build_server(services)) as client:
        listed = await client.call_tool("staged_captures", {})
        assert listed.structured_content is not None
        assert [item["capture_id"] for item in listed.structured_content["captures"]] == ids
        # The list does not remove them.
        assert len(await services.staged.list()) == 2
        popped = await client.call_tool("multimeter_read", {"include_image": True})
        before_live = vision.requests
        live = await client.call_tool("multimeter_read", {})
    assert not popped.is_error, popped.content
    batch = popped.structured_content
    assert batch is not None
    assert [item["capture_id"] for item in batch["staged"]] == ids
    assert batch["count"] == 2
    assert batch["note"] == STAGED_NOTE
    assert all(item["meter"]["value"] == 4.98 for item in batch["staged"])
    assert all(item["age_s"] >= 0 for item in batch["staged"])
    # Each capture: its photo, its meter image, then (include_image) its other meter frame, each with its capture id.
    kinds = [(meta["capture_id"], meta[META_STAGED]) for meta in images(popped)]
    assert kinds == [
        (ids[0], STAGED_PHOTO),
        (ids[0], STAGED_METER_CROP),
        (ids[0], STAGED_METER_FRAME),
        (ids[1], STAGED_PHOTO),
        (ids[1], STAGED_METER_CROP),
        (ids[1], STAGED_METER_FRAME),
    ]
    # The queue is empty: the next call reads live.
    assert live.structured_content is not None
    assert live.structured_content["status"] == "confirmed"
    assert vision.requests == before_live + 2


class NumberedWebcam(MeterWebcam):
    """Each meter frame has another width (21, 22, ... px), so a test sees which frame an image is."""

    async def capture_jpeg(self) -> bytes:
        self.captures += 1
        return make_jpeg(20 + self.captures, 10)


async def test_a_staged_pop_has_the_photo_the_meter_image_and_the_reading(settings: Settings, tmp_path: Path) -> None:
    # The user's request: each staged capture gives the phone photo, the meter picture, and the reading, also without
    # include_image.
    capturer, services, _, _ = capture_setup(settings, tmp_path)
    capturer.store = services.staged
    services.webcam = NumberedWebcam(CROP)
    [capture_id] = await stage(capturer, 1)
    async with Client(build_server(services)) as client:
        popped = await client.call_tool("multimeter_read", {})
    assert popped.structured_content is not None
    [reading] = popped.structured_content["staged"]
    assert (reading["meter"]["value"], reading["meter"]["unit"]) == (4.98, "V")
    blocks = [block for block in popped.content if block.type == "image"]
    assert [((block.meta or {})["capture_id"], (block.meta or {})[META_STAGED]) for block in blocks] == [
        (capture_id, STAGED_PHOTO),
        (capture_id, STAGED_METER_CROP),
    ]
    # The meter image is the crop frame that gave the reading: the first frame (its capture id is the result's).
    with PilImage.open(io.BytesIO(base64.b64decode(blocks[1].data))) as meter_image:
        assert meter_image.size == (21, 10)


async def test_live_true_skips_the_queue(settings: Settings, tmp_path: Path) -> None:
    capturer, services, _, _ = capture_setup(settings, tmp_path)
    capturer.store = services.staged
    await stage(capturer, 1)
    async with Client(build_server(services)) as client:
        live = await client.call_tool("multimeter_read", {"live": True})
    assert live.structured_content is not None
    assert "staged" not in live.structured_content
    assert len(await services.staged.list()) == 1


async def test_another_server_pops_the_page_servers_captures(settings: Settings, tmp_path: Path) -> None:
    page_capturer, page_services, _, _ = capture_setup(settings, tmp_path)
    page_capturer.store = page_services.staged
    ids = await stage(page_capturer, 2)
    # A second MCP server (for example the bench session) on the same state folder.
    other, _, _, _ = capture_setup(settings, tmp_path / "other")
    other_services = other._services
    assert other_services.staged.directory == page_services.staged.directory
    async with Client(build_server(other_services)) as client:
        popped = await client.call_tool("multimeter_read", {})
    assert popped.structured_content is not None
    assert [item["capture_id"] for item in popped.structured_content["staged"]] == ids
    assert await page_services.staged.list() == []


# endregion: multimeter_read and staged_captures

# region: N88 the camera view of an agent photo during a capture


async def test_a_capture_does_not_change_the_view_of_an_agent_photo(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dd-qa probe B1: an agent photo at zoom 1, zoom 2, then a capture and an agent phone_snapshot together.
    capturer, services, _, _ = capture_setup(settings, tmp_path, crop=None)
    delays = {"agent": AGENT_STILL_DELAY}

    def slow_transform(*args: object) -> bytes:
        time.sleep(delays.pop("agent", STAGED_STILL_DELAY))
        return transform_jpeg(*args)  # type: ignore[arg-type]

    async with Client(build_server(services)) as client:
        await client.call_tool("phone_snapshot", {})
        await client.call_tool("phone_zoom", {"ratio": 2.0})
        monkeypatch.setattr("debug_devices_mcp.server.transform_jpeg", slow_transform)
        delays["agent"] = STAGED_STILL_DELAY  # the capture starts first: its still takes 0.3 s
        await capturer.capture()
        await asyncio.sleep(0.05)
        delays["agent"] = AGENT_STILL_DELAY  # the agent's still takes 0.5 s and ends after the capture's
        await client.call_tool("phone_snapshot", {})
        await capturer.wait()
    latest = services.captures.latest_photo
    assert latest is not None
    assert latest.view is not None
    assert latest.view.zoom_ratio == 2.0


# endregion: N88

# region: N89 the crop cleared during a capture


class ClearingWebcam(MeterWebcam):
    """The user clears the crop box during the capture: after the first frame, or while the second frame is read."""

    def __init__(self, clear_while_reading: bool) -> None:
        super().__init__(CROP)
        self.clear_while_reading = clear_while_reading
        self.crops_seen: list[object] = []

    async def capture_jpeg(self) -> bytes:
        self.crops_seen.append(self.crop)
        jpeg = await super().capture_jpeg()
        if self.clear_while_reading or self.captures == 1:
            self.crop = None
        return jpeg


@pytest.mark.parametrize("clear_while_reading", [False, True], ids=["after-the-first-frame", "while-reading"])
async def test_a_crop_cleared_during_the_capture_stops_it(
    settings: Settings, tmp_path: Path, clear_while_reading: bool
) -> None:
    capturer, services, vision, _ = capture_setup(settings, tmp_path)
    webcam = ClearingWebcam(clear_while_reading)
    services.webcam = webcam
    await capturer.capture()
    await capturer.wait()
    [capture] = await capturer.store.list()
    # Every frame that the webcam read had the crop box; no frame without it went to the vision model or the store.
    assert all(crop is not None for crop in webcam.crops_seen)
    assert vision.requests <= 1
    assert vision.requests <= webcam.captures
    assert capture.meter is None
    assert capture.meter_frames == 0
    assert any(CROP_REMOVED in note for note in capture.notes)


# endregion: N89

# region: N90, N91, and the page log


async def test_only_a_capture_id_names_a_file(tmp_path: Path) -> None:
    store = StagedStore(tmp_path / "staged")
    (tmp_path / "secret.photo.jpg").write_bytes(b"secret")
    assert await store.photo("../secret") is None
    assert await store.delete("../secret") is False
    assert (tmp_path / "secret.photo.jpg").exists()


async def test_the_refusal_names_the_staged_captures(settings: Settings, tmp_path: Path) -> None:
    capturer, _, _, _ = capture_setup(settings, tmp_path)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.staged = capturer
    transport = httpx.ASGITransport(app=create_app(monitor))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        refused = await client.post("/api/staged")
        missing = await client.get("/api/staged/not-a-capture/photo.jpg")
    assert refused.status_code == 403
    assert "staged captures" in refused.json()["error"]
    assert missing.status_code == 404


async def test_the_page_log_names_a_staged_batch(settings: Settings, tmp_path: Path) -> None:
    capturer, services, _, _ = capture_setup(settings, tmp_path)
    capturer.store = services.staged
    await stage(capturer, 2)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.instrument(build_server(services))
    call, result = await monitor.call_from_ui("multimeter_read", {})
    assert not result.is_error
    assert call.summary == "2 staged captures (the moments of their capture)"
    assert "reading" not in call.details
    assert STAGED_NOTE in texts(result)[0]


# endregion: N90, N91, and the page log


def test_the_store_is_in_the_private_state_folder(settings: Settings, tmp_path: Path) -> None:
    # conftest gives every test its own $XDG_STATE_HOME: a test never pops the user's real queue.
    _, services, _, _ = capture_setup(settings, tmp_path)
    assert services.staged.directory.is_relative_to(Path(os.environ["XDG_STATE_HOME"]))


# region: N92 record a popped reading in any server, and N93 the parameters that were not applied

SAFE_READING = {**READING, "value": 0.01, "display_text": "0.01"}


def two_servers(settings: Settings, tmp_path: Path, reading: dict) -> tuple[StagedCapturer, object]:
    """The page server that captures, and a second server (the bench session) on the same state and bench files."""
    capturer, page, _, _ = capture_setup(settings, tmp_path, reading=reading)
    capturer.store = page.staged
    other, _, _, _ = capture_setup(settings, tmp_path / "other")
    bench_session = other._services
    shared_bench = BenchStateStore(tmp_path / "bench-state.json")
    page.bench = shared_bench
    bench_session.bench = shared_bench
    bench_session.board.board = identity_board()
    return capturer, bench_session


async def test_a_second_server_records_a_staged_reading_by_each_id(settings: Settings, tmp_path: Path) -> None:
    capturer, bench_session = two_servers(settings, tmp_path, SAFE_READING)
    await stage(capturer, 3)
    async with Client(build_server(bench_session)) as client:  # type: ignore[arg-type]
        popped = await call(client, "multimeter_read")
        first, second, third = popped["staged"]
        ids = [first["capture_id"], second["meter"]["capture_id"], third["meter"]["frames"][1]["capture_id"]]
        labels = ["C8850.1", "L501.1", "L501.2"]
        recorded = [
            await call(client, "bench_record_measurement", capture_id=capture_id, label=label)
            for capture_id, label in zip(ids, labels, strict=True)
        ]
    points = recorded[-1]["state"]["residual_points"]
    assert [(point["point"], point["safe"]) for point in points] == [
        ("c8850.1", True),
        ("l501.1", True),
        ("l501.2", True),
    ]


async def test_an_unsafe_staged_reading_gets_its_point_name(settings: Settings, tmp_path: Path) -> None:
    capturer, bench_session = two_servers(settings, tmp_path, READING)
    await stage(capturer, 1)
    async with Client(build_server(bench_session)) as client:  # type: ignore[arg-type]
        # The capture closed the gate at an unknown point, in the page server, before any pop.
        before = await call(client, "bench_state")
        assert any("unknown point" in item for item in before["gate"]["missing"])
        popped = await call(client, "multimeter_read")
        named = await call(
            client, "bench_record_measurement", capture_id=popped["staged"][0]["capture_id"], label="L501.2"
        )
    assert [(point["point"], point["safe"]) for point in named["state"]["residual_points"]] == [("l501.2", False)]
    assert not any("unknown point" in item for item in named["gate"]["missing"])


async def test_the_batch_names_the_parameters_that_were_not_applied(settings: Settings, tmp_path: Path) -> None:
    capturer, services, _, _ = capture_setup(settings, tmp_path)
    capturer.store = services.staged
    await stage(capturer, 1)
    async with Client(build_server(services)) as client:
        given = await call(client, "multimeter_read", expected_mode="resistance", frames=3)
        await stage(capturer, 1)
        plain = await call(client, "multimeter_read")
    assert given["not_applied"] == ["expected_mode", "frames"]
    assert "expected_mode, frames: given, but not applied" in given["not_applied_note"]
    assert "live: true" in given["not_applied_note"]
    assert plain["not_applied"] == []
    assert plain["not_applied_note"] is None


# endregion: N92 and N93
