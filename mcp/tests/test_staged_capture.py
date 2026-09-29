"""A staged capture from the monitor page (ui/staged_capture.py and the page routes): the phone photo and the meter
reading of the key press, the privacy rule (no crop box: no meter part, no vision call), the bench gate at capture
time, and the page routes. Fakes only."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from debug_devices_mcp.config import Settings
from debug_devices_mcp.instructions import EVIDENCE_RULES
from debug_devices_mcp.staged import MAX_STAGED, StagedState, StagedStore
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.constants import tools
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import SettingsStore
from debug_devices_mcp.ui.staged_capture import NO_CROP, StagedCapturer
from debug_devices_mcp.webcam import Crop

from .conftest import JPEG, make_jpeg
from .test_markings import START
from .test_multimeter import READING, completion
from .test_server import FakePhone, make_services

BASE_URL = "http://127.0.0.1:18766"
PAGE = {"Origin": BASE_URL}
CROP = Crop(x=10, y=20, width=300, height=120)


class MeterWebcam:
    """The webcam at the multimeter: `crop` None is a webcam without a crop box."""

    device = Path("/dev/video0")

    def __init__(self, crop: Crop | None) -> None:
        self.crop = crop
        self.captures = 0

    async def capture_jpeg(self) -> bytes:
        self.captures += 1
        return JPEG


class Vision:
    """The vision model: counts the requests, answers `reading` (default READING: 4.98 V DC)."""

    def __init__(self, reading: dict | None = None) -> None:
        self.requests = 0
        self.reading = reading if reading is not None else READING

    def transport(self) -> httpx.MockTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            self.requests += 1
            return completion(json.dumps(self.reading))

        return httpx.MockTransport(answer)


def capture_setup(
    settings: Settings, tmp_path: Path, crop: Crop | None = CROP, phone_up: bool = True, reading: dict | None = None
) -> tuple[StagedCapturer, Any, Vision, MeterWebcam]:
    vision = Vision(reading)
    phone = FakePhone(up=phone_up)
    phone.snapshot = make_jpeg(64, 48)
    fast = settings.model_copy(update={"meter_frame_interval": timedelta(0)})
    services = make_services(fast, phone, vision.transport())
    webcam = MeterWebcam(crop)
    services.webcam = webcam
    capturer = StagedCapturer(services, StagedStore(tmp_path / "staged"), lambda: "test 1")
    return capturer, services, vision, webcam


async def test_a_key_press_stages_the_photo_and_the_meter_result(settings: Settings, tmp_path: Path) -> None:
    capturer, services, vision, webcam = capture_setup(settings, tmp_path)
    staged = await capturer.capture()
    assert staged.state is StagedState.PENDING
    await capturer.wait()
    [capture] = await capturer.store.list()
    assert capture.state is StagedState.READY
    assert capture.meter is not None
    assert (capture.meter.value, capture.meter.unit, str(capture.meter.status)) == (4.98, "V", "confirmed")
    assert capture.photo is not None
    assert (capture.photo.width, capture.photo.height) == (64, 48)
    assert capture.meter_frames == 2
    assert (vision.requests, webcam.captures) == (2, 2)
    assert await capturer.store.photo(capture.capture_id) is not None
    # The staged photo is not the agent's photo: the pixel tools keep their snapshot.
    assert services.last_snapshot is None
    # A second press stages a second capture.
    await capturer.capture()
    await capturer.wait()
    assert len(await capturer.store.list()) == 2


async def test_without_a_crop_box_no_frame_goes_to_the_vision_model(settings: Settings, tmp_path: Path) -> None:
    capturer, _, vision, webcam = capture_setup(settings, tmp_path, crop=None)
    await capturer.capture()
    await capturer.wait()
    [capture] = await capturer.store.list()
    assert capture.meter is None
    assert capture.meter_frames == 0
    assert NO_CROP in capture.notes
    assert (vision.requests, webcam.captures) == (0, 0)
    # The photo still comes.
    assert capture.photo is not None
    assert capture.state is StagedState.READY


async def test_no_phone_and_no_crop_is_a_failed_capture_with_the_reasons(settings: Settings, tmp_path: Path) -> None:
    capturer, _, _, _ = capture_setup(settings, tmp_path, crop=None, phone_up=False)
    await capturer.capture()
    await capturer.wait()
    [capture] = await capturer.store.list()
    assert capture.state is StagedState.FAILED
    assert any(note.startswith("no phone photo:") for note in capture.notes)
    assert NO_CROP in capture.notes


async def test_an_unsafe_voltage_closes_the_gate_at_capture_time(settings: Settings, tmp_path: Path) -> None:
    capturer, services, _, _ = capture_setup(settings, tmp_path)
    assert services.bench.load().last_unsafe_at is None
    await capturer.capture()
    await capturer.wait()
    # 4.98 V DC: the bench state has the unsafe reading now, while the capture still waits in the queue.
    assert services.bench.load().last_unsafe_at is not None
    [capture] = await capturer.store.list()
    assert capture.meter is not None
    assert capture.meter.bench_notice


# region: the page routes


def page_client(monitor: Monitor) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(monitor)), base_url=BASE_URL)


async def test_the_page_routes(settings: Settings, tmp_path: Path) -> None:
    capturer, _, _, _ = capture_setup(settings, tmp_path)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.staged = capturer
    async with page_client(monitor) as client:
        # Only the page can capture, delete, or clear (a same-origin request).
        assert (await client.post("/api/staged")).status_code == 403
        created = await client.post("/api/staged", headers=PAGE)
        assert created.status_code == 202, created.text
        capture_id = created.json()["capture_id"]
        await capturer.wait()
        listed = (await client.get("/api/staged")).json()
        [view] = listed["captures"]
        assert (view["state"], view["meter_text"], view["meter_status"], view["has_photo"]) == (
            "ready",
            "4.98 V",
            "confirmed",
            True,
        )
        assert listed["limit"] == MAX_STAGED
        photo = await client.get(f"/api/staged/{capture_id}/photo.jpg")
        assert photo.headers["content-type"] == "image/jpeg"
        assert (await client.delete(f"/api/staged/{capture_id}")).status_code == 403
        assert (await client.delete(f"/api/staged/{capture_id}", headers=PAGE)).json() == {"changed": 1}
        await client.post("/api/staged", headers=PAGE)
        await capturer.wait()
        assert (await client.delete("/api/staged", headers=PAGE)).json() == {"changed": 1}
    rows = [call for call in monitor.bus.calls() if call.tool == tools.STAGED_CAPTURE]
    assert len(rows) == 2


async def test_a_full_queue_is_a_page_message(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("debug_devices_mcp.staged.MAX_STAGED", 1)
    capturer, _, _, _ = capture_setup(settings, tmp_path)
    monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
    monitor.staged = capturer
    async with page_client(monitor) as client:
        assert (await client.post("/api/staged", headers=PAGE)).status_code == 202
        full = await client.post("/api/staged", headers=PAGE)
        await capturer.wait()
    assert full.status_code == 409
    assert "multimeter_read" in full.json()["error"]


# endregion: the page routes


def test_the_evidence_rules_name_staged_captures() -> None:
    # The agent calls multimeter_read when the user captured; a staged photo and value show their capture time only.
    assert "call multimeter_read: staged captures come first" in EVIDENCE_RULES
    assert "evidence of their capture time only" in EVIDENCE_RULES
