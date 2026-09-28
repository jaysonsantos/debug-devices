"""The decimal point of the meter (docs/briefs/meter-decimal.md). A check can only lower a status, never confirm."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mcp import Client

from debug_devices_mcp.config import Settings
from debug_devices_mcp.meter_frames import MeterLimits, combine
from debug_devices_mcp.multimeter import (
    DISAGREE_PROBLEM,
    MeterResult,
    MeterStatus,
    MultimeterReading,
    build_request,
    check_reading,
    display_digits,
    reading_json_schema,
)
from debug_devices_mcp.server import build_server

from .test_multimeter import READING, completion
from .test_server import FakePhone, make_services


def reading(display_text: str, value: float | None, **changes: Any) -> MultimeterReading:
    return MultimeterReading.model_validate({**READING, "display_text": display_text, "value": value, **changes})


# region: round 1, digits and decimal point fields


def test_schema_asks_for_the_digits_and_the_point() -> None:
    schema = reading_json_schema()
    assert schema["properties"]["digits"]["type"] == ["string", "null"]
    assert schema["properties"]["digits_before_point"]["type"] == ["integer", "null"]
    assert {"digits", "digits_before_point"} <= set(schema["required"])


@pytest.mark.parametrize(
    ("display_text", "value", "digits", "before"),
    [
        ("1.415", 1.415, "1415", 1),
        ("-0.123", -0.123, "0123", 1),
        ("443.0", 443.0, "4430", 3),
        ("512", 512.0, "512", None),
    ],
)
def test_agreeing_fields_stay_confirmed(display_text: str, value: float, digits: str, before: int | None) -> None:
    result = check_reading(reading(display_text, value, digits=digits, digits_before_point=before))
    assert result.status is MeterStatus.CONFIRMED
    assert result.value == value
    assert (result.digits, result.digits_before_point) == (digits, before)


def test_value_that_does_not_fit_the_lcd_text_is_uncertain() -> None:
    result = check_reading(reading("1.415", 14.15, digits="1415", digits_before_point=1))
    assert result.status is MeterStatus.UNCERTAIN
    assert result.value is None
    assert any(problem.startswith(DISAGREE_PROBLEM) for problem in result.problems)


def test_point_field_that_does_not_fit_the_lcd_text_is_uncertain() -> None:
    result = check_reading(reading("14.15", 14.15, digits="1415", digits_before_point=1))
    assert result.status is MeterStatus.UNCERTAIN
    assert "the fields say 1415 with 1" in " ".join(result.problems)


def test_overload_and_missing_fields_are_not_checked() -> None:
    overload = check_reading(reading("O.L", None, mode="resistance", unit="MΩ"))
    assert overload.status is MeterStatus.CONFIRMED
    # A caller without the new fields (older data): only the value is compared with the LCD text.
    assert check_reading(reading("4.98", 4.98)).status is MeterStatus.CONFIRMED


def test_the_consistency_check_never_raises_a_status() -> None:
    disputed = check_reading(reading("1.415", 14.15, mode="resistance", unit="V"))
    assert disputed.status is MeterStatus.DISPUTED


# endregion


# region: round 2, meter display profile (meter_counts)

COUNTS = 6000


@pytest.mark.parametrize("display_text", ["51.0", "93.2"])
def test_three_digits_on_a_four_digit_display_are_uncertain(display_text: str) -> None:
    result = check_reading(reading(display_text, float(display_text)), counts=COUNTS)
    assert result.status is MeterStatus.UNCERTAIN
    assert result.value is None
    assert any("the meter shows 4 digits; the model read 3" in problem for problem in result.problems)


def test_value_above_the_counts_is_uncertain() -> None:
    result = check_reading(reading("6.123", 6.123), counts=COUNTS)
    assert result.status is MeterStatus.UNCERTAIN
    assert any("above the 6000 counts" in problem for problem in result.problems)


def test_leading_zero_in_auto_range_is_uncertain() -> None:
    auto = check_reading(reading("05.10", 5.10, flags=["AUTO"]), counts=COUNTS)
    assert auto.status is MeterStatus.UNCERTAIN
    assert any("leading zero" in problem for problem in auto.problems)
    by_range = check_reading(reading("05.10", 5.10, flags=[], range="auto"), counts=COUNTS)
    assert by_range.status is MeterStatus.UNCERTAIN
    # "0.123" is possible in auto range, and "05.10" in a manual range.
    assert check_reading(reading("0.123", 0.123, flags=["AUTO"]), counts=COUNTS).status is MeterStatus.CONFIRMED
    assert check_reading(reading("05.10", 5.10, flags=[], range="20V"), counts=COUNTS).status is MeterStatus.CONFIRMED


def test_four_digits_within_the_counts_stay_confirmed() -> None:
    for display_text in ("1.415", "5.100", "443.0", "5999"):
        assert check_reading(reading(display_text, float(display_text)), counts=COUNTS).status is MeterStatus.CONFIRMED


def test_unknown_counts_check_nothing() -> None:
    assert check_reading(reading("51.0", 51.0)).status is MeterStatus.CONFIRMED


def test_prompt_names_the_digit_count() -> None:
    assert display_digits(6000) == 4
    assert display_digits(20000) == 5
    text = build_request("m", b"jpeg", "PROSTER T21D", COUNTS).messages[1].content[0].text  # type: ignore[union-attr]
    assert text.endswith(
        "For volts and amperes, its display has 6000 counts: at most 4 digits. The lowest range can show a blank for "
        "the leading digit: do not add a zero for it."
    )


# endregion


# region: round 3, expected value (context)

LIMITS = MeterLimits(max_volts=30.0, max_amps=10.0)
AUTO = {"flags": ["AUTO"], "range": "auto"}


def frames(display_text: str, value: float, count: int = 3, counts: int = 0, **changes: Any) -> list[MeterResult]:
    checked = check_reading(reading(display_text, value, **{**AUTO, **changes}), counts=counts)
    return [
        checked.model_copy(update={"capture_id": str(uuid.uuid7()), "captured_at": datetime.now(UTC)})
        for _ in range(count)
    ]


def test_value_far_above_the_expected_value_is_uncertain_and_names_the_shift() -> None:
    result = combine(frames("14.15", 14.15), LIMITS, expected_value=3.3)
    assert result.status is MeterStatus.UNCERTAIN
    assert result.value is None
    assert result.expected_value == 3.3
    assert "14.15 V is 4.3 \u00d7 the expected 3.3 V" in result.problems[0]
    assert result.problems[1] == "1.415 V, with the point one place to the left, is near the expected 3.3 V"
    assert result.request is not None
    assert "ask the user to read the LCD" in result.request


def test_value_below_the_band_names_a_shift_to_the_right() -> None:
    result = combine(frames("0.512", 0.512), LIMITS, expected_value=5.0)
    assert result.status is MeterStatus.UNCERTAIN
    assert "with the point one place to the right" in result.problems[-1]


def test_value_in_the_band_stays_confirmed() -> None:
    assert combine(frames("3.281", 3.281), LIMITS, expected_value=3.3).status is MeterStatus.CONFIRMED
    # Expected 0 (or none): no check.
    assert combine(frames("14.15", 14.15), LIMITS, expected_value=0).status is MeterStatus.CONFIRMED


def test_prefixes_are_converted_to_the_base_unit() -> None:
    millivolts = combine(frames("3300", 3300.0, unit="mV"), LIMITS, expected_value=3.3)
    assert millivolts.status is MeterStatus.CONFIRMED
    kilohms = combine(frames("443.0", 443.0, mode="resistance", unit="k\u03a9"), LIMITS, expected_value=440_000)
    assert kilohms.status is MeterStatus.CONFIRMED
    far = combine(frames("44.30", 44.30, mode="resistance", unit="k\u03a9"), LIMITS, expected_value=440_000)
    assert far.status is MeterStatus.UNCERTAIN
    assert "443000 \u03a9, with the point one place to the right" in far.problems[-1]


def test_a_shift_that_the_display_cannot_show_is_not_named() -> None:
    # "0.510" -> "05.10" is not possible in auto range with a known count; "051.0" neither.
    result = combine(frames("0.510", 0.510, counts=6000), LIMITS, expected_value=51.0, counts=6000)
    assert result.status is MeterStatus.UNCERTAIN
    assert not any("with the point" in problem for problem in result.problems)


def test_the_expected_value_never_raises_a_status() -> None:
    low = frames("3.300", 3.3, confidence=0.3)
    assert combine(low, LIMITS, expected_value=3.3).status is MeterStatus.UNCERTAIN


# endregion


# region: round 4, the brief's cases through the tools (fake vision answers)


def answer(display_text: str, value: float | None, digits: str | None, before: int | None) -> dict[str, Any]:
    return {
        **READING,
        **AUTO,
        "display_text": display_text,
        "value": value,
        "digits": digits,
        "digits_before_point": before,
        "unit": "V",
        "mode": "dc_voltage",
        "confidence": 0.9,
    }


def fake_vision(model_answer: dict[str, Any]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: completion(json.dumps(model_answer)))


async def read(settings: Settings, model_answer: dict[str, Any], **arguments: object) -> dict[str, Any]:
    services = make_services(settings, FakePhone(), fake_vision(model_answer))
    async with Client(build_server(services)) as client:
        result = await client.call_tool("multimeter_read", arguments)
    assert result.structured_content is not None, result.content
    return result.structured_content


async def test_bench_case_14_15_with_expected_3_3_is_uncertain(settings: Settings) -> None:
    result = await read(settings, answer("14.15", 14.15, "1415", 2), frames=3, expected_value=3.3)
    assert result["status"] == "uncertain"
    assert result["value"] is None
    assert len(result["frames"]) == 3
    assert any("1.415 V" in problem for problem in result["problems"])


async def test_51_0_with_6000_counts_is_uncertain(settings: Settings) -> None:
    settings.meter_counts = 6000
    result = await read(settings, answer("51.0", 51.0, "510", 2))
    assert result["status"] in {"uncertain", "disputed"}  # also above the 30 V bench limit
    assert any("the meter shows 4 digits; the model read 3" in problem for problem in result["problems"])
    settings.meter_counts = 0
    within_limit = await read(settings, answer("5.10", 5.10, "510", 1))
    settings.meter_counts = 6000
    three_digits = await read(settings, answer("5.10", 5.10, "510", 1))
    assert within_limit["status"] == "confirmed"
    assert three_digits["status"] == "uncertain"


async def test_self_contradicting_answer_is_uncertain(settings: Settings) -> None:
    result = await read(settings, answer("1.415", 14.15, "1415", 1))
    assert result["status"] == "uncertain"
    assert result["value"] is None
    assert any(problem.startswith(DISAGREE_PROBLEM) for problem in result["problems"])


async def test_1_415_with_expected_3_3_and_6000_counts_is_confirmed(settings: Settings) -> None:
    settings.meter_counts = 6000
    result = await read(settings, answer("1.415", 1.415, "1415", 1), frames=3, expected_value=3.3)
    assert result["status"] == "confirmed"
    assert result["value"] == 1.415
    assert result["expected_value"] == 3.3
    assert (result["digits"], result["digits_before_point"]) == ("1415", 1)


async def test_bench_measure_passes_the_expected_value(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), fake_vision(answer("14.15", 14.15, "1415", 2)))
    async with Client(build_server(services)) as client:
        result = await client.call_tool("bench_measure", {"expected_value": 3.3})
    assert result.structured_content is not None, result.content
    meter = result.structured_content["meter"]
    assert meter["status"] == "uncertain"
    assert meter["expected_value"] == 3.3


# endregion


# region: round 5, the display profile only for volts and amperes, fewer digits only without a prefix


@pytest.mark.parametrize(
    ("display_text", "mode", "unit"),
    [
        ("12.3", "dc_voltage", "mV"),  # " 12.3" on the 600.0 mV range: the leading digit is blank
        ("51.0", "resistance", "\u03a9"),  # " 51.0" on the 600.0 ohm range
        ("50.0", "duty_cycle", "%"),
        ("9.999", "frequency", "kHz"),  # frequency often has other counts (9999)
        ("12.3", "dc_current", "uA"),
        ("12.3", "dc_current", "mA"),
    ],
)
def test_the_profile_does_not_lower_a_blank_leading_digit_or_other_functions(
    display_text: str, mode: str, unit: str
) -> None:
    result = check_reading(reading(display_text, float(display_text), mode=mode, unit=unit), counts=COUNTS)
    assert result.status is MeterStatus.CONFIRMED, result.problems
    assert result.value == float(display_text)


@pytest.mark.parametrize("unit", ["V", "A"])
def test_fewer_digits_in_the_base_unit_stay_uncertain(unit: str) -> None:
    mode = "dc_voltage" if unit == "V" else "dc_current"
    for display_text in ("51.0", "93.2"):
        result = check_reading(reading(display_text, float(display_text), mode=mode, unit=unit), counts=COUNTS)
        assert result.status is MeterStatus.UNCERTAIN
        assert any("the meter shows 4 digits; the model read 3" in problem for problem in result.problems)


def test_more_digits_and_the_counts_limit_stay_for_all_prefixes() -> None:
    more = check_reading(reading("123.45", 123.45, unit="mV"), counts=COUNTS)
    assert more.status is MeterStatus.UNCERTAIN
    assert any("the meter shows 4 digits; the model read 5" in problem for problem in more.problems)
    above = check_reading(reading("612.3", 612.3, unit="mV"), counts=COUNTS)
    assert above.status is MeterStatus.UNCERTAIN
    assert any("above the 6000 counts" in problem for problem in above.problems)
    leading_zero = check_reading(reading("012.3", 12.3, unit="mV", flags=["AUTO"]), counts=COUNTS)
    assert leading_zero.status is MeterStatus.UNCERTAIN
    assert any("leading zero" in problem for problem in leading_zero.problems)


def test_the_profile_does_not_limit_other_functions() -> None:
    # A value above 6000 counts, a leading zero, or 5 digits are not lowered outside volts and amperes.
    for display_text, mode, unit in (
        ("9999", "frequency", "Hz"),
        ("06.80", "capacitance", "uF"),
        ("0.4430", "resistance", "M\u03a9"),
    ):
        result = check_reading(
            reading(display_text, float(display_text), mode=mode, unit=unit, flags=["AUTO"]), counts=COUNTS
        )
        assert result.status is MeterStatus.CONFIRMED, (display_text, result.problems)


async def test_12_3_mv_with_6000_counts_is_confirmed_through_the_tool(settings: Settings) -> None:
    settings.meter_counts = COUNTS
    result = await read(settings, {**answer("12.3", 12.3, "123", 2), "unit": "mV"})
    assert result["status"] == "confirmed", result["problems"]
    assert result["value"] == 12.3


# endregion
