"""The local 7-segment decoder (sevenseg/decode.py) on synthetic LCD crops: digits, points, sign, OL, symbols, and
degraded frames. The tests draw every image."""

from datetime import timedelta

import pytest

from debug_devices_mcp.meter_mode import MeterMode
from debug_devices_mcp.sevenseg.decode import UNKNOWN_CHAR, decode_digit, decode_jpeg, pattern
from debug_devices_mcp.sevenseg.profile import Point, Symbol
from debug_devices_mcp.sevenseg.reading import LocalReading, LocalStatus
from debug_devices_mcp.sevenseg.template import t21d_layout

from .sevenseg_synth import CLEAN, CROP_SIZE, Degradation, Scene, crop_jpeg, degrade, jpeg, place_on_crop, profile_for
from .sevenseg_synth import render_lcd as render

LAYOUT = t21d_layout()
PROFILE = profile_for(LAYOUT)
VOLTS = {Symbol.VOLT, Symbol.DC}


def read(scene: Scene, degradation: Degradation = CLEAN) -> LocalReading:
    return decode_jpeg(crop_jpeg(LAYOUT, scene, degradation), PROFILE).reading


# region: digits, point, sign


@pytest.mark.parametrize("digit", "0123456789")
def test_every_digit_in_every_position(digit: str) -> None:
    reading = read(Scene(digit * 4, symbols=VOLTS))
    assert reading.status is LocalStatus.READ
    assert reading.digits == digit * 4
    assert reading.value == int(digit * 4)


@pytest.mark.parametrize("point", [0, 1, 2])
def test_the_decimal_point_position(point: int) -> None:
    reading = read(Scene("1234", point=point, symbols=VOLTS))
    assert reading.digits_before_point == point + 1
    assert reading.display_text == "1234"[: point + 1] + "." + "1234"[point + 1 :]
    assert reading.value == pytest.approx(1234 / 10 ** (3 - point))


def test_the_minus_sign() -> None:
    reading = read(Scene("1234", point=0, negative=True, symbols={Symbol.MILLI, Symbol.VOLT}))
    assert (reading.display_text, reading.negative, reading.value, reading.unit) == ("-1.234", True, -1.234, "mV")


def test_a_blank_leading_digit_is_not_a_zero() -> None:
    reading = read(Scene(" 510", point=1, symbols=VOLTS))
    assert (reading.display_text, reading.digits, reading.digits_before_point) == ("5.10", "510", 1)
    assert reading.value == pytest.approx(5.10)


def test_overload() -> None:
    reading = read(Scene(" 0L ", symbols={Symbol.MEGA, Symbol.OHM}))
    assert reading.overload
    assert (reading.display_text, reading.digits, reading.value) == ("OL", None, None)
    assert (reading.unit, reading.mode) == ("MΩ", MeterMode.RESISTANCE)
    assert reading.status is LocalStatus.READ


def test_a_blank_digit_between_digits_is_uncertain() -> None:
    reading = read(Scene("4 70", point=0, symbols={Symbol.KILO, Symbol.OHM}))
    assert reading.status is LocalStatus.UNCERTAIN
    assert "a blank digit between digits" in reading.problems


def test_digit_patterns() -> None:
    assert decode_digit(pattern("abcdefg")) == ("8", True)
    assert decode_digit(pattern("")) == (" ", True)
    # One segment missing from "2" (abdeg), and no other pattern at that distance.
    assert decode_digit(pattern("abde")) == ("2", False)
    assert decode_digit(pattern("af")) == (UNKNOWN_CHAR, False)


# endregion

# region: symbols


@pytest.mark.parametrize(
    ("symbols", "unit", "mode"),
    [
        ({Symbol.VOLT, Symbol.DC}, "V", MeterMode.DC_VOLTAGE),
        ({Symbol.MILLI, Symbol.VOLT}, "mV", MeterMode.DC_VOLTAGE),
        ({Symbol.VOLT, Symbol.AC}, "V", MeterMode.AC_VOLTAGE),
        ({Symbol.MILLI, Symbol.AMPERE}, "mA", MeterMode.DC_CURRENT),
        ({Symbol.AMPERE, Symbol.AC}, "A", MeterMode.AC_CURRENT),
        ({Symbol.KILO, Symbol.OHM}, "kΩ", MeterMode.RESISTANCE),
        ({Symbol.OHM, Symbol.CONTINUITY}, "Ω", MeterMode.CONTINUITY),
        ({Symbol.VOLT, Symbol.DIODE}, "V", MeterMode.DIODE),
        ({Symbol.NANO, Symbol.FARAD}, "nF", MeterMode.CAPACITANCE),
        ({Symbol.MICRO, Symbol.FARAD}, "µF", MeterMode.CAPACITANCE),
        ({Symbol.KILO, Symbol.HERTZ}, "kHz", MeterMode.FREQUENCY),
        ({Symbol.PERCENT}, "%", MeterMode.DUTY_CYCLE),
    ],
)
def test_symbols_give_the_unit_and_the_mode(symbols: set[Symbol], unit: str, mode: MeterMode) -> None:
    reading = read(Scene("1234", point=1, symbols=symbols))
    assert (reading.unit, reading.mode, reading.status) == (unit, mode, LocalStatus.READ)


def test_flags() -> None:
    reading = read(Scene("1234", symbols={Symbol.AUTO, Symbol.HOLD, Symbol.LOW_BATTERY, Symbol.VOLT}))
    assert reading.flags == ["AUTO", "HOLD", "low battery"]


def test_no_unit_symbol_is_uncertain() -> None:
    reading = read(Scene("1234"))
    assert (reading.unit, reading.mode, reading.status) == ("unknown", MeterMode.OTHER, LocalStatus.UNCERTAIN)
    assert "no unit symbol is on" in reading.problems


# endregion

# region: degraded frames

DEGRADED = [
    Degradation(blur=1.2),
    Degradation(noise=8),
    Degradation(glare=70),
    Degradation(glare=60, glare_at=(0.3, 0.6)),
    Degradation(blur=1.0, noise=6, glare=50),
]


@pytest.mark.parametrize("degradation", DEGRADED)
def test_blur_noise_and_glare(degradation: Degradation) -> None:
    reading = read(Scene("3456", point=0, negative=True, symbols={Symbol.MILLI, Symbol.VOLT}), degradation)
    assert (reading.display_text, reading.unit, reading.status) == ("-3.456", "mV", LocalStatus.READ)


def test_a_strong_jpeg_compression() -> None:
    image = degrade(place_on_crop(render(LAYOUT, Scene("5908", point=2, symbols=VOLTS))), Degradation(noise=4))
    reading = decode_jpeg(jpeg(image, quality=50), PROFILE).reading
    assert reading.display_text == "590.8"


def test_another_perspective() -> None:
    corners = (Point(x=70, y=50), Point(x=360, y=80), Point(x=350, y=235), Point(x=62, y=200))
    image = place_on_crop(render(LAYOUT, Scene("2718", point=0, symbols=VOLTS)), corners)
    reading = decode_jpeg(jpeg(image), profile_for(LAYOUT, corners)).reading
    assert (reading.display_text, reading.status) == ("2.718", LocalStatus.READ)


def test_low_contrast_is_unreadable() -> None:
    reading = read(Scene("1234", symbols=VOLTS, paper=150, ink=145))
    assert (reading.status, reading.readable, reading.value) == (LocalStatus.UNREADABLE, False, None)
    assert "contrast" in reading.problems[0]


def test_a_changed_crop_is_unreadable() -> None:
    other = PROFILE.model_copy(update={"image_width": CROP_SIZE[0] + 10})
    reading = decode_jpeg(crop_jpeg(LAYOUT, Scene("1234", symbols=VOLTS)), other).reading
    assert reading.status is LocalStatus.UNREADABLE
    assert "crop changed" in reading.problems[0]


def test_a_frame_takes_milliseconds() -> None:
    decoded = decode_jpeg(crop_jpeg(LAYOUT, Scene("1234", symbols=VOLTS)), PROFILE)
    # Generous for a busy CI machine: the vision model takes 5-10 s per frame.
    assert decoded.elapsed < timedelta(milliseconds=500)


# endregion
