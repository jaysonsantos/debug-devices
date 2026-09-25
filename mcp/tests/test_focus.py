"""Phone distance and detail: the math, the advice, old apps, and the phone_status result."""

import pytest
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.focus import Advice, PhoneStatusReport, detail_px_per_mm, distance_cm, focus_report
from debug_devices_mcp.phone_api import CameraStatus, Optics
from debug_devices_mcp.server import build_server
from debug_devices_mcp.ui.events import EventBus

from .test_phone_api import STATUS
from .test_server import FakePhone, make_services, no_vision

OPTICS = {"focal_length_mm": 6.07, "sensor_width_mm": 9.14, "output_width_px": 4080}


def status(diopters: float | None, calibration: str = "approximate", minimum: float = 10.0) -> CameraStatus:
    focus = {
        "distance_diopters": diopters,
        "state": "focused",
        "calibration": calibration,
        "min_distance_diopters": minimum,
    }
    return CameraStatus.model_validate(STATUS | {"focus": focus, "optics": OPTICS})


def test_the_math_with_known_values() -> None:
    distance = distance_cm(3.41)
    assert distance == pytest.approx(29.3, abs=0.1)
    assert detail_px_per_mm(Optics(**OPTICS), distance) == pytest.approx(9.3, abs=0.1)
    assert detail_px_per_mm(Optics(**OPTICS), 10) == pytest.approx(27, abs=0.5)
    assert distance_cm(0) is None  # infinity
    assert distance_cm(None) is None


def test_far_says_how_close_to_move() -> None:
    report = focus_report(status(3.41))
    assert (report.distance_cm, report.detail_px_per_mm, report.min_distance_cm) == (29, 9.2, 10)
    assert report.advice is Advice.FAR
    assert report.advice_text == "move closer: about 11 cm gives about 24.6 px/mm"


def test_good_and_too_close() -> None:
    assert focus_report(status(10.0)).advice is Advice.GOOD
    close = focus_report(status(15.0))
    assert close.advice is Advice.TOO_CLOSE
    assert close.distance_cm == 6.7  # one decimal below 10 cm
    assert "move back" in close.advice_text


def test_uncalibrated_shows_no_cm() -> None:
    report = focus_report(status(3.41, calibration="uncalibrated"))
    assert report.distance_cm is None
    assert report.detail_px_per_mm is None
    assert report.advice is Advice.UNKNOWN
    assert report.calibration == "uncalibrated"


def test_infinity_no_value_and_fixed_focus() -> None:
    assert focus_report(status(0.0)).advice is Advice.FAR  # focused at infinity: far from the board
    assert focus_report(status(None)).advice is Advice.UNKNOWN
    fixed = focus_report(status(15.0, minimum=0.0))  # fixed focus: never "too close"
    assert fixed.min_distance_cm is None
    assert fixed.advice is Advice.GOOD


def test_an_old_app_without_focus_still_works() -> None:
    old = CameraStatus.model_validate(STATUS)
    assert old.focus is None
    assert old.optics is None
    assert focus_report(old).advice is Advice.UNKNOWN
    unknown_values = CameraStatus.model_validate(
        STATUS | {"focus": {"distance_diopters": 3, "state": "new", "calibration": "new", "min_distance_diopters": 5}}
    )
    assert unknown_values.focus is not None
    assert (unknown_values.focus.state, unknown_values.focus.calibration) == ("unknown", "uncalibrated")


def test_the_page_state_gets_the_report() -> None:
    bus = EventBus()
    bus.update_phone(status=status(3.41))
    assert bus.phone.focus is not None
    assert bus.phone.focus.distance_cm == 29


async def test_phone_status_returns_the_distance(settings: Settings) -> None:
    phone = FakePhone()
    phone.status.update(
        focus={
            "distance_diopters": 3.41,
            "state": "focused",
            "calibration": "approximate",
            "min_distance_diopters": 10.0,
        },
        optics=OPTICS,
    )
    async with Client(build_server(make_services(settings, phone, no_vision()))) as client:
        result = await client.call_tool("phone_status", {})
    data = result.structured_content
    assert data is not None
    assert (data["distance_cm"], data["detail_px_per_mm"], data["advice"]) == (29, 9.2, "far")
    assert data["zoom_ratio"] == STATUS["zoom_ratio"]  # still the camera status
    assert PhoneStatusReport.model_validate(data).focus is not None
