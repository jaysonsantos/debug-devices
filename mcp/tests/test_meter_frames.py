"""Multi-frame meter reads (meter_frames.py) and the user-confirmed meter mode. Bench feedback 2, items 1 and 2."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.types import ImageContent

from debug_devices_mcp.config import Settings
from debug_devices_mcp.meter_frames import MeterLimits, combine, signature
from debug_devices_mcp.multimeter import MeterMode, MeterResult, MeterStatus, MultimeterReading, check_reading
from debug_devices_mcp.server import Services, build_server

from .test_multimeter import READING, completion
from .test_server import FakePhone, make_services

LIMITS = MeterLimits(max_volts=30.0, max_amps=10.0)


def frame(
    display_text: str, value: float | None, unit: str = "V", mode: str = "dc_voltage", **changes: Any
) -> MeterResult:
    reading = MultimeterReading.model_validate(
        {**READING, "display_text": display_text, "value": value, "unit": unit, "mode": mode, **changes}
    )
    return check_reading(reading).model_copy(update={"capture_id": str(uuid.uuid7()), "captured_at": datetime.now(UTC)})


def test_signature_keeps_the_decimal_point_position() -> None:
    assert signature("5.10") == ("510", 1)
    assert signature("51.0") == ("510", 2)
    assert signature("OL") == ("", None)
    assert signature("-0.123") == ("0123", 1)


def test_same_frames_are_confirmed_and_stable() -> None:
    result = combine([frame("5.10", 5.10), frame("5.10", 5.10)], LIMITS)
    assert result.status is MeterStatus.CONFIRMED
    assert result.value == 5.10
    assert result.stable is True
    assert len(result.frames) == 2


def test_moved_decimal_point_is_disputed() -> None:
    result = combine([frame("5.10", 5.10), frame("51.0", 51.0)], LIMITS)
    assert result.status is MeterStatus.DISPUTED
    assert result.value is None
    assert any("decimal point moved" in problem for problem in result.problems)
    assert any("above the bench limit of 30 V" in problem for problem in result.problems)


@pytest.mark.parametrize(
    ("display_text", "value", "unit", "status"),
    [
        ("93.2", 93.2, "V", MeterStatus.DISPUTED),
        ("900", 900.0, "mV", MeterStatus.CONFIRMED),
        ("12.0", 12.0, "A", MeterStatus.DISPUTED),
        ("250", 250.0, "mA", MeterStatus.CONFIRMED),
    ],
)
def test_plausibility_limits(display_text: str, value: float, unit: str, status: MeterStatus) -> None:
    mode = "dc_current" if unit.endswith("A") else "dc_voltage"
    single = frame(display_text, value, unit, mode)
    assert combine([single, single.model_copy()], LIMITS).status is status


def test_changing_digits_are_uncertain_with_the_range() -> None:
    result = combine([frame("5.10", 5.10), frame("5.12", 5.12)], LIMITS)
    assert result.status is MeterStatus.UNCERTAIN
    assert result.value is None
    assert (result.value_min, result.value_max) == (5.10, 5.12)
    assert result.stable is False


def test_worst_frame_status_wins() -> None:
    blurred = frame("5.10", 5.10, unit="unknown", confidence=0.3)
    result = combine([frame("5.10", 5.10), blurred], LIMITS)
    assert result.status is MeterStatus.UNCERTAIN


# region: through the tool


def answers(*readings: dict[str, Any]) -> httpx.MockTransport:
    queue = list(readings)

    def handler(request: httpx.Request) -> httpx.Response:
        return completion(json.dumps(queue.pop(0) if len(queue) > 1 else queue[0]))

    return httpx.MockTransport(handler)


def services_with(settings: Settings, vision: httpx.MockTransport) -> Services:
    return make_services(settings, FakePhone(), vision)


async def test_tool_returns_every_frame_image(settings: Settings) -> None:
    services = services_with(settings, answers(READING))
    async with Client(build_server(services)) as client:
        result = await client.call_tool("multimeter_read", {"include_image": True, "frames": 3})

    assert result.structured_content is not None
    frames = result.structured_content["frames"]
    assert len(frames) == 3
    ids = [block.meta["capture_id"] for block in result.content if isinstance(block, ImageContent)]
    assert ids == [item["capture_id"] for item in frames]
    assert result.structured_content["capture_id"] == ids[0]
    assert all(services.captures.meter_results[capture_id].status == "confirmed" for capture_id in ids)


async def test_tool_disputes_a_misread_decimal_point(settings: Settings) -> None:
    good = {**READING, "display_text": "5.10", "value": 5.10}
    bad = {**READING, "display_text": "51.0", "value": 51.0}
    services = services_with(settings, answers(good, bad))
    async with Client(build_server(services)) as client:
        result = await client.call_tool("multimeter_read", {})

    assert result.structured_content is not None
    assert result.structured_content["status"] == "disputed"
    assert result.structured_content["value"] is None


# endregion

# region: user-confirmed mode (item 2)

DIODE_VOLTS = {**READING, "mode": "diode", "unit": "V", "display_text": "5.10", "value": 5.10, "confidence": 0.9}


async def confirm_mode(client: Client, mode: str) -> None:
    result = await client.call_tool("bench_state_update", {"meter_mode_confirmed_by_user": mode})
    assert not result.is_error, result.content


async def test_recent_user_mode_confirms_when_two_frames_agree(settings: Settings) -> None:
    services = services_with(settings, answers(DIODE_VOLTS))
    async with Client(build_server(services)) as client:
        await confirm_mode(client, "dc_voltage")
        two = await client.call_tool("multimeter_read", {})
        one = await client.call_tool("multimeter_read", {"frames": 1})

    assert two.structured_content is not None
    assert two.structured_content["status"] == "confirmed"
    assert two.structured_content["mode"] == "dc_voltage"
    assert two.structured_content["model_mode"] == "diode"
    assert two.structured_content["mode_source"] == "user"
    assert one.structured_content is not None
    assert one.structured_content["status"] == "uncertain"


async def test_user_mode_conflict_stays_disputed(settings: Settings) -> None:
    ohms = {**DIODE_VOLTS, "mode": "resistance", "unit": "kΩ"}
    services = services_with(settings, answers(ohms))
    async with Client(build_server(services)) as client:
        await confirm_mode(client, "dc_voltage")
        result = await client.call_tool("multimeter_read", {})

    assert result.structured_content is not None
    assert result.structured_content["status"] == "disputed"
    assert any("the user confirmed" in problem for problem in result.structured_content["problems"])


async def test_old_user_mode_is_not_used(settings: Settings) -> None:
    services = services_with(settings, answers(DIODE_VOLTS))
    async with Client(build_server(services)) as client:
        await confirm_mode(client, "dc_voltage")
        state = services.bench.load()
        assert state.meter_mode is not None
        state.meter_mode.recorded_at -= timedelta(minutes=11)
        services.bench.save(state)
        result = await client.call_tool("multimeter_read", {})

    assert result.structured_content is not None
    assert result.structured_content["mode_source"] == "lcd"
    assert result.structured_content["mode"] == "diode"


def test_user_mode_in_the_single_frame_check() -> None:
    reading = MultimeterReading.model_validate(DIODE_VOLTS)
    checked = check_reading(reading, user_mode=MeterMode.DC_VOLTAGE)
    assert checked.mode is MeterMode.DC_VOLTAGE
    assert checked.model_mode is MeterMode.DIODE
    assert checked.status is MeterStatus.CONFIRMED


# endregion
