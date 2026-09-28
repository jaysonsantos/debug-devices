"""Several frames of one meter reading: a value is confirmed only when the frames agree.

A single frame can misplace the decimal point ("51.0 V" for "5.10 V"). multimeter_read reads 2-3 frames about a second
apart. The combined result is confirmed only when every frame is confirmed, the digits and the decimal point position
are the same, and the value is below the plausibility limit of its unit (--max-voltage, --max-current).
"""

from pydantic import BaseModel

from debug_devices_mcp.multimeter import (
    FrameReading,
    MeterMode,
    MeterResult,
    MeterStatus,
    UnitFamily,
    impossible_leading_zero,
    is_auto_range,
    lower,
    signature,
    signed_value,
    unit_parts,
    uses_display_profile,
)

MIN_FRAMES = 1
MAX_FRAMES = 3
DEFAULT_FRAMES = 2
# The user's mode confirmation needs this many frames that agree.
USER_MODE_MIN_FRAMES = 2
# Worst first: the combined status is the worst frame status.
STATUS_ORDER = (MeterStatus.DISPUTED, MeterStatus.UNREADABLE, MeterStatus.UNCERTAIN, MeterStatus.CONFIRMED)
PREFIX_FACTORS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "M": 1e6}
BASE_SYMBOLS = {UnitFamily.VOLTAGE: "V", UnitFamily.CURRENT: "A"}
MORE_FRAMES_REQUEST = (
    "Read the meter again (multimeter_read with frames 2 or 3) and keep the probes still. If the digits keep "
    "changing, report the range, not one value."
)


# region: expected value
# A nominal value of the test (for example 3.3 for a 3.3 V rail) is context: a value far from it is more likely a
# misplaced decimal point than a real result. It can lower a result, never confirm one.

# A value outside [expected / band, expected x band] is suspicious.
EXPECTED_BAND = 3.0
BASE_UNITS = {UnitFamily.VOLTAGE: "V", UnitFamily.CURRENT: "A", UnitFamily.RESISTANCE: "\u03a9"}
PLACE_WORDS = {1: "one place", 2: "two places", 3: "three places"}
EXPECTED_REQUEST = (
    "Check the decimal point: ask the user to read the LCD, or read the meter again with the LCD larger in the frame."
)


def in_band(value: float, expected: float) -> bool:
    return abs(expected) / EXPECTED_BAND <= abs(value) <= abs(expected) * EXPECTED_BAND


def expected_problems(result: MeterResult, expected: float | None, counts: int) -> list[str]:
    """Compare the number on the LCD (in the base unit) with the expected value, and name a point shift that fits."""
    if not expected or not result.readable or result.overload:
        return []
    family, prefix = unit_parts(result.unit)
    symbol, factor = BASE_UNITS.get(family), PREFIX_FACTORS.get(prefix)
    digits, point = signature(result.display_text)
    if symbol is None or factor is None or not digits:
        return []
    value = signed_value(result.display_text, digits, point) * factor
    if in_band(value, expected):
        return []
    problems = [
        f"{value:g} {symbol} is {abs(value) / abs(expected):.2g} \u00d7 the expected {expected:g} {symbol}: check the "
        "decimal point; ask the user to read the LCD"
    ]
    current = point if point is not None else len(digits)
    auto = uses_display_profile(result.unit, counts) and is_auto_range(result.flags, result.range)
    candidates = []
    for position in range(1, len(digits) + 1):
        new_point = position if position < len(digits) else None
        if position == current:
            continue
        if auto and impossible_leading_zero(digits, new_point):
            continue
        shifted = signed_value(result.display_text, digits, new_point) * factor
        if in_band(shifted, expected):
            candidates.append((abs(abs(shifted) - abs(expected)) / abs(expected), position, shifted))
    if candidates:
        _, position, shifted = min(candidates)
        places = abs(position - current)
        direction = "left" if position < current else "right"
        problems.append(
            f"{shifted:g} {symbol}, with the point {PLACE_WORDS.get(places, f'{places} places')} to the {direction}, "
            f"is near the expected {expected:g} {symbol}"
        )
    return problems


# endregion: expected value


class MeterLimits(BaseModel):
    """Plausibility limits for the bench (from the settings)."""

    max_volts: float
    max_amps: float


def base_value(result: MeterResult) -> float | None:
    """The value in V or A (the unit prefix applied), or None for other families or no value."""
    symbol = BASE_SYMBOLS.get(result.unit_family)
    if symbol is None or result.value is None:
        return None
    prefix = result.unit.strip().removesuffix(symbol).strip()
    factor = PREFIX_FACTORS.get(prefix)
    return None if factor is None else result.value * factor


def frame_of(result: MeterResult) -> FrameReading:
    if result.capture_id is None or result.captured_at is None:
        raise ValueError("a meter frame needs its capture id and time")
    return FrameReading(
        capture_id=result.capture_id,
        captured_at=result.captured_at,
        display_text=result.display_text,
        unit=result.unit,
        mode=result.mode,
        value=result.value,
        confidence=result.confidence,
        status=result.status,
    )


def limit_problem(result: MeterResult, limits: MeterLimits) -> str | None:
    value = base_value(result)
    if value is None:
        return None
    limit = limits.max_volts if result.unit_family is UnitFamily.VOLTAGE else limits.max_amps
    if abs(value) > limit:
        return (
            f"{result.display_text} {result.unit} is above the bench limit of {limit:g} "
            f"{BASE_SYMBOLS[result.unit_family]}: a misread (for example a wrong decimal point) is more likely"
        )
    return None


def combine(
    results: list[MeterResult],
    limits: MeterLimits,
    user_mode: MeterMode | None = None,
    *,
    expected_value: float | None = None,
    counts: int = 0,
) -> MeterResult:
    """One result from the checked frames. The first frame gives the capture id of the result.

    `expected_value` (context) and `counts` (the display profile) can only lower the combined status.
    """
    first = results[0]
    problems = list(dict.fromkeys(problem for result in results for problem in result.problems))
    status = min((result.status for result in results), key=STATUS_ORDER.index)
    for result in results:
        if (problem := limit_problem(result, limits)) is not None:
            status = MeterStatus.DISPUTED
            problems.append(problem)
    readable = [result for result in results if result.readable]
    signatures = {signature(result.display_text) for result in readable}
    points = {point for _, point in signatures}
    stable = len(signatures) == 1 and len(readable) == len(results)
    if len(points) > 1:
        status = MeterStatus.DISPUTED
        problems.append(
            "the decimal point moved between the frames ("
            + ", ".join(result.display_text for result in readable)
            + "): one of the frames is a misread"
        )
    elif len(signatures) > 1 and status is MeterStatus.CONFIRMED:
        status = MeterStatus.UNCERTAIN
        problems.append("the digits changed between the frames: the value is not stable")
    if user_mode is not None and len(results) < USER_MODE_MIN_FRAMES and status is MeterStatus.CONFIRMED:
        status = MeterStatus.UNCERTAIN
        problems.append(f"a mode from the user's confirmation needs {USER_MODE_MIN_FRAMES} frames that agree")
    far_from_expected = expected_problems(first, expected_value, counts)
    status = lower(status, far_from_expected, problems)
    confirmed = status is MeterStatus.CONFIRMED
    values = [result.value for result in readable if result.value is not None and result.unit == first.unit]
    request = None
    if not confirmed:
        request = EXPECTED_REQUEST if far_from_expected and first.request is None else first.request
        request = request or MORE_FRAMES_REQUEST
        if len(signatures) > 1 and MORE_FRAMES_REQUEST not in request:
            request = f"{request} {MORE_FRAMES_REQUEST}"
    return first.model_copy(
        update={
            "status": status,
            "value": first.value if confirmed and not first.overload else None,
            "problems": problems,
            "request": request,
            "frames": [frame_of(result) for result in results],
            "stable": stable,
            "value_min": min(values) if values else None,
            "value_max": max(values) if values else None,
            "expected_value": expected_value,
        }
    )
