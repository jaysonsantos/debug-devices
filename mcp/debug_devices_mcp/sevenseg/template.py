"""The built-in LCD layout of the Proster T21D (6000 counts, 4 digits) in normalized LCD coordinates.

The digit row, the decimal points, "AUTO", and "kΩ" come from a real photo of the meter (docs/images/demo-meter.jpg).
The positions of the other symbols are estimates: check them on the annotated calibration image, and move a region
in the profile JSON when it misses its symbol.
"""

from debug_devices_mcp.sevenseg.constants import TEMPLATE_T21D
from debug_devices_mcp.sevenseg.profile import LcdLayout, Rect, Segment, Symbol

# region: digit row
DIGIT_COUNT = 4
DIGIT_LEFT = 0.07
DIGIT_PITCH = 0.165
DIGIT_WIDTH = 0.13
DIGIT_TOP = 0.22
DIGIT_HEIGHT = 0.60
DIGIT_SLANT = 0.08
# The decimal point sits at the bottom, in the gap after its digit.
POINT_WIDTH = 0.03
POINT_HEIGHT = 0.08
POINT_GAP = 0.004
SIGN = Rect(x=0.005, y=0.48, width=0.05, height=0.06)
# endregion: digit row

# region: segments (inner parts of each segment in digit-box coordinates: the sensing area, not the full stroke)
HORIZONTAL_LEFT = 0.25
HORIZONTAL_WIDTH = 0.5
HORIZONTAL_HEIGHT = 0.07
VERTICAL_WIDTH = 0.14
VERTICAL_HEIGHT = 0.24
LEFT_EDGE = 0.02
RIGHT_EDGE = 1.0 - LEFT_EDGE - VERTICAL_WIDTH
UPPER_TOP = 0.13
LOWER_TOP = 0.63
SEGMENTS: dict[Segment, Rect] = {
    Segment.A: Rect(x=HORIZONTAL_LEFT, y=0.01, width=HORIZONTAL_WIDTH, height=HORIZONTAL_HEIGHT),
    Segment.B: Rect(x=RIGHT_EDGE, y=UPPER_TOP, width=VERTICAL_WIDTH, height=VERTICAL_HEIGHT),
    Segment.C: Rect(x=RIGHT_EDGE, y=LOWER_TOP, width=VERTICAL_WIDTH, height=VERTICAL_HEIGHT),
    Segment.D: Rect(x=HORIZONTAL_LEFT, y=0.92, width=HORIZONTAL_WIDTH, height=HORIZONTAL_HEIGHT),
    Segment.E: Rect(x=LEFT_EDGE, y=LOWER_TOP, width=VERTICAL_WIDTH, height=VERTICAL_HEIGHT),
    Segment.F: Rect(x=LEFT_EDGE, y=UPPER_TOP, width=VERTICAL_WIDTH, height=VERTICAL_HEIGHT),
    Segment.G: Rect(x=HORIZONTAL_LEFT, y=0.465, width=HORIZONTAL_WIDTH, height=HORIZONTAL_HEIGHT),
}
# endregion: segments

# region: symbols
SYMBOLS: dict[Symbol, Rect] = {
    # Seen in the photo.
    Symbol.AUTO: Rect(x=0.06, y=0.12, width=0.16, height=0.09),
    Symbol.KILO: Rect(x=0.815, y=0.64, width=0.04, height=0.16),
    Symbol.OHM: Rect(x=0.865, y=0.64, width=0.11, height=0.16),
    # Estimates: the top row.
    Symbol.DC: Rect(x=0.26, y=0.04, width=0.07, height=0.07),
    Symbol.AC: Rect(x=0.34, y=0.04, width=0.07, height=0.07),
    Symbol.HOLD: Rect(x=0.44, y=0.04, width=0.12, height=0.07),
    Symbol.DIODE: Rect(x=0.60, y=0.04, width=0.06, height=0.07),
    Symbol.CONTINUITY: Rect(x=0.68, y=0.04, width=0.06, height=0.07),
    Symbol.LOW_BATTERY: Rect(x=0.80, y=0.04, width=0.08, height=0.07),
    # Estimates: the unit column at the right.
    Symbol.PERCENT: Rect(x=0.815, y=0.14, width=0.06, height=0.10),
    Symbol.HERTZ: Rect(x=0.885, y=0.14, width=0.09, height=0.10),
    Symbol.NANO: Rect(x=0.815, y=0.28, width=0.04, height=0.10),
    Symbol.MICRO: Rect(x=0.865, y=0.28, width=0.04, height=0.10),
    Symbol.FARAD: Rect(x=0.925, y=0.28, width=0.04, height=0.10),
    Symbol.MILLI: Rect(x=0.815, y=0.44, width=0.05, height=0.12),
    Symbol.AMPERE: Rect(x=0.875, y=0.44, width=0.045, height=0.12),
    Symbol.VOLT: Rect(x=0.93, y=0.44, width=0.045, height=0.12),
    Symbol.MEGA: Rect(x=0.76, y=0.64, width=0.045, height=0.16),
}
# endregion: symbols

LCD_ASPECT = 2.2


def t21d_layout() -> LcdLayout:
    digits = [
        Rect(x=DIGIT_LEFT + index * DIGIT_PITCH, y=DIGIT_TOP, width=DIGIT_WIDTH, height=DIGIT_HEIGHT)
        for index in range(DIGIT_COUNT)
    ]
    points = [
        Rect(
            x=digit.x + digit.width + POINT_GAP,
            y=digit.y + digit.height - POINT_HEIGHT,
            width=POINT_WIDTH,
            height=POINT_HEIGHT,
        )
        for digit in digits[:-1]
    ]
    return LcdLayout(
        aspect=LCD_ASPECT,
        digits=digits,
        slant=DIGIT_SLANT,
        segments=dict(SEGMENTS),
        points=points,
        sign=SIGN,
        symbols=dict(SYMBOLS),
    )


TEMPLATES = {TEMPLATE_T21D: t21d_layout}
