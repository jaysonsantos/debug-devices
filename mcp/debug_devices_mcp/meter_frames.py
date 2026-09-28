"""Several frames of one meter reading: a value is confirmed only when the frames agree.

A single frame can misplace the decimal point ("51.0 V" for "5.10 V"). multimeter_read reads 2-3 frames about a second
apart. The combined result is confirmed only when every frame is confirmed, the digits and the decimal point position
are the same, and the value is below the plausibility limit of its unit (--max-voltage, --max-current).
"""

from pydantic import BaseModel

from debug_devices_mcp.constants import defaults
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
DIODE_REQUEST = (
    "Ask the user which mode the dial is on. If it is DC V, record it (bench_state_update "
    "meter_mode_confirmed_by_user dc_voltage) and read the meter again."
)
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
    max_diode_volts: float = defaults.MAX_DIODE_VOLTAGE


def base_value(result: MeterResult) -> float | None:
    """The number on the LCD in V or A (the unit prefix applied), or None for other families, an overload, or no digits.

    It comes from the LCD text, not from `value`: a frame that a different check lowered (the digit count, a low
    confidence) has no value, but the bench limit still applies to the number that the LCD shows.
    """
    family, prefix = unit_parts(result.unit)
    factor = PREFIX_FACTORS.get(prefix)
    digits, point = signature(result.display_text)
    if family not in BASE_SYMBOLS or factor is None or not digits or not result.readable or result.overload:
        return None
    return signed_value(result.display_text, digits, point) * factor


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
        model_mode=result.model_mode,
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


def diode_problem(result: MeterResult, limits: MeterLimits) -> str | None:
    """A diode test shows at most the test voltage of the meter; a higher number is a DC V reading with a wrong mode.

    The mode is the checked one: a DC V mode that the user confirmed replaces the model's "diode".
    """
    value = base_value(result)
    if result.mode is not MeterMode.DIODE or value is None or abs(value) <= limits.max_diode_volts:
        return None
    return (
        f"{result.display_text} {result.unit} in diode mode is above the diode test voltage of "
        f"{limits.max_diode_volts:g} V: the dial is probably on DC V"
    )


def unit_key(unit: str) -> tuple[UnitFamily, str] | None:
    """The family and the prefix ("V" and "mV" differ). None for a unit that is not readable: that frame is already
    uncertain, and it does not show another unit."""
    family, prefix = unit_parts(unit)
    return (family, prefix) if family is not UnitFamily.UNKNOWN else None


def sign_key(display_text: str) -> bool | None:
    """True for a negative number, with the rule of `signed_value` (a minus anywhere before the first digit, as in
    "DC -5.10"); None for a zero or no digits (a meter can show "-0.00" and "0.00" at zero)."""
    digits, point = signature(display_text)
    if not digits.strip("0"):
        return None
    return signed_value(display_text, digits, point) < 0


def disagreements(readable: list[MeterResult]) -> list[str]:
    """The frames must show the same unit, mode, and sign: "4.98 V" and "4.98 mV", or "-0.12" and "0.12", disagree."""
    problems = []
    checks = (
        ("unit", [result.unit for result in readable], [unit_key(result.unit) for result in readable]),
        ("mode", [str(result.mode) for result in readable], [result.mode for result in readable]),
        ("sign", [result.display_text for result in readable], [sign_key(result.display_text) for result in readable]),
    )
    for name, texts, keys in checks:
        if len({key for key in keys if key is not None}) > 1:
            problems.append(
                f"the {name} changed between the frames ({', '.join(texts)}): one of the frames is a misread"
            )
    return problems


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
    # Frames with the same LCD text give the same problem: show it once.
    above_limit = list(dict.fromkeys(problem for result in results if (problem := limit_problem(result, limits))))
    if above_limit:
        status = MeterStatus.DISPUTED
        problems.extend(above_limit)
    readable = [result for result in results if result.readable]
    signatures = {signature(result.display_text) for result in readable}
    points = {point for _, point in signatures}
    diode = list(dict.fromkeys(problem for result in results if (problem := diode_problem(result, limits))))
    status = lower(status, diode, problems)
    changed = disagreements(readable)
    stable = len(signatures) == 1 and not changed and len(readable) == len(results)
    if len(points) > 1:
        status = MeterStatus.DISPUTED
        problems.append(
            "the decimal point moved between the frames ("
            + ", ".join(result.display_text for result in readable)
            + "): one of the frames is a misread"
        )
    if changed:
        status = MeterStatus.DISPUTED
        problems.extend(changed)
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
        request = request or (DIODE_REQUEST if diode else MORE_FRAMES_REQUEST)
        if (len(signatures) > 1 or changed) and MORE_FRAMES_REQUEST not in request:
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
